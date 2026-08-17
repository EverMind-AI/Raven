"""TokenWise core abstractions — colocated with implementations.

Migrated from ``raven/core/interfaces.py``. The colocate-with-implementation
rule means strategies live next to the ABC they implement.

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
    """Token usage and cost for a single LLM call.

    Convention: ``input_tokens`` is *fresh* (non-cached) prompt tokens.
    Provider adapters normalize total/fresh divergence (some providers
    report total ``prompt_tokens`` including cache reads/writes;
    AgentLoop's ``_build_usage_snapshot`` subtracts when needed so this
    field has consistent semantics across providers).
    """

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: float = 0.0
    session_key: str | None = None


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
        """Post-call hook. Used by UsageTracker, BudgetAlerter. Default: no-op."""


__all__ = ["TokenStrategy", "UsageSnapshot"]
