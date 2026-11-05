"""Best guess (DR flow): a draft that abstains is sent back to name its likeliest answer.

On a 400-question batch 25 of 178 wrong runs ended on an abstention ("No verifiable
answer"), 16 of them after a reviewer's rejection. An abstention gives a reader nothing
to check and is never right; a named candidate sometimes is. The check reads only the
draft's answer span (``support.best_guess``), so a report that doubts a clue above a
named answer is not bounced.

Sits before the reviewer: the draft it bounces is not worth a review, and the one that
comes back is reviewed as any other.
"""

from __future__ import annotations

import logging

from raven.contracts.loop_hooks import HookDecision
from research_flow.gates.base import Gate, GateCtx
from research_flow.support.answer_text import closing_tag_bar, visible_answer
from research_flow.support.best_guess import abstains

logger = logging.getLogger(__name__)

_BEST_GUESS_PROMPT = (
    "The draft above does not name an answer. The reader needs one even when the "
    "evidence is incomplete. If one more specific check could settle it, run it; then "
    "name the single candidate your evidence best supports, in the answer format, and "
    "say how confident you are and what remains unconfirmed. Do not reply that the "
    "answer cannot be determined."
)


class BestGuessGate(Gate):
    """Bounce a draft whose answer abstains, at most ``bounces`` times per attempt."""

    def __init__(self, bounces: int = 1, closing_tag_required: bool = False) -> None:
        self._bounces = bounces
        self._closing_tag_required = closing_tag_required

    @property
    def name(self) -> str:
        return "BestGuessGate"

    async def after_iteration(self, ctx: GateCtx) -> HookDecision:
        if getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        draft = visible_answer(
            getattr(ctx.response, "content", None) or "",
            closing_tag_required=closing_tag_bar(
                self._closing_tag_required,
                getattr(ctx.response, "reasoning_content", None),
            ),
        )
        if not draft or not abstains(draft):
            return HookDecision()
        state = ctx.metadata.setdefault("best_guess", {"abstained": 0, "bounces": 0})
        state["abstained"] += 1
        if state["bounces"] >= self._bounces:
            return HookDecision(notes=["best_guess: abstained again; released"])
        state["bounces"] += 1
        logger.warning("best-guess: draft abstains; bouncing back %d/%d", state["bounces"], self._bounces)
        return HookDecision(
            rollback=True,
            rollback_inject=[
                {"role": "assistant", "content": draft},
                {"role": "user", "content": _BEST_GUESS_PROMPT},
            ],
            notes=[f"best_guess: bounced, {state['bounces']}/{self._bounces}"],
        )
