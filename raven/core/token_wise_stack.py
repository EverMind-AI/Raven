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
from typing import TYPE_CHECKING, Any, TypeVar

from loguru import logger

from raven.contracts.token_strategy import TokenStrategy
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


_T = TypeVar("_T")


def _setting(
    cfg: Any,
    name: str,
    default: _T,
    kind: type,
    *,
    allowed: tuple[Any, ...] = (),
    floor: int | None = None,
    ceiling: int | None = None,
) -> _T:
    """One field of the config block, or ``default`` if it cannot be used.

    Absent, of the wrong type, outside ``allowed``, or under ``floor`` all mean
    the same thing here: take the default and say so once. Checked rather than
    coerced, because coercion is how a value nobody wrote becomes a value the
    agent runs on -- ``int(some_object)`` succeeding says nothing about whether
    a number was meant. See the note in :func:`install_from_config` for why this
    layer forgives and the setters it feeds do not.
    """
    raw = getattr(cfg, name, default)
    # `bool` is an `int`, so an unguarded numeric check would accept `True` as a
    # breakpoint count of one.
    if not isinstance(raw, kind) or (kind is not bool and isinstance(raw, bool)):
        logger.warning("token_wise: {} is {!r}, not a {}; using {!r}", name, raw, kind.__name__, default)
        return default
    if allowed and raw not in allowed:
        logger.warning("token_wise: {} is {!r}, not one of {!r}; using {!r}", name, raw, allowed, default)
        return default
    if floor is not None and raw < floor:
        logger.warning("token_wise: {} is {!r}, below {!r}; using {!r}", name, raw, floor, default)
        return default
    if ceiling is not None and raw > ceiling:
        logger.warning("token_wise: {} is {!r}, above {!r}; using {!r}", name, raw, ceiling, default)
        return default
    return raw


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

    Also applies the one part of that config a registry cannot carry, because it
    is read where a request is built rather than where a turn is: the cache
    lifetime. Process-wide by necessity -- ``providers.pool`` rebuilds a
    provider from config on a model switch, so anything fixed at one
    construction is gone after the first one -- and set here because this is the
    one function that reads this config block.

    Breakpoint *ownership* is deliberately not set here. Suppressing the
    provider's own placement is the loop's job and it does it per turn, on the
    binding's provider. Anything this function could set would be wider than the
    requests the strategy it installs actually sees.

    Every value is read through :func:`_setting`, which falls back rather than
    raises. This is the seam a config file arrives at, and the fields here tune
    an optimisation: a value this function cannot make sense of has to cost the
    default, never the agent's ability to start. The setters it calls are strict
    for their own reasons -- an unrecognised cache ``ttl`` is accepted and billed
    upstream instead of refused -- and that strictness belongs one layer down
    from user input, not at it.

    Called for its effect as much as its return value, and safe to call again:
    the last call wins.

    Every value is read through :func:`_setting`, which falls back rather than
    raises. This is the seam a config file arrives at, and the fields here tune
    an optimisation: a value this function cannot make sense of has to cost the
    default, never the agent's ability to start. The setters it calls are strict
    for their own reasons -- an unrecognised cache ``ttl`` is accepted and billed
    upstream instead of refused -- and that strictness belongs one layer down
    from user input, not at it.
    """
    from raven.providers import prompt_cache

    if cfg is None or not _setting(cfg, "enabled", True, bool):
        prompt_cache.set_ttl(None)
        return StrategyRegistry([])

    ttl = _setting(cfg, "cache_ttl", "5m", str, allowed=("5m", "1h"))
    prompt_cache.set_ttl(None if ttl == "5m" else ttl)

    optimize = _setting(cfg, "cache_optimization", True, bool)

    strategies: list[TokenStrategy] = []

    if optimize:
        # Bounded above as well as below. The vendor refuses a fifth breakpoint
        # outright, and until this function had production callers the value
        # never reached a real request, so nothing had ever had to hold the
        # ceiling. `max_cache_breakpoints: 5` is now a refused request on every
        # call -- recoverable, since the provider retries without breakpoints,
        # but paid for once and silently un-optimised afterwards.
        breakpoints = _setting(
            cfg,
            "max_cache_breakpoints",
            prompt_cache.MAX_BREAKPOINTS,
            int,
            floor=1,
            ceiling=prompt_cache.MAX_BREAKPOINTS,
        )
        strategies.append(
            CacheOptimizer(
                max_breakpoints=breakpoints,
                supports_caching=supports_caching,
            )
        )

    if _setting(cfg, "usage_tracking", True, bool):
        strategies.append(UsageTracker(telemetry_dir=telemetry_dir))

    return StrategyRegistry(strategies)
