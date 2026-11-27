"""The write step (DR flow): the report is written once the research is over, under its rule.

The report rule sits in the system prompt, and by the time a research turn writes, a
hundred thousand tokens of search listings and pages stand between the two. The model
then keeps the rules stated once in a fixed form and drops the ones that ask it to
judge its content's form. This gate makes the writing a step of its own: the first
draft a research turn produces marks the research as over and is set aside, and the
model is asked again with the report rule appended after the research and no tools
offered, so it writes from the evidence with the rule as the last thing it read.

Once per turn. What follows is the flow as it was: the reviewer reads the report the
write step produced, and a rejection reopens the research with the tools back; the
revision is written in the loop, the brief still in the transcript above it. A turn
that did no research - a plain answer, a clarify - is passed through, as is a draft on
an iteration that offered no web tools for another reason than the budget. A round the
budget note closed (``budgetNote.closeAfter``) withdraws them ahead of this gate, and
its draft is the research's end like any other: it is set aside and the report asked for.

The report's bold is the bold pass's, last in the chain (``gates.bold_pass``).
"""

from __future__ import annotations

import logging
import time

from raven.contracts.loop_hooks import HookDecision
from research_flow.gates.base import Gate, GateCtx
from research_flow.support.answer_text import closing_tag_bar, visible_answer
from research_flow.support.ledger import ledger_append

logger = logging.getLogger(__name__)

_LEDGER_OP = "write_phase"
_STATE = "write_phase"
_WEB_TOOLS = frozenset({"web_search", "web_fetch"})


class WritePhaseGate(Gate):
    """Set aside a research turn's first draft and ask for the report again, tools withdrawn."""

    def __init__(self, brief: str, closing_tag_required: bool = False) -> None:
        self._brief = brief
        self._closing_tag_required = closing_tag_required

    @property
    def name(self) -> str:
        return "WritePhaseGate"

    async def before_iteration(self, ctx: GateCtx) -> HookDecision:
        state = ctx.metadata.setdefault(_STATE, {})
        if state.get("writing"):
            return HookDecision(modified_tools=[], notes=["write_phase: tools withdrawn for the report"])
        if ctx.tools is not None:
            names = {(t.get("function") or {}).get("name") for t in ctx.tools if isinstance(t, dict)}
            # The budget note runs first and strips the web tools from ``ctx.tools`` once it
            # closes the research; those tools were withdrawn, not absent.
            closed = bool((ctx.metadata.get("budget_note") or {}).get("closed"))
            state["web_tools_offered"] = bool(names & _WEB_TOOLS) or closed
        return HookDecision()

    async def after_iteration(self, ctx: GateCtx) -> HookDecision:
        state = ctx.metadata.setdefault(_STATE, {})
        if state.pop("writing", False):
            state["written_chars"] = len(getattr(ctx.response, "content", None) or "")
            return HookDecision(notes=["write_phase: report written"])
        if state.get("asked") or getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        draft = self._visible(ctx)
        if not draft:
            return HookDecision()
        if not state.get("web_tools_offered", True):
            return HookDecision(notes=["write_phase: skipped, no web tools this iteration"])
        if not researched(ctx.messages or [], ctx.turn_base or 0):
            return HookDecision(notes=["write_phase: skipped, no research this turn"])
        state.update(asked=True, writing=True, draft_chars=len(draft))
        ledger_append(
            {
                "ts": time.time(),
                "op": _LEDGER_OP,
                "event": "asked",
                "iteration": ctx.iteration,
                "draft_chars": len(draft),
            }
        )
        logger.warning(
            "write-phase: research over at iteration %s; setting the %d-char draft aside and asking for the report",
            ctx.iteration,
            len(draft),
        )
        return HookDecision(
            rollback=True,
            # Synthetic: the persisted turn keeps the report, not the brief that asked for it.
            rollback_inject=[{"role": "user", "content": self._brief, "_recovery_synthetic": True}],
            notes=["write_phase: draft set aside, report asked for"],
        )

    def _visible(self, ctx: GateCtx) -> str:
        return visible_answer(
            getattr(ctx.response, "content", None) or "",
            closing_tag_required=closing_tag_bar(
                self._closing_tag_required, getattr(ctx.response, "reasoning_content", None)
            ),
        )


def researched(messages: list[dict], turn_base: int) -> bool:
    """Whether this turn called a web tool."""
    return any(
        isinstance(m, dict) and m.get("role") == "tool" and m.get("name") in _WEB_TOOLS for m in messages[turn_base:]
    )


__all__ = ["WritePhaseGate", "researched"]
