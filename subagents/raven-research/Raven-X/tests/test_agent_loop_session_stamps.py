"""Persisted session messages carry a wall-clock timestamp.

The real agent loop persists turns through ``AgentLoop._save_turn`` which
appends raw dicts via the ``Session.record`` choke point. These tests drive
the real loop path (stubbed LLM) and assert the JSONL lines on disk carry a
``timestamp`` and no longer carry the dropped per-message ``received_at`` /
``turn_id`` — pinning the simplified stamping contract at the level that
reproduces a real TUI/CLI turn.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.tools.base import Tool
from raven.providers.base import LLMProvider, LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class StubProvider(LLMProvider):
    """Always returns a fixed assistant message. No tool calls."""

    def __init__(self, content: str = "stub response"):
        super().__init__(api_key="test")
        self._content = content

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content=self._content, finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _make_agent(workspace: Path) -> AgentLoop:
    return AgentLoop(
        provider=StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
    )


def _make_msg(content: str = "hello") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel="tui",
            chat_id="chat1",
            sender_id="user",
            chat_type=ChatType.DM,
        ),
        text=content,
    )


def _persisted_messages(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "sessions" / "tui" / "chat1.jsonl"
    assert path.exists(), "session file was not persisted"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in records if r.get("_type") != "metadata"]


@pytest.mark.asyncio
async def test_persisted_messages_carry_timestamp_not_turn_fields(workspace):
    agent = _make_agent(workspace)
    out = await agent._process_message(_make_msg("hello"))
    assert out is not None

    msgs = _persisted_messages(workspace)
    roles = [m.get("role") for m in msgs]
    assert "user" in roles and "assistant" in roles

    for m in msgs:
        assert m.get("timestamp"), f"missing timestamp: {m}"
        assert "received_at" not in m, f"received_at should be dropped: {m}"
        assert "turn_id" not in m, f"turn_id should be dropped: {m}"


class _NoopTool(Tool):
    @property
    def name(self):
        return "noop"

    @property
    def description(self):
        return "stub"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "ok"


class _ToolOnceProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        from raven.providers.base import ToolCallRequest

        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="noop", arguments={})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="done", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_messages_carry_creation_time_not_save_time(workspace, monkeypatch):
    """Tool results and assistant messages are stamped when created
    (ContextBuilder), not when the turn finally saves — so trajectories keep
    real per-iteration wall clock instead of one end-of-turn time for all."""
    from datetime import datetime

    import raven.agent.context.builder as builder_mod

    frozen = datetime(2001, 2, 3, 4, 5, 6)

    class _FrozenDatetime:
        @staticmethod
        def now():
            return frozen

    monkeypatch.setattr(builder_mod, "datetime", _FrozenDatetime)

    agent = AgentLoop(
        provider=_ToolOnceProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=3,
        restrict_to_workspace=True,
    )
    agent.tools.register(_NoopTool())
    out = await agent._process_message(_make_msg("hello"))
    assert out is not None

    msgs = _persisted_messages(workspace)
    stamped = [m for m in msgs if m.get("role") in ("tool", "assistant")]
    assert stamped, "expected tool and assistant messages in the persisted turn"
    for m in stamped:
        assert m["timestamp"] == frozen.isoformat(), f"save-time stamp leaked into: {m}"


@pytest.mark.asyncio
async def test_persisted_messages_carry_flow_version(workspace):
    """Every persisted line is stamped with the harness build identity, so
    trajectories from different loop builds stay distinguishable."""
    from raven import __version__

    agent = _make_agent(workspace)
    out = await agent._process_message(_make_msg("hello"))
    assert out is not None

    msgs = _persisted_messages(workspace)
    assert msgs
    for m in msgs:
        assert m.get("flow_version") == f"raven-{__version__}", f"missing flow_version: {m}"


@pytest.mark.asyncio
async def test_assistant_messages_carry_finish_reason(workspace):
    """Each real assistant turn persists its LLM finish_reason, so the no-op
    turn (closed think, no tool_call) is distinguishable from a genuine stop
    offline. Wire-safe: never sent back to an API (not in the key allowlist)."""
    agent = AgentLoop(
        provider=_ToolOnceProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=3,
        restrict_to_workspace=True,
    )
    agent.tools.register(_NoopTool())
    out = await agent._process_message(_make_msg("hello"))
    assert out is not None

    msgs = _persisted_messages(workspace)
    asst = [m for m in msgs if m.get("role") == "assistant"]
    assert len(asst) == 2, f"expected tool-call turn + final turn, got {[m.get('content') for m in asst]}"
    assert asst[0].get("finish_reason") == "tool_calls"
    assert asst[1].get("finish_reason") == "stop"

    from raven.providers.litellm_provider import _ALLOWED_MSG_KEYS

    assert "finish_reason" not in _ALLOWED_MSG_KEYS, "finish_reason must stay wire-stripped"
