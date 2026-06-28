"""Tests for the MessageTool -> TUI route, now on the native run_turn.

The swap (message tool reply -> token stream) and its finally-restore live in
``AgentLoop.run_turn``; the synthetic tool.complete (Fix B) lives in
``TuiTurnRunner`` (raven/tui_rpc/spine.py). AC-1/AC-4 drive the real spine via
``build_tui`` against a real AgentLoop whose provider fires the message tool;
AC-2 drives run_turn directly for the restore variants; AC-3 stays at the
AgentLoop layer (unchanged).
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop, TurnOutcome
from raven.agent.tools.message import MessageTool
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.providers.base import LLMResponse, StreamDelta
from raven.spine import ChatType, Origin, Source, TurnRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class FakeEmitter:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, session_key: str, event: dict) -> None:
        self.emitted.append((session_key, event))

    def events(self) -> list[dict]:
        return [e for _k, e in self.emitted]

    def types(self) -> list[str]:
        return [e["type"] for _k, e in self.emitted]


class _MessageToolProvider:
    """chat_stream fires a tool call to the message tool, then (next call) ends.
    Drives the real run_turn message-tool path under stream=True."""

    def __init__(self, content="hi") -> None:
        self._content = content
        self._i = 0

    async def chat_stream(self, **kwargs):
        self._i += 1
        if self._i == 1:
            yield StreamDelta(content=None, tool_call_delta={
                "tool_calls": [{
                    "index": 0, "id": "m1",
                    "function": {"name": "message", "arguments": f'{{"content": "{self._content}"}}'},
                }],
            })
        # second call: no content, no tool -> the loop finishes

    def get_default_model(self) -> str:
        return "fake/model"


def _make_agent(workspace: Path, provider=None) -> AgentLoop:
    loop = AgentLoop(
        provider=provider or _MessageToolProvider(),
        workspace=workspace,
        model="fake/model",
        max_iterations=3,
    )

    async def _noop() -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


def _req(session_key: str = "tui:default") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
        text="hi",
        conversation=session_key,
    )


# --- AC-1: message tool reply reaches the UI as token.delta (via the real spine) ---


async def test_ac1_message_tool_content_routes_to_token_delta_event(workspace) -> None:
    from raven.tui_rpc.spine import build_tui

    loop = _make_agent(workspace, _MessageToolProvider(content="hi from tool"))
    emitter = FakeEmitter()
    scheduler, _hub, turn_ids, teardown = build_tui(loop, emitter)
    try:
        turn_ids["tui:default"] = "t1"
        await scheduler.submit(_req()).result()
    finally:
        await teardown()

    deltas = [e for e in emitter.events() if e["type"] == "token.delta"]
    assert deltas, f"expected a token.delta from the message tool; got {emitter.types()}"
    assert "hi from tool" in "".join(e["payload"]["text"] for e in deltas)


# --- AC-2: the per-turn callback swap is task-local — the lane runs each turn
# in its own task, so a turn's swap cannot leak to another turn ---


async def test_ac2_callback_isolated_per_turn(workspace) -> None:
    loop = _make_agent(workspace)
    message_tool = loop.tools.get("message")
    assert isinstance(message_tool, MessageTool)
    original = message_tool._cur().send_callback  # this task's baseline

    async def _noop_emit(_event) -> None:
        return None

    # Mimic the lane: run the turn in its own task. run_turn swaps the
    # message-tool callback turn-locally; it must not leak back to this task.
    await asyncio.create_task(
        loop.run_turn(_req(), _noop_emit, lambda: [], stream=True)
    )
    assert message_tool._cur().send_callback is original  # no cross-task leak


async def test_ac2_callback_isolated_even_on_error(workspace) -> None:
    class _BoomProvider:
        async def chat_stream(self, **kwargs):
            raise RuntimeError("simulated crash")
            yield  # pragma: no cover

        def get_default_model(self) -> str:
            return "fake/model"

    loop = _make_agent(workspace, _BoomProvider())
    message_tool = loop.tools.get("message")
    assert isinstance(message_tool, MessageTool)
    original = message_tool._cur().send_callback

    async def _noop_emit(_event) -> None:
        return None

    with pytest.raises(RuntimeError):
        await asyncio.create_task(
            loop.run_turn(_req(), _noop_emit, lambda: [], stream=True)
        )
    assert message_tool._cur().send_callback is original  # no leak even on error


# --- AC-4: synthetic tool.complete[message] before message.complete (via spine) ---


async def test_ac4_synthetic_tool_complete_before_message_complete(workspace) -> None:
    from raven.tui_rpc.spine import build_tui

    loop = _make_agent(workspace)
    emitter = FakeEmitter()
    scheduler, _hub, turn_ids, teardown = build_tui(loop, emitter)
    try:
        turn_ids["tui:default"] = "t-ac4"
        await scheduler.submit(_req()).result()
    finally:
        await teardown()

    types = emitter.types()
    tool_completes = [e for e in emitter.events() if e["type"] == "tool.complete"]
    assert len(tool_completes) == 1, f"expected one synthetic tool.complete; got {types}"
    payload = tool_completes[0]["payload"]
    assert payload["tool_call_id"] == "msg-t-ac4"
    assert payload["result_preview"] == "(message sent via tool)"
    assert payload["truncated"] is False
    # Fix B (runner) fires before the sink's message.complete.
    assert types.index("tool.complete") < types.index("message.complete")


async def test_ac4_no_synthetic_tool_complete_when_message_tool_unused(workspace) -> None:
    class _PlainProvider:
        async def chat_stream(self, **kwargs):
            yield StreamDelta(content="just text")

        def get_default_model(self) -> str:
            return "fake/model"

    from raven.tui_rpc.spine import build_tui

    loop = _make_agent(workspace, _PlainProvider())
    emitter = FakeEmitter()
    scheduler, _hub, turn_ids, teardown = build_tui(loop, emitter)
    try:
        turn_ids["tui:default"] = "t1"
        await scheduler.submit(_req()).result()
    finally:
        await teardown()

    assert [e for e in emitter.events() if e["type"] == "tool.complete"] == []


# --- AC-3: AgentLoop logs final_content on the silent-return path (unchanged) ---


def _make_inbound(content: str = "test prompt") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM,
        ),
        text=content,
    )


async def test_ac3_silent_return_logs_final_content_when_message_tool_used(workspace, monkeypatch) -> None:
    agent = _make_agent(workspace)
    message_tool = agent.tools.get("message")
    assert isinstance(message_tool, MessageTool)

    async def fake_run_agent_loop(*args, **kwargs):
        message_tool._turn.set(replace(message_tool._cur(), sent=True))
        return ("hello world via message tool", [], [], TurnOutcome())

    monkeypatch.setattr(agent, "_run_agent_loop", fake_run_agent_loop)

    captured: list[str] = []
    from loguru import logger
    sink_id = logger.add(
        lambda msg: captured.append(str(msg)),
        level="INFO",
        format="{message}",
    )
    try:
        result = await agent._process_message(_make_inbound())
    finally:
        logger.remove(sink_id)

    assert result is None, "silent-return path expected"
    fingerprint = [c for c in captured if "MessageTool sent in turn" in c]
    assert len(fingerprint) == 1, (
        f"expected exactly 1 'MessageTool sent in turn' log line; got {captured!r}"
    )
    assert "hello world via message tool" in fingerprint[0], (
        f"log must include final_content preview; got {fingerprint[0]!r}"
    )


async def test_ac3_no_log_when_final_content_empty(workspace, monkeypatch) -> None:
    """Empty final_content + _sent_in_turn=True must NOT log (avoid spam)."""
    agent = _make_agent(workspace)
    message_tool = agent.tools.get("message")

    async def fake_run_agent_loop(*args, **kwargs):
        message_tool._turn.set(replace(message_tool._cur(), sent=True))
        return ("", [], [], TurnOutcome())  # empty final_content

    monkeypatch.setattr(agent, "_run_agent_loop", fake_run_agent_loop)

    captured: list[str] = []
    from loguru import logger
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="INFO", format="{message}")
    try:
        result = await agent._process_message(_make_inbound())
    finally:
        logger.remove(sink_id)

    assert result is None
    assert not any("MessageTool sent in turn" in c for c in captured)
