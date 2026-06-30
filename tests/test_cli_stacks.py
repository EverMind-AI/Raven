"""Tests for the CLI assembly helpers.

The three new stacks (``build_memory_stack`` / ``build_eval_stack`` /
``build_hooks_stack``) are optional composition helpers — AgentLoop's
own constructor still handles its assembly. These tests pin the
contract that:

1. The helpers return correctly-typed objects ready to plug into
   AgentLoop.
2. Default configs produce no-op outputs (mounting them into a hook
   chain does NOT change AgentLoop behavior).
3. The legacy token_wise ``install_from_config`` helper relocated to
   the CLI tier still constructs a valid registry.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.hook import AgentHook, CompositeHook
from raven.cli._eval_stack import build_eval_stack
from raven.cli._hooks_stack import build_hooks_stack
from raven.cli._token_wise_stack import install_from_config
from raven.eval_engine import EvalEngine, EvalEngineConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# ===========================================================================
# build_eval_stack
# ===========================================================================


class TestBuildEvalStack:
    def test_default_config_yields_disabled_engine(self):
        engine = build_eval_stack()
        assert isinstance(engine, EvalEngine)
        assert engine.config.enabled is False

    def test_explicit_config_is_passed_through(self):
        cfg = EvalEngineConfig(enabled=True, judge_model="custom-model")
        engine = build_eval_stack(config=cfg)
        assert engine.config is cfg
        assert engine.config.judge_model == "custom-model"

    def test_returns_three_hooks(self):
        engine = build_eval_stack()
        hooks = engine.hooks()
        assert len(hooks) == 3

    async def test_default_hooks_are_noops_when_mounted_into_chain(self):
        from raven.agent.hook.base import AgentHookContext

        engine = build_eval_stack()
        composite = CompositeHook(engine.hooks())
        ctx = AgentHookContext(session_key="cli:test", iteration=1, messages=[])

        for phase in (
            "before_iteration",
            "before_execute_tools",
            "after_iteration",
        ):
            decision = await getattr(composite, phase)(ctx)
            assert decision.short_circuit_result is None


# ===========================================================================
# build_hooks_stack
# ===========================================================================


class TestBuildHooksStack:
    def test_no_inputs_yields_empty_composite(self):
        chain = build_hooks_stack()
        assert isinstance(chain, CompositeHook)
        assert len(chain) == 0

    def test_eval_engine_hooks_added(self):
        engine = build_eval_stack()
        chain = build_hooks_stack(eval_engine=engine)
        # Default EvalEngine yields 3 hooks.
        assert len(chain) == 3

    def test_extra_hooks_appended_after_eval(self):
        class _Custom(AgentHook):
            @property
            def name(self) -> str:
                return "custom"

        engine = build_eval_stack()
        custom = _Custom()
        chain = build_hooks_stack(eval_engine=engine, extra_hooks=[custom])
        hooks = list(chain)
        assert len(hooks) == 4
        # Extra hook comes after eval engine's three.
        assert hooks[-1] is custom

    def test_extra_hooks_only(self):
        class _A(AgentHook):
            pass

        class _B(AgentHook):
            pass

        chain = build_hooks_stack(extra_hooks=[_A(), _B()])
        assert len(chain) == 2


# ===========================================================================
# install_from_config (TokenWise — relocated)
# ===========================================================================


class TestInstallFromConfig:
    """Verify the token_wise ``install_from_config`` helper still works
    after relocation to the CLI tier."""

    def test_returns_strategy_registry(self):
        from raven.config.raven import TokenWiseConfig
        from raven.token_wise import StrategyRegistry

        cfg = TokenWiseConfig()
        registry = install_from_config(cfg)
        assert isinstance(registry, StrategyRegistry)

    def test_disabled_config_yields_empty_registry(self):
        from raven.config.raven import TokenWiseConfig
        from raven.token_wise import StrategyRegistry

        cfg = TokenWiseConfig(enabled=False)
        registry = install_from_config(cfg)
        assert isinstance(registry, StrategyRegistry)
        # Disabled config → no strategies registered.
        assert len(registry) == 0

    def test_none_config_yields_empty_registry(self):
        from raven.token_wise import StrategyRegistry

        registry = install_from_config(None)
        assert isinstance(registry, StrategyRegistry)
        assert len(registry) == 0

    def test_module_path(self):
        """Pin the new canonical import path so future refactors
        catch any test still trying ``raven.token_wise.install``."""
        from raven.cli import _token_wise_stack

        assert hasattr(_token_wise_stack, "install_from_config")

    def test_old_module_path_is_gone(self):
        with pytest.raises(ModuleNotFoundError):
            import raven.token_wise.install  # noqa: F401


# ===========================================================================
# build_memory_stack — deleted in Phase B-3.
#
# The ``DefaultMemoryEngine`` facade + ``build_memory_stack`` helper
# went away when AgentLoop migrated to directly holding
# ``MemoryConsolidator`` (which owns its own ``MemoryStore``) +
# ``ContextBuilder.skills``. The previous test class here exercised
# that assembly; it has no current equivalent because there's nothing
# to assemble — the same wiring now lives inline in
# ``AgentLoop.__init__``.
# ===========================================================================
