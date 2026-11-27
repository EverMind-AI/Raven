"""A reply cut off at the output limit (DR flow): asked for again, shorter, before review.

A reply that ends on ``finish_reason=length`` stops mid-way, and the loop takes it as
the answer like any other: one run shipped a report cut in its third section, with no
list of sources. This gate sets such a reply aside and asks for it again, whole and
shorter than the cut one, with the tools withdrawn, at most ``max_asks`` times a turn;
past that, the cut reply ships and the turn's state says so.

It sits after the write step, whose own draft is set aside whether cut or not, and
before the reviewer, which should not spend a review on a reply that stops mid-way.
The ask does not show the cut reply again: it would hold the room the rewrite needs.
"""

from __future__ import annotations

import logging

from raven.contracts.loop_hooks import HookDecision
from research_flow.gates.base import Gate, GateCtx
from research_flow.support.answer_text import closing_tag_bar, visible_answer

logger = logging.getLogger(__name__)

_STATE = "cut_report"
# The rewrite is asked to come in this far under the cut length.
_SHORTER = 0.75
_ASK = (
    "Your last reply was cut off at the output limit after about {chars} characters, so "
    "it stopped mid-way and the reader would get no ending and no sources. Write the whole "
    "reply again, complete, in at most about {target} characters: keep its sections and "
    "its sources, and say each point more briefly. Do not mention that it was cut."
)


class CutReportGate(Gate):
    """Ask again, shorter and without tools, for a reply cut at the output limit."""

    def __init__(self, max_asks: int = 2, closing_tag_required: bool = False) -> None:
        self._max_asks = max_asks
        self._closing_tag_required = closing_tag_required

    @property
    def name(self) -> str:
        return "CutReportGate"

    async def before_iteration(self, ctx: GateCtx) -> HookDecision:
        if (ctx.metadata.get(_STATE) or {}).get("asking"):
            return HookDecision(modified_tools=[], notes=["cut_report: tools withdrawn for the rewrite"])
        return HookDecision()

    async def after_iteration(self, ctx: GateCtx) -> HookDecision:
        state = ctx.metadata.setdefault(_STATE, {"asks": 0})
        state.pop("asking", None)
        response = ctx.response
        if getattr(response, "has_tool_calls", False):
            return HookDecision()
        if not (getattr(response, "finish_reason", "") == "length" or getattr(response, "truncated", False)):
            return HookDecision()
        reply = visible_answer(
            getattr(response, "content", None) or "",
            closing_tag_required=closing_tag_bar(
                self._closing_tag_required, getattr(response, "reasoning_content", None)
            ),
        )
        if not reply:
            return HookDecision()
        if state["asks"] >= self._max_asks:
            state["shipped_cut"] = True
            logger.warning(
                "cut-report: reply cut at the output limit again after %d ask(s); it ships cut", state["asks"]
            )
            return HookDecision(notes=["cut_report: still cut, ships as it is"])
        state["asks"] += 1
        state["asking"] = True
        state.setdefault("cut_chars", []).append(len(reply))
        logger.warning(
            "cut-report: %d-char reply cut at the output limit; asking for it again, shorter (%d/%d)",
            len(reply),
            state["asks"],
            self._max_asks,
        )
        ask = _ASK.format(chars=len(reply), target=int(len(reply) * _SHORTER))
        return HookDecision(
            rollback=True,
            # Synthetic: the persisted turn keeps the rewrite, not the ask.
            rollback_inject=[{"role": "user", "content": ask, "_recovery_synthetic": True}],
            notes=["cut_report: cut reply set aside, asked again shorter"],
        )


__all__ = ["CutReportGate"]
