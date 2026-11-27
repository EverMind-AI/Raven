"""Budget visibility for the model (DR flow).

Appends a compact budget line to the newest tool result each iteration,
plus a one-shot converge warning past ``warn_ratio``. With ``close_after`` set,
the warning is also a deadline: that many iterations after it, ``web_search`` and
``web_fetch`` are withdrawn for the rest of the turn, so the turn has to write.
A warning alone was read and ignored: one product run said "two more searches"
from the warning at iteration 48 to the iteration cap at 60. With ``reserve_tokens``
set, research also closes at once when the context leaves less than that for the
report and its revision, and a reviewer's evidence round does not reopen it: one run
warned at 80% of a 262k window, fetched for six more iterations, and wrote its report
into 11.9k tokens of room and every revision into 4k, each cut at the limit. Persisted history
is the only train-serve-safe channel for this: the runtime-context block
is stripped on persist and TokenWise rewrites are wire-only, so anything
injected there would be invisible to training. The note is part of the
trajectory **by design** — its exact format is frozen with the flow
version, because trained models condition on it.
"""

from __future__ import annotations

import logging

from raven.contracts.loop_hooks import HookDecision
from raven.utils.tokens import estimate_message_tokens
from research_flow.gates.base import Gate, GateCtx
from research_flow.support.budget import usage_tokens
from research_flow.support.evidence_round import EvidenceRound

logger = logging.getLogger(__name__)

_RESEARCH_TOOLS = frozenset({"web_search", "web_fetch"})

_CONVERGE_WARNING = (
    "[budget warning: most of the budget is spent - stop opening new leads, "
    "verify what you have, and draft the final answer]"
)
_CLOSE_WARNING = (
    "[budget warning: research closes {n} iterations from now - web_search and "
    "web_fetch will be withdrawn then, so start the final answer before that]"
)
_CLOSED_NOTE = (
    "[budget: research is closed - web_search and web_fetch are withdrawn for the "
    "rest of this turn. Write the final answer now from what you have gathered.]"
)
_FULL_NOTE = (
    "[budget: the context is nearly full - web_search and web_fetch are withdrawn for "
    "the rest of this turn, and the room left is for the answer. Write the final answer "
    "now from what you have gathered.]"
)


class BudgetNoteObserver(Gate):
    """Make the remaining budget visible to the model, in-history."""

    def __init__(
        self,
        max_iterations: int,
        context_window_tokens: int = 0,
        warn_ratio: float = 0.8,
        close_after: int | None = None,
        evidence_round: EvidenceRound | None = None,
        reserve_tokens: int | None = None,
    ) -> None:
        self._max_iterations = max_iterations
        self._context_window_tokens = context_window_tokens
        self._warn_ratio = warn_ratio
        self._close_after = close_after
        self._evidence_round = evidence_round
        self._reserve_tokens = reserve_tokens

    @property
    def name(self) -> str:
        return "BudgetNoteObserver"

    def _closed(self, state: dict) -> bool:
        # A reviewer's evidence round is a sanctioned search after a rejection, so it
        # reopens research for as long as it lasts - unless the context is full.
        if state.get("full"):
            return True
        return bool(state.get("closed")) and not (self._evidence_round is not None and self._evidence_round.active)

    async def before_iteration(self, ctx: GateCtx) -> HookDecision:
        state = ctx.metadata.get("budget_note") or {}
        if ctx.tools is None or not self._closed(state):
            return HookDecision()
        kept = [t for t in ctx.tools if (t.get("function") or {}).get("name") not in _RESEARCH_TOOLS]
        return HookDecision(modified_tools=kept) if len(kept) != len(ctx.tools) else HookDecision()

    async def before_execute_tools(self, ctx: GateCtx) -> HookDecision:
        state = ctx.metadata.get("budget_note") or {}
        if self._closed(state):
            calls = getattr(ctx.response, "tool_calls", None) or []
            named = sum(1 for call in calls if getattr(call, "name", None) in _RESEARCH_TOOLS)
            if named:
                # Withholding un-offers a tool, it does not unregister it: observation only.
                state["called_when_closed"] = state.get("called_when_closed", 0) + named
        return HookDecision()

    async def after_iteration(self, ctx: GateCtx) -> HookDecision:
        if not getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        messages = ctx.messages or []
        if not messages or messages[-1].get("role") != "tool":
            return HookDecision()

        parts = []
        if self._max_iterations and ctx.iteration:
            parts.append(f"iteration {ctx.iteration}/{self._max_iterations}")
        used = usage_tokens(ctx.response)
        pct = None
        if used and self._context_window_tokens:
            # The denominator is resolved by the CALLER, not here. Since dr@3.0
            # (the flow assembly passes in the resolved window) this holds the
            # window the turn actually runs on, not the configured default - on a
            # model the resolver knows, the two differ by up to 15x and this line
            # used to claim 110% of a window the turn was nowhere near.
            #
            # Said here because the repair is invisible from this file: nothing in
            # ``budget_note.py`` changed, so reading this line alone reproduces the
            # original diagnosis, and it has already been re-reported as unfixed once
            # from a tree where only this file was grepped. ``SpinEntryBreaker`` takes
            # the same injected value and gates control flow on it, so this is not a
            # display-only quantity.
            #
            # Still open, and NOT fixed by that change: the numerator. ``usage_tokens``
            # is prompt + completion, i.e. it counts the turn's own output as context
            # already consumed, while the loop's pre-call fit trims on prompt tokens
            # alone. Both this note and the spin breaker divide by it, so moving it is
            # a distribution change owed its own label.
            pct = round(100 * used / self._context_window_tokens)
            parts.append(f"context ~{pct}%")
        if not parts:
            return HookDecision()

        note = f"[budget: {' | '.join(parts)}]"
        state = ctx.metadata.setdefault("budget_note", {})
        if self._reserve_tokens and used and self._context_window_tokens and not state.get("full"):
            # ``used`` is the call before this round's results; they are in the next one.
            held = used + sum(estimate_message_tokens(m) for m in _this_rounds_results(messages))
            if self._context_window_tokens - held < self._reserve_tokens:
                state.update(full=True, closed=True, full_at=ctx.iteration or 0, held_tokens=held)
                state.setdefault("closed_at", ctx.iteration or 0)
                logger.warning(
                    "budget-note: ~%d of %d context tokens held, under %d left; research tools withdrawn at iteration %s",
                    held,
                    self._context_window_tokens,
                    self._reserve_tokens,
                    ctx.iteration,
                )
                return HookDecision(append_note=f"{note}\n{_FULL_NOTE}")
        crossed_iter = bool(
            self._max_iterations and ctx.iteration and ctx.iteration / self._max_iterations >= self._warn_ratio
        )
        crossed_ctx = pct is not None and pct >= self._warn_ratio * 100
        if (crossed_iter or crossed_ctx) and not state.get("warned"):
            state["warned"] = True
            state["warned_at"] = ctx.iteration or 0
            note = f"{note}\n{_CONVERGE_WARNING}"
            if self._close_after:
                note = f"{note}\n{_CLOSE_WARNING.format(n=self._close_after)}"
            logger.info("budget-note: converge warning injected at iteration %s", ctx.iteration)
        elif (
            self._close_after
            and state.get("warned")
            and not state.get("closed")
            and (ctx.iteration or 0) - int(state.get("warned_at") or 0) >= self._close_after
        ):
            state["closed"] = True
            state["closed_at"] = ctx.iteration or 0
            note = f"{note}\n{_CLOSED_NOTE}"
            logger.warning("budget-note: research tools withdrawn at iteration %s", ctx.iteration)

        # The note travels as ``append_note``: the loop lands it on the newest
        # tool result, a blank line between.
        return HookDecision(append_note=note)


def _this_rounds_results(messages: list[dict]) -> list[dict]:
    """The tool results after the last assistant message."""
    tail: list[dict] = []
    for message in reversed(messages):
        if message.get("role") != "tool":
            break
        tail.append(message)
    return tail


__all__ = ["BudgetNoteObserver"]
