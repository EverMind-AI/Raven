"""The two-stage playbook match funnel — L1 vocabulary index, L2 LLM gate.

Per message: normalize once, substring-match the whole matchable library's
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

from raven.playbook.triggers import normalize
from raven.playbook.types import ParamSpec, Triggers

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
    contenders: list[str] = Field(default_factory=list)
    """Filled only on a too-close-to-call null match: the candidate ids the
    gate could not separate, for the funnel to hand the user as a choice."""

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
User message:
{message}

Candidate task templates (nominated by trigger words; possibly all wrong):
{candidates}

Decide: is this message clearly asking to run one of these templates?

Rules:
- The user merely mentions a related topic, asks a question, or sounds
  unsure -> set match to null. Prefer letting go over mismatching: a
  mismatch launches a whole background run, a pass-through only continues
  the normal conversation;
- When several candidates apply, pick the single best task fit; too close
  to call -> set match to null, say so in reason, and list the contending
  template ids in contenders so the user can be asked to choose;
- On a match, extract param values from the original message per that
  template's param table: extracted values go into params; required params
  absent from the message go into missing;
- confidence is "high" only when the user clearly wants this done."""


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
                        "contenders": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["match", "confidence", "reason"],
                },
            },
        }
    ]


def _render_candidates(candidates: list[MatchCandidate]) -> str:
    blocks = []
    for c in candidates:
        lines = [f"- id: {c.playbook_id}", f"  intent: {c.description}"]
        if c.params:
            lines.append("  params:")
            for name, p in c.params.items():
                need = "required" if p.required and p.default is None else f"default={p.default!r}"
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
