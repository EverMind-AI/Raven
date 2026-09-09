"""Build a StrategyRegistry from a ``TokenWiseConfig``.

Callers (typically ``cli/commands.py`` when constructing ``AgentLoop``)
do not need to know which individual strategies exist — this module is
the single place that translates config flags into a concrete registry.

Ordering rationale (matches PLAN.md §2):
    SmartRouter → ToolResultLifecycle → SkillLazyLoader →
    CacheOptimizer → UsageTracker → BudgetAlerter

Step 1/2 only populate CacheOptimizer and UsageTracker. The rest are
wired in as they land (step 4, 5, 6).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from raven.token_wise.base import TokenStrategy
from raven.token_wise.cache_optimizer import CacheOptimizer
from raven.token_wise.registry import StrategyRegistry
from raven.token_wise.usage_tracker import UsageTracker

if TYPE_CHECKING:
    from raven.config.raven import TokenWiseConfig


def install_from_config(
    cfg: "TokenWiseConfig | None",
    *,
    telemetry_dir: Path | None = None,
) -> StrategyRegistry:
    """Return a registry populated according to ``cfg``.

    If ``cfg`` is None or ``cfg.enabled`` is False, returns an empty registry
    (the agent loop treats this as a 100% pass-through).
    """
    if cfg is None or not cfg.enabled:
        return StrategyRegistry([])

    strategies: list[TokenStrategy] = []

    if cfg.cache_optimization:
        strategies.append(CacheOptimizer(max_breakpoints=cfg.max_cache_breakpoints))

    if cfg.usage_tracking:
        strategies.append(UsageTracker(telemetry_dir=telemetry_dir))

    return StrategyRegistry(strategies)
