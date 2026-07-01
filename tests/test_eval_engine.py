"""Contract tests for the Eval Engine.

Pins the three-hook scaffold:

- ``BeforeIterationHook``: token-budget gate — short-circuits when the
  per-iteration estimate exceeds ``config.max_iteration_tokens``.
- ``ToolAuditHook``: deterministic deny-list — short-circuits when a
  tool call in ``ctx.response.tool_calls`` is in the denylist.
- ``AfterIterationHook``: LLM-judge → MemoryEngine writeback.

Default config (``enabled=False``) is a hard requirement — all three
hooks must be no-ops in the default state, so simply instantiating
``EvalEngine()`` and dropping its ``hooks()`` into a ``CompositeHook``
must not change AgentLoop behavior.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.agent.hook.base import AgentHookContext
from raven.eval_engine import (
    AfterIterationHook,
    BeforeIterationHook,
    EvalEngine,
    EvalEngineConfig,
    JudgeVerdict,
    ToolAuditHook,
)
from raven.eval_engine.adapter.adapter import EvalAdapter
from raven.eval_engine.judge.judge import EvalJudge

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_with_messages():
    return AgentHookContext(
        session_key="cli:test",
        iteration=1,
        messages=[
            {"role": "user", "content": "summarize this"},
            {"role": "assistant", "content": "ok, summary: foo"},
        ],
    )


# ===========================================================================
# Default config — everything must be no-op
# ===========================================================================


class TestDefaultDisabledBehavior:
    def test_config_default_enabled_false(self):
        cfg = EvalEngineConfig()
        assert cfg.enabled is False

    def test_config_subflags_have_safe_defaults(self):
        cfg = EvalEngineConfig()
        # on_task_completion defaults True so that flipping just
        # `enabled=True` activates the most useful hook.
        assert cfg.on_task_completion is True
        # Expensive hooks default off.
        assert cfg.on_tool_audit is False
        assert cfg.on_iteration_gate is False

    async def test_before_iteration_is_noop_when_disabled(self, ctx_with_messages):
        hook = BeforeIterationHook(EvalEngineConfig())
        decision = await hook.before_iteration(ctx_with_messages)
        assert decision.short_circuit_result is None

    async def test_tool_audit_is_noop_when_disabled(self, ctx_with_messages):
        hook = ToolAuditHook(EvalEngineConfig(tool_denylist=["dangerous"]))
        # response has a denylisted tool call but enabled=False
        ctx_with_messages.response = {"tool_calls": [{"name": "dangerous", "arguments": {}}]}
        decision = await hook.before_execute_tools(ctx_with_messages)
        assert decision.short_circuit_result is None


# ===========================================================================
# BeforeIterationHook
# ===========================================================================


class TestBeforeIterationHook:
    async def test_within_budget_passes_through(self, ctx_with_messages):
        cfg = EvalEngineConfig(enabled=True, on_iteration_gate=True, max_iteration_tokens=1_000_000)
        hook = BeforeIterationHook(cfg)
        decision = await hook.before_iteration(ctx_with_messages)
        assert decision.short_circuit_result is None

    async def test_over_budget_short_circuits(self, ctx_with_messages):
        cfg = EvalEngineConfig(enabled=True, on_iteration_gate=True, max_iteration_tokens=1)
        hook = BeforeIterationHook(cfg)
        decision = await hook.before_iteration(ctx_with_messages)

        assert decision.short_circuit_result is not None
        assert "budget" in str(decision.short_circuit_result).lower()
        assert any("token_budget_exceeded" in n for n in decision.notes)

    async def test_empty_messages_is_passthrough(self):
        cfg = EvalEngineConfig(enabled=True, on_iteration_gate=True)
        hook = BeforeIterationHook(cfg)
        ctx = AgentHookContext(session_key="cli:test", messages=[])
        decision = await hook.before_iteration(ctx)
        assert decision.short_circuit_result is None

    async def test_gate_off_keeps_passthrough_even_with_huge_messages(self, ctx_with_messages):
        # enabled=True but gate-flag off
        cfg = EvalEngineConfig(enabled=True, on_iteration_gate=False, max_iteration_tokens=1)
        hook = BeforeIterationHook(cfg)
        decision = await hook.before_iteration(ctx_with_messages)
        assert decision.short_circuit_result is None

    def test_estimator_handles_unserializable_payload(self):
        # Object that defies json.dumps — fall back to 0 estimate
        # rather than crashing.
        cfg = EvalEngineConfig(enabled=True, on_iteration_gate=True)
        hook = BeforeIterationHook(cfg)
        weird = object()
        # default=str in the estimator catches this; should return some int
        result = hook._estimate_tokens([{"weird": weird}])
        assert isinstance(result, int)
        assert result >= 0


# ===========================================================================
# ToolAuditHook
# ===========================================================================


class TestToolAuditHook:
    async def test_no_denylist_means_passthrough(self, ctx_with_messages):
        cfg = EvalEngineConfig(enabled=True, on_tool_audit=True, tool_denylist=[])
        hook = ToolAuditHook(cfg)
        ctx_with_messages.response = {"tool_calls": [{"name": "anything", "arguments": {}}]}
        decision = await hook.before_execute_tools(ctx_with_messages)
        assert decision.short_circuit_result is None

    async def test_dict_style_denied_tool_short_circuits(self, ctx_with_messages):
        cfg = EvalEngineConfig(enabled=True, on_tool_audit=True, tool_denylist=["shell", "exec"])
        hook = ToolAuditHook(cfg)
        ctx_with_messages.response = {"tool_calls": [{"name": "shell", "arguments": {"cmd": "rm -rf /"}}]}
        decision = await hook.before_execute_tools(ctx_with_messages)
        assert decision.short_circuit_result is not None
        assert "shell" in decision.short_circuit_result

    async def test_object_style_tool_call_supported(self, ctx_with_messages):
        cfg = EvalEngineConfig(enabled=True, on_tool_audit=True, tool_denylist=["dangerous"])
        hook = ToolAuditHook(cfg)

        class FakeCall:
            name = "dangerous"

        ctx_with_messages.response = MagicMock(tool_calls=[FakeCall()])
        decision = await hook.before_execute_tools(ctx_with_messages)
        assert decision.short_circuit_result is not None

    async def test_function_nested_tool_name_supported(self, ctx_with_messages):
        # OpenAI-style nested name (function.name)
        cfg = EvalEngineConfig(enabled=True, on_tool_audit=True, tool_denylist=["banned"])
        hook = ToolAuditHook(cfg)
        ctx_with_messages.response = {"tool_calls": [{"function": {"name": "banned"}}]}
        decision = await hook.before_execute_tools(ctx_with_messages)
        assert decision.short_circuit_result is not None

    async def test_no_response_is_passthrough(self):
        cfg = EvalEngineConfig(enabled=True, on_tool_audit=True, tool_denylist=["banned"])
        hook = ToolAuditHook(cfg)
        ctx = AgentHookContext(session_key="cli:test")  # response=None
        decision = await hook.before_execute_tools(ctx)
        assert decision.short_circuit_result is None

    async def test_audit_off_with_denylisted_call_is_passthrough(self, ctx_with_messages):
        # enabled=True but on_tool_audit=False
        cfg = EvalEngineConfig(enabled=True, on_tool_audit=False, tool_denylist=["banned"])
        hook = ToolAuditHook(cfg)
        ctx_with_messages.response = {"tool_calls": [{"name": "banned"}]}
        decision = await hook.before_execute_tools(ctx_with_messages)
        assert decision.short_circuit_result is None


# ===========================================================================
# EvalJudge
# ===========================================================================


class TestEvalJudge:
    async def test_completed_verdict(self):
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(return_value=MagicMock(content="completed"))
        judge = EvalJudge(provider, model="haiku-test")
        verdict = await judge.judge("did you do X?", "yes, here is X")
        assert verdict is JudgeVerdict.completed

    async def test_failed_verdict(self):
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(return_value=MagicMock(content="Verdict: failed."))
        judge = EvalJudge(provider)
        assert await judge.judge("X?", "I couldn't do X") is JudgeVerdict.failed

    async def test_unknown_when_response_ambiguous(self):
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(return_value=MagicMock(content="hmmm, it's complicated"))
        judge = EvalJudge(provider)
        assert await judge.judge("X?", "...") is JudgeVerdict.unknown

    async def test_provider_exception_returns_unknown(self):
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("boom"))
        judge = EvalJudge(provider)
        assert await judge.judge("X?", "Y") is JudgeVerdict.unknown

    async def test_timeout_returns_unknown(self):
        async def slow(*args: Any, **kwargs: Any):
            import asyncio

            await asyncio.sleep(10)
            return MagicMock(content="completed")

        provider = MagicMock()
        provider.chat_with_retry = slow
        judge = EvalJudge(provider, timeout_seconds=0.05)
        assert await judge.judge("X?", "Y") is JudgeVerdict.unknown


# ===========================================================================
# EvalAdapter
# ===========================================================================


class TestEvalAdapter:
    def test_completed_writes_history_entry(self):
        memory = MagicMock()
        adapter = EvalAdapter(
            memory,
            now_fn=lambda: datetime(2026, 5, 13, 10, 0),
        )
        adapter.record_task_completion(
            JudgeVerdict.completed,
            user_goal="summarize report.pdf",
            session_key="cli:c1",
        )
        memory.append_history.assert_called_once()
        entry = memory.append_history.call_args[0][0]
        assert "eval verdict=completed" in entry
        assert "session=cli:c1" in entry
        assert "summarize report.pdf" in entry

    def test_unknown_is_noop(self):
        memory = MagicMock()
        adapter = EvalAdapter(memory)
        adapter.record_task_completion(JudgeVerdict.unknown, user_goal="x", session_key="cli:c")
        memory.append_history.assert_not_called()

    def test_memory_exception_is_swallowed(self):
        memory = MagicMock()
        memory.append_history.side_effect = RuntimeError("io error")
        adapter = EvalAdapter(memory)
        # Should NOT raise.
        adapter.record_task_completion(JudgeVerdict.failed, user_goal="x", session_key="cli:c")

    def test_truncates_long_goal(self):
        memory = MagicMock()
        adapter = EvalAdapter(memory)
        long_goal = "x" * 500
        adapter.record_task_completion(JudgeVerdict.completed, user_goal=long_goal, session_key="cli:c")
        entry = memory.append_history.call_args[0][0]
        # Goal got truncated to <=160 chars within the quoted segment.
        quoted = entry.split('goal="', 1)[1].rsplit('"', 1)[0]
        assert len(quoted) <= 160


# ===========================================================================
# AfterIterationHook
# ===========================================================================


class TestAfterIterationHook:
    @pytest.fixture
    def cfg_enabled(self):
        return EvalEngineConfig(enabled=True, on_task_completion=True)

    @pytest.fixture
    def fake_judge(self):
        j = MagicMock()
        j.judge = AsyncMock(return_value=JudgeVerdict.completed)
        return j

    @pytest.fixture
    def adapter(self):
        return MagicMock()

    async def test_completed_writes_through_adapter(self, cfg_enabled, fake_judge, adapter, ctx_with_messages):
        hook = AfterIterationHook(cfg_enabled, fake_judge, adapter)
        decision = await hook.after_iteration(ctx_with_messages)
        adapter.record_task_completion.assert_called_once()
        kwargs = adapter.record_task_completion.call_args.kwargs
        assert kwargs["verdict"] is JudgeVerdict.completed
        assert "summarize" in kwargs["user_goal"]
        # Hook never short-circuits — evaluator must not interrupt the reply.
        assert decision.short_circuit_result is None

    async def test_unknown_verdict_skips_adapter(self, cfg_enabled, adapter, ctx_with_messages):
        judge = MagicMock()
        judge.judge = AsyncMock(return_value=JudgeVerdict.unknown)
        hook = AfterIterationHook(cfg_enabled, judge, adapter)
        await hook.after_iteration(ctx_with_messages)
        adapter.record_task_completion.assert_not_called()

    async def test_disabled_is_full_noop(self, fake_judge, adapter, ctx_with_messages):
        hook = AfterIterationHook(
            EvalEngineConfig(),  # disabled
            fake_judge,
            adapter,
        )
        await hook.after_iteration(ctx_with_messages)
        fake_judge.judge.assert_not_awaited()
        adapter.record_task_completion.assert_not_called()

    async def test_missing_user_or_assistant_is_noop(self, cfg_enabled, fake_judge, adapter):
        hook = AfterIterationHook(cfg_enabled, fake_judge, adapter)
        # No assistant message
        ctx = AgentHookContext(
            session_key="cli:test",
            messages=[{"role": "user", "content": "hi"}],
        )
        await hook.after_iteration(ctx)
        fake_judge.judge.assert_not_awaited()

    async def test_judge_exception_recovers_as_unknown(self, cfg_enabled, adapter, ctx_with_messages):
        judge = MagicMock()
        judge.judge = AsyncMock(side_effect=RuntimeError("boom"))
        hook = AfterIterationHook(cfg_enabled, judge, adapter)
        decision = await hook.after_iteration(ctx_with_messages)
        # No write through, no crash.
        adapter.record_task_completion.assert_not_called()
        assert decision.short_circuit_result is None


# ===========================================================================
# EvalEngine orchestrator
# ===========================================================================


class TestEvalEngineOrchestrator:
    def test_default_engine_yields_three_hooks(self):
        engine = EvalEngine()
        hooks = engine.hooks()
        assert len(hooks) == 3
        # Order is canonical: before_iteration → tool_audit → after_iteration
        assert isinstance(hooks[0], BeforeIterationHook)
        assert isinstance(hooks[1], ToolAuditHook)
        # When no MemoryEngine is wired, the after-iteration slot is a noop.
        # We just assert that *some* AgentHook subclass occupies it.
        from raven.agent.hook.base import AgentHook

        assert isinstance(hooks[2], AgentHook)

    def test_engine_with_memory_engine_uses_real_after_hook(self, tmp_path):
        memory = MagicMock()
        engine = EvalEngine(memory=memory)
        hooks = engine.hooks()
        assert isinstance(hooks[2], AfterIterationHook)

    def test_engine_exposes_config(self):
        cfg = EvalEngineConfig(enabled=True, judge_model="custom-model")
        engine = EvalEngine(cfg)
        assert engine.config is cfg
        assert engine.config.judge_model == "custom-model"

    async def test_default_engine_hooks_are_all_noops(self, ctx_with_messages):
        """The smoke contract — mount EvalEngine.hooks() into a chain
        with default config and AgentLoop behavior must be unchanged."""
        from raven.agent.hook import CompositeHook

        engine = EvalEngine()  # default config — disabled
        composite = CompositeHook(engine.hooks())

        for phase in (
            "before_iteration",
            "before_execute_tools",
            "after_iteration",
        ):
            decision = await getattr(composite, phase)(ctx_with_messages)
            assert decision.short_circuit_result is None
            assert decision.modified_content is None
