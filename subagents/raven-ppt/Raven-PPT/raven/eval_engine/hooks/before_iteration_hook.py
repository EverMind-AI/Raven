"""Before-iteration token-budget / pruning gate.

Cheapest of the three hooks: zero LLM calls, just a rough token
estimate on the messages list. If the estimate exceeds
``config.max_iteration_tokens`` the hook short-circuits the
iteration with a synthetic "budget exhausted" response.

The estimator is intentionally crude — `len(json.dumps(msgs)) // 4` —
because (a) AgentLoop already has tighter budget logic elsewhere and
(b) the goal here is to prevent a runaway iteration loop, not to be
millisecond-accurate.
"""

from __future__ import annotations

import json
import logging

from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision
from raven.eval_engine.config import EvalEngineConfig

logger = logging.getLogger(__name__)


class BeforeIterationHook(AgentHook):
    """Token-budget / pruning gate.

    Pass-through unless ``config.enabled and config.on_iteration_gate``
    are both True. When active, computes a rough byte/4 estimate of
    ``ctx.messages`` length and short-circuits with a polite halt
    string if the budget is exceeded.
    """

    def __init__(self, config: EvalEngineConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "EvalBeforeIterationHook"

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if not (self._config.enabled and self._config.on_iteration_gate):
            return HookDecision()
        messages = ctx.messages or []
        if not messages:
            return HookDecision()

        estimate = self._estimate_tokens(messages)
        if estimate <= self._config.max_iteration_tokens:
            return HookDecision()

        logger.info(
            "EvalEngine before_iteration: token estimate %d > budget %d; halting iteration",
            estimate,
            self._config.max_iteration_tokens,
        )
        return HookDecision(
            short_circuit_result=(
                "I've hit the conversation token budget for this turn. "
                "Let me know if you'd like me to summarize or start fresh."
            ),
            notes=[f"token_budget_exceeded estimate={estimate}"],
        )

    @staticmethod
    def _estimate_tokens(messages: list[dict]) -> int:
        try:
            return len(json.dumps(messages, ensure_ascii=False, default=str)) // 4
        except Exception:  # noqa: BLE001 — fall through to no-op on estimator error
            return 0


__all__ = ["BeforeIterationHook"]
