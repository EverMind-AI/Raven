"""StrategyRegistry — chains TokenStrategy hooks around the agent's LLM call.

Agent loop calls:
    msgs, tools, model = await registry.before_llm_call(msgs, tools, model)
    response = await provider.chat_with_retry(...)
    await registry.after_llm_call(response_dict, usage_snapshot)

Ordering guarantees:
    - Strategies are invoked in registration order for both hooks.
    - ``before_llm_call`` failures propagate — a bad pre-process must not
      silently send a broken request.
    - ``after_llm_call`` failures are caught and logged — telemetry or
      budget errors must never abort the main loop.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from raven.token_wise.base import TokenStrategy, UsageSnapshot


class StrategyRegistry:
    """Holds an ordered list of TokenStrategy instances."""

    def __init__(self, strategies: list[TokenStrategy] | None = None):
        self._strategies: list[TokenStrategy] = list(strategies or [])

    # ---- Introspection ----

    @property
    def strategies(self) -> list[TokenStrategy]:
        """Return a copy of the strategy list (callers can't mutate internals)."""
        return list(self._strategies)

    def __len__(self) -> int:
        return len(self._strategies)

    def __bool__(self) -> bool:
        return bool(self._strategies)

    def get(self, name: str) -> TokenStrategy | None:
        """Return the first strategy with ``name``, or None."""
        for s in self._strategies:
            if s.name == name:
                return s
        return None

    def register(self, strategy: TokenStrategy, *, first: bool = False) -> None:
        """Add a strategy. Order matters — see class docstring.

        ``first=True`` inserts at the front so it runs before the others (e.g.
        a tool-list filter must run before CacheOptimizer marks the final tool
        with ``cache_control``); default appends to the end.
        """
        if first:
            self._strategies.insert(0, strategy)
        else:
            self._strategies.append(strategy)

    # ---- Hooks ----

    async def before_llm_call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, str]:
        """Run each strategy's before-call hook. Errors propagate."""
        for s in self._strategies:
            messages, tools, model = await s.before_llm_call(messages, tools, model)
        return messages, tools, model

    async def after_llm_call(self, response: dict[str, Any], usage: UsageSnapshot) -> None:
        """Run each strategy's after-call hook. Errors are swallowed + logged."""
        for s in self._strategies:
            try:
                await s.after_llm_call(response, usage)
            except Exception as e:
                logger.warning("TokenStrategy '{}' after_llm_call failed: {}", s.name, e)
