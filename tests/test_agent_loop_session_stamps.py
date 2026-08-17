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


@pytest.mark.asyncio
async def test_the_user_message_is_stamped_at_turn_start_not_turn_end(workspace):
    """The regression that made every restored fold read "1s".

    ``_save_turn`` runs after the turn completes, so left alone it stamps the
    user message and the final answer with the same clock read. A restored
    transcript derives the turn's duration from exactly that gap, so the user
    entry must carry the wall clock at which the message *arrived*.
    """
    from datetime import datetime, timedelta

    base = datetime(2026, 8, 13, 12, 0, 0)
    ticks = {"n": 0}

    def fake_now() -> datetime:
        ticks["n"] += 1
        return base + timedelta(seconds=ticks["n"])

    agent = AgentLoop(
        provider=StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        now_fn=fake_now,
    )
    out = await agent._process_message(_make_msg("hello"))
    assert out is not None

    msgs = _persisted_messages(workspace)
    user_ts = next(m["timestamp"] for m in msgs if m.get("role") == "user")
    answer_ts = next(m["timestamp"] for m in reversed(msgs) if m.get("role") == "assistant")
    assert user_ts < answer_ts, (
        f"user message must be stamped when it arrived, before the answer: {user_ts} !< {answer_ts}"
    )


class ScriptedProvider(LLMProvider):
    """Plays back a fixed list of responses, one per call."""

    def __init__(self, script):
        super().__init__(api_key="test")
        self._script = list(script)

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
        return self._script.pop(0)

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_file_tools_diff_is_stored_on_its_tool_entry(workspace):
    """The live tool event was the diff's only carrier, so a reloaded page
    could never number a change again. The stored tool entry keeps it -- under
    the plain key, with the in-flight private spelling gone."""
    from raven.providers.base import ToolCallRequest

    provider = ScriptedProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c1", name="write_file", arguments={"path": "a.txt", "content": "one\ntwo\n"})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=3,
        restrict_to_workspace=True,
    )
    out = await agent._process_message(_make_msg("write it"))
    assert out is not None

    msgs = _persisted_messages(workspace)
    tool_entry = next(m for m in msgs if m.get("role") == "tool")
    assert "_diff" not in tool_entry
    assert "+one" in (tool_entry.get("diff") or ""), f"stored tool entry carries no diff: {tool_entry}"
