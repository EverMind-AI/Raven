"""Report length, measured: a line on the tool result of every markdown write.

A research brief often sets a length ("2000-3000 characters"), and the model has no
way to count one. Across four product runs it estimated from ``write_file``'s size
line instead and stated the estimate as the body's length: 40% low on one run, 4%
low and under the brief's floor on the next. This observer measures the file the
iteration just wrote or edited and appends the counts to that tool result, so a
length check and any count the report states rest on a number rather than a guess.
"""

from __future__ import annotations

from raven.contracts.loop_hooks import HookDecision
from research_flow.gates.base import Gate, GateCtx
from research_flow.support.report_file import length_note, touched_markdown


class ReportLengthNote(Gate):
    """Append ``[report length: ...]`` after an iteration that changed a markdown file."""

    async def after_iteration(self, ctx: GateCtx) -> HookDecision:
        if not getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        messages = ctx.messages or []
        results: list[dict] = []
        for message in reversed(messages):
            if message.get("role") != "tool":
                break
            results.append(message)
        notes = [note for path in touched_markdown(reversed(results)) if (note := length_note(path))]
        return HookDecision(append_note="\n".join(notes)) if notes else HookDecision()


__all__ = ["ReportLengthNote"]
