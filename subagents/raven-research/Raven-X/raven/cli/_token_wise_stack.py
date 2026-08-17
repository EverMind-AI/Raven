"""Build a StrategyRegistry from a ``TokenWiseConfig``.

Called from ``cli/agent_commands.py`` when constructing ``AgentLoop``. Callers
do not need to know which individual strategies exist — this module is
the single place that translates config flags into a concrete registry.

Until 20260811 this function had **no callers at all**. The docstring named one
("typically ``cli/commands.py``") that was never written, ``PLAN.md`` §133 named
an ``install.py`` that does not exist, and nothing passed ``strategies=`` to
``AgentLoop``, so it fell through to the empty pass-through registry. The
``TokenWiseConfig`` defaults (``enabled``, ``cache_optimization``,
``max_cache_breakpoints=4``) were therefore read by no code on any path, and
``CacheOptimizer`` — complete, tested, and benchmarked against Hermes — never
ran. The only symptom was the bill: measured at 4.3x a comparable agent on the
same questions, with ``cached_tokens`` flat at the size of the system prompt.
A default-on capability whose activation site is never called has no functional
symptom, which is why the enumeration test in
``tests/test_cli_agent_loop_wiring.py`` asserts over construction sites rather
than over config defaults.

Ordering rationale (matches PLAN.md §2):
    SmartRouter → ToolResultLifecycle → SkillLazyLoader →
    CacheOptimizer → UsageTracker → BudgetAlerter

Step 1/2 only populate CacheOptimizer and UsageTracker. The rest are
wired in as they land (step 4, 5, 6).
"""

from __future__ import annotations

from collections.abc import Callable
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
    supports_caching: "Callable[[str], bool] | None" = None,
) -> StrategyRegistry:
    """Return a registry populated according to ``cfg``.

    If ``cfg`` is None or ``cfg.enabled`` is False, returns an empty registry
    (the agent loop treats this as a 100% pass-through).

    ``supports_caching`` should be the provider's own
    ``supports_prompt_caching`` bound method. Without it ``CacheOptimizer``
    falls back to a model-string lookup that cannot see the gateway.
    """
    if cfg is None or not cfg.enabled:
        return StrategyRegistry([])

    strategies: list[TokenStrategy] = []

    if cfg.cache_optimization:
        strategies.append(
            CacheOptimizer(
                max_breakpoints=cfg.max_cache_breakpoints,
                supports_caching=supports_caching,
            )
        )

    if cfg.usage_tracking:
        strategies.append(UsageTracker(telemetry_dir=telemetry_dir))

    return StrategyRegistry(strategies)
