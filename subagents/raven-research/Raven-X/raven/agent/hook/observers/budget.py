"""Budget watermark observer — pure observability.

Tracks how much of the turn's iteration and context budgets is spent
and tags/logs when a warning watermark is crossed. It never changes
loop behavior: surfacing the remaining budget to the *model* (runtime
context injection) changes the trajectory distribution, so that step
deliberately ships together with paired training data — this observer
only plumbs the state that change will need, and gives operators the
signal today.

State lives in ``ctx.metadata["budget"]``::

    {
        "iterations_warned_at": int,   # first iteration past the watermark
        "context_warned_at": int,      # tokens seen when first past watermark
        "max_context_used": int,       # high-water mark across the turn
    }
"""

from __future__ import annotations

import logging

from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision

logger = logging.getLogger(__name__)


def usage_tokens(response: object) -> int:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if not isinstance(usage, dict):
        return 0
    try:
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return prompt + completion


class BudgetObserver(AgentHook):
    """Warn (once each) when iteration count or context usage crosses
    ``warn_ratio`` of its budget. Never short-circuits, never rolls back."""

    def __init__(
        self,
        max_iterations: int = 0,
        context_window_tokens: int = 0,
        warn_ratio: float = 0.8,
    ) -> None:
        self._max_iterations = max_iterations
        self._context_window_tokens = context_window_tokens
        self._warn_ratio = warn_ratio

    @property
    def name(self) -> str:
        return "BudgetObserver"

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if not self._max_iterations or not ctx.iteration:
            return HookDecision()
        state = ctx.metadata.setdefault("budget", {})
        if "iterations_warned_at" in state:
            return HookDecision()
        if ctx.iteration / self._max_iterations < self._warn_ratio:
            return HookDecision()
        state["iterations_warned_at"] = ctx.iteration
        logger.warning(
            "budget: iteration %d/%d crossed the %.0f%% watermark",
            ctx.iteration,
            self._max_iterations,
            self._warn_ratio * 100,
        )
        return HookDecision(notes=[f"budget_iterations {ctx.iteration}/{self._max_iterations}"])

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        used = usage_tokens(ctx.response)
        if not used:
            return HookDecision()
        state = ctx.metadata.setdefault("budget", {})
        state["max_context_used"] = max(used, state.get("max_context_used", 0))
        if not self._context_window_tokens or "context_warned_at" in state:
            return HookDecision()
        if used / self._context_window_tokens < self._warn_ratio:
            return HookDecision()
        state["context_warned_at"] = used
        logger.warning(
            "budget: context usage %d/%d crossed the %.0f%% watermark",
            used,
            self._context_window_tokens,
            self._warn_ratio * 100,
        )
        return HookDecision(notes=[f"budget_context {used}/{self._context_window_tokens}"])


__all__ = ["BudgetObserver"]
