"""The two-stage playbook match funnel — L1 vocabulary index, L2 LLM gate.

Per message: normalize once, substring-match the whole ready library's
vocabulary (L1, pure code, no model); zero hits means the message flows on
untouched — that is the common case and it must stay free. Hits become
candidates for one gate call (L2) that judges intent, extracts params and
adjudicates between overlapping candidates in the same breath.

Failure contract mirrors ``raven.routing.knn_router``: any gate failure —
no tool call, bad JSON, unknown id — resolves to "no match" so the caller
falls through to the normal conversation. A mismatch costs a wasted run;
a pass-through costs nothing but a missed shortcut.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from raven.memory_engine.playbook.triggers import normalize
from raven.memory_engine.playbook.types import ParamSpec, Triggers

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider

_GATE_TOOL_NAME = "emit_match_verdict"


@dataclass(frozen=True)
class MatchCandidate:
    """What L1 hands to the gate for one nominated playbook."""

    playbook_id: str
    description: str
    params: dict[str, ParamSpec] = field(default_factory=dict)


class GateVerdict(BaseModel):
    """The gate's structured output — also the matcher's public result."""

    model_config = ConfigDict(extra="forbid")

    match: str | None = None
    confidence: Literal["high", "low"] = "low"
    reason: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)

    @property
    def actionable(self) -> bool:
        """True when the downstream flow (confirm gate) should engage."""
        return self.match is not None and self.confidence == "high"


class TriggerIndex:
    """L1 — the whole library's vocabulary, substring-matched per message.

    Vocabulary entries are normalized at build time with the same
    :func:`normalize` the query goes through, so matching is a plain
    ``in`` check. Library sizes here are tens of playbooks with tens of
    entries each; a scan is microseconds and needs no cleverness.
    """

    def __init__(self, library: dict[str, Triggers]) -> None:
        self._entries: list[tuple[str, str]] = [
            (normalize(entry), pid) for pid, trig in library.items() for entry in trig.keywords if entry.strip()
        ]

    def match(self, message: str) -> list[str]:
        """Playbook ids nominated by this message, first-hit order, deduped."""
        text = normalize(message)
        if not text:
            return []
        hits: list[str] = []
        for entry, pid in self._entries:
            if pid not in hits and entry in text:
                hits.append(pid)
        return hits


_GATE_PROMPT = """\
用户消息：
{message}

候选任务模板（按触发词初筛得到，可能全都不对）：
{candidates}

判断：用户这句话是不是明确想执行其中某个模板的任务？

规则：
- 用户只是聊到相关话题、提问、或表达不确定 → match 填 null。宁可放过，不可错配：
  错配会启动一整套后台执行，放过只是走普通对话；
- 多个候选都沾边时选任务契合度最高的一个；难分高下 → match 填 null 并在 reason 里说明；
- 命中时按该模板的参数表从原句抽取参数值：抽得到的进 params，必填但原句里没有的进 missing；
- confidence 只在"用户明确要干这件事"时填 high。"""


def _gate_tool() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": _GATE_TOOL_NAME,
                "description": "Submit the match verdict.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "match": {"type": ["string", "null"]},
                        "confidence": {"type": "string", "enum": ["high", "low"]},
                        "reason": {"type": "string"},
                        "params": {"type": "object"},
                        "missing": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["match", "confidence", "reason"],
                },
            },
        }
    ]


def _render_candidates(candidates: list[MatchCandidate]) -> str:
    blocks = []
    for c in candidates:
        lines = [f"- id: {c.playbook_id}", f"  意图: {c.description}"]
        if c.params:
            lines.append("  参数表:")
            for name, p in c.params.items():
                need = "必填" if p.required and p.default is None else f"默认={p.default!r}"
                lines.append(f"    - {name} ({p.type}, {need}): {p.description}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


async def gate(
    provider: "LLMProvider",
    message: str,
    candidates: list[MatchCandidate],
    *,
    model: str | None = None,
) -> GateVerdict:
    """L2 — one call: judge, extract params, adjudicate. Never raises for
    model-side failures; those resolve to a no-match verdict."""
    if not candidates:
        return GateVerdict(reason="no candidates")
    try:
        response = await provider.chat_with_retry(
            messages=[
                {
                    "role": "user",
                    "content": _GATE_PROMPT.format(message=message, candidates=_render_candidates(candidates)),
                }
            ],
            tools=_gate_tool(),
            model=model,
            tool_choice={"type": "function", "function": {"name": _GATE_TOOL_NAME}},
        )
    except Exception:  # noqa: BLE001 - a gate failure must never break the turn
        logger.opt(exception=True).warning("playbook gate call failed; passing through")
        return GateVerdict(reason="gate call failed")

    if not response.has_tool_calls:
        return GateVerdict(reason="no verdict returned")
    args = response.tool_calls[0].arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return GateVerdict(reason="unparseable verdict")
    try:
        verdict = GateVerdict.model_validate(args)
    except ValidationError:
        return GateVerdict(reason="invalid verdict shape")

    known = {c.playbook_id for c in candidates}
    if verdict.match is not None and verdict.match not in known:
        logger.warning("gate returned unknown playbook id {}; treating as no match", verdict.match)
        return GateVerdict(reason=f"unknown id {verdict.match}")
    return verdict
