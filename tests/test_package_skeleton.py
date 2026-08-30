"""Package skeleton smoke tests: the package imports and the paper shapes behave.

These tests pass on a fresh checkout with only Python stdlib and
pydantic + loguru installed. They do NOT require an LLM provider, a
configured workspace, or any external service.

Where the shapes live (the papers package):

    TokenStrategy / UsageSnapshot      raven.contracts.token_strategy
    AssembledContext / TokenBudget     raven.contracts.assembled

``raven.core`` is the assembly root; the tests below pin that it carries no
interface shapes of its own.
"""

from __future__ import annotations

import pytest

from raven import __version__
from raven.contracts.assembled import AssembledContext, TokenBudget
from raven.contracts.token_strategy import TokenStrategy, UsageSnapshot

# ---------------------------------------------------------------------------
# Package metadata
# ---------------------------------------------------------------------------


def test_package_imports():
    assert isinstance(__version__, str)
    assert __version__  # non-empty


def test_assembled_shapes_live_in_the_papers():
    from raven.contracts.assembled import AssembledContext as ReAssembled
    from raven.contracts.assembled import TokenBudget as ReBudget

    assert ReAssembled is AssembledContext
    assert ReBudget is TokenBudget


def test_raven_core_is_the_assembly_root_not_the_old_context_home():
    # raven.core was once a transitional home for AssembledContext/TokenBudget
    # (now in memory_engine) and was deleted with a tombstone here. The name is
    # re-founded as the assembly root (the *_stack builders). Keep the old
    # meaning dead: a holdout importing the context types from here must still
    # fail loudly, while the assembly stacks answer at their new address.
    import raven.core

    assert not hasattr(raven.core, "AssembledContext")
    assert not hasattr(raven.core, "TokenBudget")
    from raven.core.plugin_stack import build_plugin_registry  # noqa: F401


# ---------------------------------------------------------------------------
# Surviving ABC: TokenStrategy
#
# ContextEngine / Monitor / SkillHandler were removed — they had zero
# implementations and the design owners chose alternate routes
# (SkillService, Sentinel Planner, pending Curator). The TokenStrategy
# contract remains load-bearing (CacheOptimizer, UsageTracker,
# SystemAndTailCacheStrategy all implement it), so its abstractness is still
# part of the tier-1 contract.
# ---------------------------------------------------------------------------


def test_token_strategy_is_abstract():
    with pytest.raises(TypeError):
        TokenStrategy()  # type: ignore[abstract]


def test_minimal_token_strategy_subclass():
    class Noop(TokenStrategy):
        @property
        def name(self) -> str:
            return "noop"

    strat = Noop()
    assert strat.name == "noop"


# ---------------------------------------------------------------------------
# Surviving dataclass behavior
# ---------------------------------------------------------------------------


def test_token_budget_is_a_plain_shape():
    import dataclasses

    b = TokenBudget(
        context_length=100_000,
        reserved_output=8_000,
        reserved_tools=4_000,
        reserved_system=2_000,
        available_history=86_000,
    )
    assert dataclasses.is_dataclass(b)
    assert [f.name for f in dataclasses.fields(b)] == [
        "context_length",
        "reserved_output",
        "reserved_tools",
        "reserved_system",
        "available_history",
    ]
    assert b.available_history == 86_000


def test_assembled_context_defaults():
    ac = AssembledContext(messages=[{"role": "user", "content": "hi"}])
    assert ac.system_prompt_addition is None
    assert ac.include_indices is None
    assert ac.metadata == {}


def test_usage_snapshot():
    u = UsageSnapshot(
        model="claude-opus-4-6",
        input_tokens=10_000,
        output_tokens=500,
    )
    # None, not 0.0: no price has been established for this call yet, and a
    # plan-billed provider never establishes one.
    assert u.estimated_cost_usd is None
    assert u.session_key is None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_module_imports():
    # Lazy import — config depends on pydantic-settings which may not load
    # if the env is misconfigured. We still want the test to be informative.
    from raven.config import (
        ContextConfig,
        RavenConfig,
        SentinelConfig,
        SkillForgeConfig,
        TokenWiseConfig,
    )

    cfg = RavenConfig()
    assert isinstance(cfg.context, ContextConfig)
    assert isinstance(cfg.sentinel, SentinelConfig)
    assert isinstance(cfg.token_wise, TokenWiseConfig)
    assert isinstance(cfg.skill_forge, SkillForgeConfig)


def test_config_safe_defaults():
    from raven.config import RavenConfig

    cfg = RavenConfig()
    # Risky/novel auto-* features must default to OFF so a fresh install
    # behaves like vanilla raven; the baseline retrieval pipeline
    # (context engine, skill_forge retrieval/injection) defaults ON as of R8.
    assert cfg.sentinel.enabled is False
    assert cfg.skill_forge.enabled is True  # R8: retrieval/injection pipeline on by default
    assert cfg.skill_forge.auto_detect is False
    assert cfg.skill_forge.auto_evolve is False
    # Baseline memory/skill feature layer defaults ON: a
    # fresh install runs the everos memory backend, the SkillForgeRouter, and
    # empty-response recovery. Pinned so a future silent flip gets caught.
    assert cfg.memory.backend == "everos"
    assert cfg.skill_forge.router.enabled is True
    assert cfg.base.agents.defaults.empty_recovery_enabled is True
    # Safe/cheap defaults can be ON.
    assert cfg.token_wise.usage_tracking is True
    assert cfg.token_wise.cache_optimization is True


def test_config_camel_and_snake_keys():
    from raven.config import SentinelConfig

    # snake_case
    s1 = SentinelConfig(idle_threshold_seconds=600)
    # camelCase (as Pydantic parses from YAML/JSON)
    s2 = SentinelConfig.model_validate({"idleThresholdSeconds": 600})

    assert s1.idle_threshold_seconds == 600
    assert s2.idle_threshold_seconds == 600


def test_tick_interval_seconds_rejects_sub_minute_values():
    """Pydantic Field(ge=60) blocks foot-guns like 1-second ticks that
    would burn Planner LLM budget faster than new inbound arrives."""
    from pydantic import ValidationError

    from raven.config import SentinelConfig

    SentinelConfig(tick_interval_seconds=60)  # boundary value OK
    with pytest.raises(ValidationError):
        SentinelConfig(tick_interval_seconds=30)
    with pytest.raises(ValidationError):
        SentinelConfig(tick_interval_seconds=1)


if __name__ == "__main__":
    # Allow `python tests/test_package_skeleton.py` as a quick smoke run.
    pytest.main([__file__, "-v"])
