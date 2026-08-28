"""Build a StrategyRegistry from a ``TokenWiseConfig``.

Called from the ``cli/*_commands.py`` modules when constructing ``AgentLoop``.
Callers do not need to know which individual strategies exist -- this module is
the single place that translates config flags into a concrete registry.

Before the construction sites below were wired, this function had **no callers
at all**. The docstring named one ("typically ``cli/commands.py``") that was
never written, and nothing passed ``strategies=`` to ``AgentLoop``, so every
surface fell through to the empty pass-through registry. The ``TokenWiseConfig``
defaults (``enabled``, ``cache_optimization``, ``max_cache_breakpoints=4``) were
therefore read by no code on any path, and ``CacheOptimizer`` -- complete,
tested and benchmarked -- never ran. A default-on capability whose activation
site is never called has no functional symptom; the only symptom available was
the bill.

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
    from raven.providers.base import LLMProvider


def caching_probe(default: "LLMProvider") -> Callable[[str], bool]:
    """Whether the running turn's provider may carry ``cache_control``.

    Not the construction-time provider: ``AgentLoop.provider`` is per session
    now, so a probe closed over the one the loop was built with answers for the
    default binding and for no other -- a session switched onto a caching model
    would be told it cannot cache. The strategies run inside ``use_binding``, so
    the active binding is the right thing to read there; outside a turn there is
    none, and the default is the only answer available.
    """

    def probe(model: str) -> bool:
        from raven.providers.binding import active_binding

        binding = active_binding()
        target = binding.provider if binding is not None else default
        return target.supports_prompt_caching(model)

    return probe


def install_from_config(
    cfg: "TokenWiseConfig | None",
    *,
    telemetry_dir: Path | None = None,
    supports_caching: Callable[[str], bool] | None = None,
) -> StrategyRegistry:
    """Return a registry populated according to ``cfg``.

    If ``cfg`` is None or ``cfg.enabled`` is False, returns an empty registry
    (the agent loop treats this as a 100% pass-through).

    ``supports_caching`` should be :func:`caching_probe` over the provider the
    loop is being built with. Without it ``CacheOptimizer`` falls back to a
    model-string lookup that cannot see the gateway.

    Leave ``telemetry_dir`` unset outside tests: ``UsageTracker``'s default is
    the one directory ``settings.usage`` reads, so rows written anywhere else
    are invisible to the only reader that aggregates them.
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
