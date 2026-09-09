"""The token-strategy paper: the hooks a TokenWise strategy implements around an LLM call.

Strategies are additive — multiple can be installed. The agent calls each
hook in registration order. A strategy that is not interested in a given
hook inherits the default no-op.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class UsageSnapshot:
    """Usage for one call. Input tokens are fresh (non-cached).
    Missing cache counts and costs are None; zero is explicitly reported."""

    model: str
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int = 0
    cost_usd: float | None = None
    # Populated by UsageTracker on aggregate snapshots.
    input_missing_calls: int = 0
    output_missing_calls: int = 0
    calls: int = 0
    cost_missing_calls: int = 0
    cache_read_missing_calls: int = 0
    cache_write_missing_calls: int = 0
    session_key: str | None = None
    root_session_key: str | None = None


class TokenStrategy(ABC):
    """Cross-cutting hooks for token and cost optimization.

    Strategies are additive — multiple can be installed. The agent calls each
    hook in registration order. A strategy that is not interested in a given
    hook inherits the default no-op.

    This is a single unified interface rather than four tiny ABCs to keep the
    install point simple. Concrete strategies will typically implement just
    one or two hooks.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy identifier (e.g. 'cache_optimizer', 'smart_router')."""

    async def before_llm_call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, str]:
        """Pre-process the outgoing request. Return (messages, tools, model).

        Used by CacheOptimizer (marks cache_control), SmartRouter (chooses
        model), ToolResultPruner (rewrites old tool output blocks).
        Default: pass through.
        """
        return messages, tools, model

    async def after_llm_call(
        self,
        response: dict[str, Any],
        usage: UsageSnapshot,
    ) -> None:
        """Post-call hook, for a strategy that accounts what a call used. Default: no-op."""


__all__ = ["TokenStrategy", "UsageSnapshot"]


__tier__ = "contract"
