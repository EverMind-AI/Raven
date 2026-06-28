"""Tests for ActionExecutor exec_kind=tool / exec_kind=spawn.

Plus tests that TaskDiscoverer records the discovery_menu dispatch
into NudgeFeedbackTracker so adaptive tuning sees it."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry
from raven.proactive_engine.sentinel.executor.action_executor import ActionExecutor
from raven.proactive_engine.sentinel.feedback.tracker import NudgeFeedbackTracker
from raven.proactive_engine.sentinel.executor.dispatcher import NudgeDispatcher
from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore
from raven.proactive_engine.sentinel.predictor.task_discoverer import TaskDiscoverer
from raven.proactive_engine.sentinel.types import PendingDecision, TaskOption
from raven.memory_engine.consolidate.consolidator import MemoryStore


_NOW = datetime(2026, 5, 8, 9, 0)
_NOW_MS = int(_NOW.timestamp() * 1000)


def _option(
    *,
    exec_kind: str,
    exec_payload: dict | None = None,
    title: str = "test option",
) -> TaskOption:
    return TaskOption(
        id="opt_test", title=title, why="why",
        type="ad_hoc", exec_kind=exec_kind,
        exec_payload=exec_payload or {},
        created_at_ms=_NOW_MS,
    )


def _decision() -> PendingDecision:
    return PendingDecision(
        decision_id="dec_x", channel="feishu", to="ou_xxx",
        created_at_ms=_NOW_MS, ttl_min=60, options=[],
    )


# ── exec_kind=tool ────────────────────────────────────────────────────


class _StubTool(Tool):
    @property
    def name(self) -> str:
        return "noop"

    @property
    def description(self) -> str:
        return "test stub tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"x": {"type": "string"}}}

    async def execute(self, x: str = "default", **kwargs) -> str:
        return f"called with x={x}"


@pytest.mark.asyncio
async def test_tool_exec_calls_registered_tool():
    registry = ToolRegistry()
    registry.register(_StubTool())
    executor = ActionExecutor(
        tool_registry=registry, now_fn=lambda: _NOW,
    )
    option = _option(
        exec_kind="tool",
        exec_payload={"tool": "noop", "args": {"x": "hello"}},
    )
    result = await executor.execute(option, decision=_decision())
    assert result.status == "ok"
    assert result.exec_kind == "tool"
    assert "called with x=hello" in result.output_text
    assert any("called tool noop" in s for s in result.side_effects)


@pytest.mark.asyncio
async def test_tool_exec_no_registry_errors():
    executor = ActionExecutor(now_fn=lambda: _NOW)
    option = _option(exec_kind="tool",
                     exec_payload={"tool": "noop", "args": {}})
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "tool_registry" in result.error.lower()


@pytest.mark.asyncio
async def test_tool_exec_unknown_tool_errors():
    registry = ToolRegistry()
    registry.register(_StubTool())
    executor = ActionExecutor(
        tool_registry=registry, now_fn=lambda: _NOW,
    )
    option = _option(exec_kind="tool",
                     exec_payload={"tool": "missing", "args": {}})
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "not registered" in result.error.lower()


@pytest.mark.asyncio
async def test_tool_exec_missing_tool_field_errors():
    registry = ToolRegistry()
    registry.register(_StubTool())
    executor = ActionExecutor(
        tool_registry=registry, now_fn=lambda: _NOW,
    )
    option = _option(exec_kind="tool", exec_payload={"args": {}})
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "exec_payload.tool missing" in result.error


@pytest.mark.asyncio
async def test_tool_exec_args_must_be_dict():
    registry = ToolRegistry()
    registry.register(_StubTool())
    executor = ActionExecutor(
        tool_registry=registry, now_fn=lambda: _NOW,
    )
    option = _option(exec_kind="tool",
                     exec_payload={"tool": "noop", "args": "not a dict"})
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "object" in result.error.lower()


@pytest.mark.asyncio
async def test_tool_exec_surfaces_tool_error_in_output():
    """ToolRegistry.execute catches exceptions and returns them as
    error strings (not re-raised). ActionExecutor surfaces those
    verbatim to the user — same convention as the AgentLoop's tool
    loop."""
    registry = ToolRegistry()

    class _BoomTool(Tool):
        @property
        def name(self): return "boom"
        @property
        def description(self): return "always fails"
        @property
        def parameters(self): return {"type": "object"}
        async def execute(self, **kw):
            raise RuntimeError("kaboom")

    registry.register(_BoomTool())
    executor = ActionExecutor(
        tool_registry=registry, now_fn=lambda: _NOW,
    )
    option = _option(exec_kind="tool",
                     exec_payload={"tool": "boom", "args": {}})
    result = await executor.execute(option, decision=_decision())
    # ToolRegistry returns "Error executing boom: kaboom..." as the
    # output string; ActionExecutor reports status=ok with the error
    # text in output_text (same shape as the agent's normal tool
    # responses — caller decides whether to render as success/failure).
    assert result.status == "ok"
    assert "kaboom" in (result.output_text or "")


# ── exec_kind=spawn ───────────────────────────────────────────────────


class _StubSubagentManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def spawn(
        self, task: str, *, label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
    ) -> str:
        self.calls.append({
            "task": task, "label": label,
            "origin_channel": origin_channel,
            "origin_chat_id": origin_chat_id,
            "session_key": session_key,
        })
        return f"Subagent [{label or task[:30]}] started (id: stub-1234)."


@pytest.mark.asyncio
async def test_spawn_exec_calls_subagent_manager():
    sub = _StubSubagentManager()
    executor = ActionExecutor(
        subagent_manager=sub, now_fn=lambda: _NOW,
    )
    option = _option(
        exec_kind="spawn",
        exec_payload={"task_description": "research X benchmarks for me"},
        title="research X",
    )
    result = await executor.execute(option, decision=_decision())
    assert result.status == "ok"
    assert result.exec_kind == "spawn"
    assert "stub-1234" in result.output_text
    assert any("spawned subagent" in s for s in result.side_effects)
    assert len(sub.calls) == 1
    assert sub.calls[0]["task"] == "research X benchmarks for me"
    assert sub.calls[0]["session_key"] == "feishu:ou_xxx"


@pytest.mark.asyncio
async def test_spawn_exec_no_manager_errors():
    executor = ActionExecutor(now_fn=lambda: _NOW)
    option = _option(exec_kind="spawn",
                     exec_payload={"task_description": "x"})
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "subagent_manager" in result.error.lower()


@pytest.mark.asyncio
async def test_spawn_exec_missing_task_description_errors():
    sub = _StubSubagentManager()
    executor = ActionExecutor(
        subagent_manager=sub, now_fn=lambda: _NOW,
    )
    option = _option(exec_kind="spawn", exec_payload={})
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "task_description" in result.error


@pytest.mark.asyncio
async def test_spawn_exec_propagates_manager_exception():

    class _BoomManager:
        async def spawn(self, **kw):
            raise RuntimeError("subagent boom")

    executor = ActionExecutor(
        subagent_manager=_BoomManager(), now_fn=lambda: _NOW,
    )
    option = _option(exec_kind="spawn",
                     exec_payload={"task_description": "x"})
    result = await executor.execute(option, decision=_decision())
    assert result.status == "error"
    assert "subagent boom" in result.error


# ── TaskDiscoverer FeedbackTracker integration ────────────────────────


class _StubProvider:
    def __init__(self, options: list[dict]):
        args_str = json.dumps({"options": options})
        class _Call:
            arguments = args_str
        class _Resp:
            has_tool_calls = True
            tool_calls = [_Call()]
        self._resp = _Resp()

    async def chat_with_retry(self, **kw):
        return self._resp


@pytest.mark.asyncio
async def test_discoverer_records_dispatched_into_feedback(tmp_path: Path):
    workspace = tmp_path / "ws"
    (workspace / "memory").mkdir(parents=True)
    memory = MemoryStore(workspace)
    memory.write_long_term("## User Information\n- name: Alice")
    memory.append_history("[2026-05-08 06:30] morning routine")

    pending_store = PendingDecisionStore(tmp_path / "pending.json")
    feedback = NudgeFeedbackTracker(workspace / "sentinel_feedback.jsonl")

    dispatcher = NudgeDispatcher(now_fn=lambda: _NOW)
    dispatcher.set_post(AsyncMock())

    provider = _StubProvider([
        {
            "title": f"task {i}", "why": "why",
            "type": "ad_hoc", "exec_kind": "reply",
            "exec_payload": {"prompt": f"do task {i}"},
        }
        for i in range(3)
    ])

    disco = TaskDiscoverer(
        memory_store=memory,
        pending_store=pending_store,
        dispatcher=dispatcher,
        provider=provider,
        model="x",
        feedback=feedback,
        max_options=3,
        now_fn=lambda: _NOW,
    )

    decision = await disco.run(channel="feishu", to="ou_xxx")
    assert decision is not None

    counts = feedback.counts(since_days=7)
    assert counts.get("dispatched", 0) == 1
