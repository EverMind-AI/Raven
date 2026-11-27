"""The bold pass (DR flow): a researched report's bold, chosen anew when it does not hold.

Last in the chain, so it reads the report every other gate has let through - the one
the write step produced or a revision the reviewer accepted - and nothing after it can
send that report back. When too few body paragraphs bold a name or a figure, or one
bolds past the cap, or a bold is a clause, a side call names the spans and the report
ships with them in place of its own (``support.report_bold``). The text is not
rewritten: a failed call ships the report with the bold it was written with.

Once per turn, and only on a turn that searched or read the web.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from raven.contracts.loop_hooks import HookDecision
from research_flow.gates.base import Gate, GateCtx
from research_flow.gates.write_phase import researched
from research_flow.support.answer_text import closing_tag_bar, visible_answer
from research_flow.support.ledger import ledger_append
from research_flow.support.report_bold import check_bold, choose_bold

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider

logger = logging.getLogger(__name__)

_STATE = "bold_pass"


class BoldPassGate(Gate):
    """Choose a researched report's bold anew when its own falls short or runs over."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str | None = None,
        floor: float = 0.4,
        closing_tag_required: bool = False,
    ) -> None:
        self._provider = provider
        self._model = model
        self._floor = floor
        self._closing_tag_required = closing_tag_required

    @property
    def name(self) -> str:
        return "BoldPassGate"

    async def after_iteration(self, ctx: GateCtx) -> HookDecision:
        state = ctx.metadata.setdefault(_STATE, {})
        if state.get("done") or getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        report = visible_answer(
            getattr(ctx.response, "content", None) or "",
            closing_tag_required=closing_tag_bar(
                self._closing_tag_required, getattr(ctx.response, "reasoning_content", None)
            ),
        )
        if not report or not researched(ctx.messages or [], ctx.turn_base or 0):
            return HookDecision()
        state["done"] = True
        before = check_bold(report)
        row = {"ts": time.time(), "op": _STATE, "iteration": ctx.iteration, "before": before.__dict__}
        if not before.fails(self._floor):
            ledger_append({**row, "event": "kept"})
            return HookDecision(notes=["bold_pass: kept"])
        started = time.monotonic()
        bolded, record = await choose_bold(self._provider, report, model=self._model)
        after = check_bold(bolded) if bolded is not None else None
        ledger_append(
            {
                **row,
                "event": "chosen" if bolded is not None else "failed",
                "seconds": round(time.monotonic() - started, 1),
                "after": after.__dict__ if after is not None else None,
                **record,
            }
        )
        logger.warning(
            "bold-pass: %d of %d paragraphs held, %d dense, %d long; %s",
            before.held,
            before.total,
            before.dense,
            before.long,
            f"chosen anew: {after.held} held, {after.dense} dense, {after.long} long"
            if after is not None
            else f"kept ({record.get('reason')})",
        )
        if bolded is None:
            return HookDecision(notes=[f"bold_pass: kept, {record.get('reason')}"])
        return HookDecision(short_circuit_result=bolded, notes=["bold_pass: bold chosen anew"])


__all__ = ["BoldPassGate"]
