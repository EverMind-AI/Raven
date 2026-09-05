"""Budget visibility for the model (DR flow).

Appends a compact budget line to the newest tool result each iteration,
plus a one-shot converge warning past ``warn_ratio``. Persisted history
is the only train-serve-safe channel for this: the runtime-context block
is stripped on persist and TokenWise rewrites are wire-only, so anything
injected there would be invisible to training. The note is part of the
trajectory **by design** — its exact format is frozen with the flow
version, because trained models condition on it.
"""

from __future__ import annotations

import logging

from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision
from research_flow.support.budget import usage_tokens

logger = logging.getLogger(__name__)

_CONVERGE_WARNING = (
    "[budget warning: most of the budget is spent - stop opening new leads, "
    "verify what you have, and draft the final answer]"
)


class BudgetNoteObserver(AgentHook):
    """Make the remaining budget visible to the model, in-history."""

    def __init__(
        self,
        max_iterations: int,
        context_window_tokens: int = 0,
        warn_ratio: float = 0.8,
    ) -> None:
        self._max_iterations = max_iterations
        self._context_window_tokens = context_window_tokens
        self._warn_ratio = warn_ratio

    @property
    def name(self) -> str:
        return "BudgetNoteObserver"

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
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
        crossed_iter = bool(
            self._max_iterations and ctx.iteration and ctx.iteration / self._max_iterations >= self._warn_ratio
        )
        crossed_ctx = pct is not None and pct >= self._warn_ratio * 100
        if (crossed_iter or crossed_ctx) and not state.get("warned"):
            state["warned"] = True
            note = f"{note}\n{_CONVERGE_WARNING}"
            logger.info("budget-note: converge warning injected at iteration %s", ctx.iteration)

        # The note travels as ``append_note``: the loop lands it on the newest
        # tool result, a blank line between.
        return HookDecision(append_note=note)


__all__ = ["BudgetNoteObserver"]
