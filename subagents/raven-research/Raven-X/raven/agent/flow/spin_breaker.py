"""Spin-entry circuit breaker (DR flow).

A research spin usually announces itself in prose before it burns the
budget: "let me start from scratch", "completely different approach",
"going in circles". A density rollback at that point replays the same
entry; the productive intervention is to redirect the turn to commit an
answer from the evidence already gathered.

Three joint criteria separate a spin entry from legitimate re-anchoring
on a secondary lead:

- **repeated restart language** — the first restart phrase of a turn is
  normal research; interception starts at the Nth iteration carrying one;
- **late budget** — restarts early in the turn are exploration;
- **entity overlap** — a restart re-plowing the same candidates shares
  entities with the previous restart, while a legitimate re-anchor
  introduces new ones and passes.

On interception the restart response is popped (its proposed tool calls
are discarded unexecuted) and a persisted user note redirects the
re-sample to write the final answer.
"""

from __future__ import annotations

import logging
import re

from raven.agent.evidence_round import EvidenceRound
from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision
from raven.agent.hook.observers.budget import usage_tokens

logger = logging.getLogger(__name__)


def _response_text(response: object) -> str:
    if response is None:
        return ""
    parts = []
    for attr in ("reasoning_content", "content"):
        value = getattr(response, attr, None)
        if value is None and isinstance(response, dict):
            value = response.get(attr)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


_RESTART_MARKERS = (
    "start from scratch",
    "from scratch",
    "start over",
    "starting over",
    # "different approach" / "completely different approach" were removed here:
    # the identity segment tells the model, every turn, to retry "with a
    # different approach" (context_engine/segments/render.py) and the loop-break
    # nudge says it a second time, so the phrase is something we teach rather
    # than a symptom. It also carried no signal: measured on one 120-question
    # batch it appeared on 75 anchor questions and 95 treated ones, and the
    # >=2-hit entry criterion was met by 67 of 120 anchor questions against 88
    # treated. Without the two idioms the same criterion is met by 6 anchor
    # questions against 27 treated - the detector only discriminates once its
    # own vocabulary stops overlapping with the instructions we give.
    "different strategy",
    "going in circles",
    "try again from the beginning",
    "restart the search",
    "重新开始",
    "从头再来",
    "从头开始",
    "换个思路",
    "换一个思路",
    "换个方向",
    "原地打转",
)

_FORCE_REPORT_NOTE = (
    "[research checkpoint] You are restarting research that was already "
    "done. Do not start over. Using only the evidence already gathered "
    "above, commit to the best-supported answer and write the final answer "
    "now: the answer first, then the key evidence with source URLs, then "
    "any remaining uncertainty."
)

_QUOTED_RE = re.compile(r"\"([^\"\n]{2,60})\"|“([^”\n]{2,60})”")
_CAP_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z0-9'\-]{2,}\b")
_CJK_RUN_RE = re.compile(r"[一-鿿]{2,8}")


def _entities(text: str) -> set[str]:
    ents: set[str] = set()
    for m in _QUOTED_RE.finditer(text):
        ents.add((m.group(1) or m.group(2) or "").strip().lower())
    ents.update(t.lower() for t in _CAP_TOKEN_RE.findall(text))
    ents.update(_CJK_RUN_RE.findall(text))
    ents.discard("")
    return ents


def _first_marker(text: str) -> str | None:
    lowered = text.lower()
    for marker in _RESTART_MARKERS:
        if marker in lowered:
            return marker
    return None


class SpinEntryBreaker(AgentHook):
    """Intercept the restart moment and redirect the turn to its report."""

    def __init__(
        self,
        max_iterations: int,
        context_window_tokens: int = 0,
        phrase_hits: int = 2,
        min_budget_ratio: float = 0.5,
        min_entity_overlap: int = 2,
        max_triggers: int = 1,
        evidence_round: "EvidenceRound | None" = None,
    ) -> None:
        self._max_iterations = max_iterations
        self._context_window_tokens = context_window_tokens
        self._phrase_hits = phrase_hits
        self._min_budget_ratio = min_budget_ratio
        self._min_entity_overlap = min_entity_overlap
        self._max_triggers = max_triggers
        self._evidence_round = evidence_round

    @property
    def name(self) -> str:
        return "SpinEntryBreaker"

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        return self._scan(ctx)

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        return self._scan(ctx)

    def _scan(self, ctx: AgentHookContext) -> HookDecision:
        text = _response_text(ctx.response)
        if not text:
            return HookDecision()
        marker = _first_marker(text)
        if marker is None:
            return HookDecision()

        state = ctx.metadata.setdefault("spin_breaker", {"hits": [], "triggers": 0})
        prev_entities: set[str] | None = state.get("_prev_entities")
        entities = _entities(text)
        overlap = len(entities & prev_entities) if prev_entities is not None else 0
        state["hits"].append(
            {
                "iteration": ctx.iteration,
                "marker": marker,
                "budget_ratio": round(self._budget_ratio(ctx), 3),
                "entity_overlap": overlap,
            }
        )
        state["_prev_entities"] = entities

        if state["triggers"] >= self._max_triggers:
            return HookDecision()
        if len(state["hits"]) < self._phrase_hits:
            return HookDecision()
        if self._budget_ratio(ctx) < self._min_budget_ratio:
            return HookDecision()
        # Entity gate: a restart that shares candidates with the previous
        # restart is re-plowing the same ground. When either side yields
        # too few entities to judge, the phrase + budget criteria decide.
        if (
            prev_entities is not None
            and len(prev_entities) >= 3
            and len(entities) >= 3
            and overlap < self._min_entity_overlap
        ):
            return HookDecision(notes=["spin_breaker_pass_new_entities"])

        # Placed after every other criterion so the counter means "a trigger that
        # would have fired was withheld", not "a round was open at the time". The
        # collision is structural rather than hypothetical: a verify rejection
        # arrives late in the budget by construction, which is exactly this
        # breaker's `min_budget_ratio` side, and the retrieval it sanctions is the
        # thing `_FORCE_REPORT_NOTE` exists to forbid. Measured on the dr@2.7 corpus
        # arm, 16 of the 17 questions carrying both events already spent this
        # breaker's single trigger BEFORE the rejection - so on the pre-change
        # distribution this stand-down is mostly defensive. That distribution is
        # what the change alters: telling the model to go searching is what makes
        # restart vocabulary likely on the turn that follows.
        if self._evidence_round is not None and self._evidence_round.active:
            self._evidence_round.note_spin_pass()
            logger.warning(
                "spin-breaker: restart language (%r) inside a sanctioned evidence round; standing down (pass %d)",
                marker,
                self._evidence_round.spin_passes,
            )
            return HookDecision(notes=["spin_breaker_pass_evidence_round"])

        state["triggers"] += 1
        logger.warning(
            "spin-breaker: restart language (%r) at iteration %s, budget %.0f%%, overlap %d; forcing report",
            marker,
            ctx.iteration,
            100 * self._budget_ratio(ctx),
            overlap,
        )
        return HookDecision(
            rollback=True,
            rollback_inject=[{"role": "user", "content": _FORCE_REPORT_NOTE}],
            notes=[f"spin_breaker_trigger marker={marker}"],
        )

    def _budget_ratio(self, ctx: AgentHookContext) -> float:
        ratio = 0.0
        if self._max_iterations and ctx.iteration:
            ratio = ctx.iteration / self._max_iterations
        used = usage_tokens(ctx.response)
        if used and self._context_window_tokens:
            ratio = max(ratio, used / self._context_window_tokens)
        return ratio


__all__ = ["SpinEntryBreaker"]
