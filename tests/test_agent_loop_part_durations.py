"""A turn's parts carry how long they took, from the stream to a restored view.

How long the model thought and how long each tool ran were measured only in the
browser that watched the stream, so switching sessions or reloading the page
threw both numbers away -- the restored transcript had to either show nothing or
invent a clock that started at page load. These tests pin the two spans as
stored properties of the entries they belong to, and pin that an entry written
before they existed reads as *unknown* rather than as zero.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse, StreamDelta, ToolCallRequest
from raven.rpc.methods.session import _map_to_wire
from raven.rpc.models import TranscriptMessage
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

# Long enough that a real clock separates the deltas, short enough to stay a
# unit test. Assertions use half of it so a loaded machine cannot flake them.
GAP_S = 0.12
FLOOR_MS = int(GAP_S * 1000 / 2)


class ThinkingStreamProvider(LLMProvider):
    """Streams a thought, waits, then emits the scripted call or answer.

    One script entry per model call. Each is ``(thought, content, tool_calls)``;
    the wait between the thought and what follows is what the reasoning clock
    has to measure.
    """

    def __init__(self, script: list[tuple[str, str, list[ToolCallRequest]]]) -> None:
        super().__init__(api_key="test")
        self._script = list(script)

    async def chat_stream(self, **kwargs: Any):
        thought, content, tool_calls = self._script.pop(0)
        for piece in thought:
            yield StreamDelta(content=None, reasoning_content=piece)
        await asyncio.sleep(GAP_S)
        for call in tool_calls:
            yield StreamDelta(
                content=None,
                tool_call_delta={
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call.id,
                            "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                        }
                    ]
                },
            )
        if content:
            yield StreamDelta(content=content)
        yield StreamDelta(content=None, finish_reason="tool_calls" if tool_calls else "stop")

    async def chat(self, messages, tools=None, model=None, **kwargs: Any):
        return LLMResponse(content="unused", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _make_msg(text: str = "write it") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="chat1", sender_id="user", chat_type=ChatType.DM),
        text=text,
    )


def _persisted(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "sessions" / "tui" / "chat1.jsonl"
    assert path.exists(), "session file was not persisted"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in records if r.get("_type") != "metadata"]


async def _run_a_thinking_turn_with_a_slow_tool(workspace: Path) -> list[dict[str, Any]]:
    provider = ThinkingStreamProvider(
        [
            (
                "let me write the file",
                "",
                [ToolCallRequest(id="c1", name="write_file", arguments={"path": "a.txt", "content": "one\n"})],
            ),
            ("now to report", "done", []),
        ]
    )
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=3,
        restrict_to_workspace=True,
    )
    real_execute = agent.tools.execute

    async def slow_execute(name, arguments, **kwargs):
        await asyncio.sleep(GAP_S)
        return await real_execute(name, arguments, **kwargs)

    agent.tools.execute = slow_execute

    async def sink(_text: str) -> None:
        return None

    out = await agent._process_message(_make_msg(), on_token_delta=sink, on_reasoning_delta=sink)
    assert out is not None
    return _persisted(workspace)


@pytest.mark.asyncio
async def test_a_thought_is_stored_with_how_long_it_took(workspace):
    msgs = await _run_a_thinking_turn_with_a_slow_tool(workspace)
    thinking = [m for m in msgs if m.get("role") == "assistant" and m.get("reasoning_content")]
    assert thinking, f"no assistant entry carried a thought: {msgs}"
    for entry in thinking:
        assert "_reasoning_ms" not in entry, f"the in-flight private key was stored: {entry}"
        assert entry.get("reasoning_ms", 0) >= FLOOR_MS, f"thought span missing or too small: {entry}"


@pytest.mark.asyncio
async def test_a_tool_result_is_stored_with_how_long_the_call_ran(workspace):
    msgs = await _run_a_thinking_turn_with_a_slow_tool(workspace)
    tool_entry = next(m for m in msgs if m.get("role") == "tool")
    assert "_duration_ms" not in tool_entry, f"the in-flight private key was stored: {tool_entry}"
    assert tool_entry.get("duration_ms", 0) >= FLOOR_MS, f"call span missing or too small: {tool_entry}"


@pytest.mark.asyncio
async def test_both_spans_survive_a_restore(workspace):
    """The whole point: a resumed transcript shows the durations the live one did."""
    msgs = await _run_a_thinking_turn_with_a_slow_tool(workspace)
    wire = [TranscriptMessage.model_validate(e) for e in _map_to_wire(msgs, "tui:chat1")]

    thought = next(m for m in wire if m.role == "assistant" and m.reasoning_content)
    assert thought.reasoning_ms is not None and thought.reasoning_ms >= FLOOR_MS

    tool = next(m for m in wire if m.role == "tool")
    assert tool.duration_ms is not None and tool.duration_ms >= FLOOR_MS


def test_a_session_written_before_the_spans_existed_reads_as_unknown():
    """Absent must mean unknown, never zero.

    Zero would tell the reader the model thought for no time and the tool
    returned instantly -- a fabricated measurement, which is worse than the
    bare header a client draws when the field is missing.
    """
    stored = [
        {"role": "user", "content": "hi", "timestamp": "2026-01-01T00:00:00"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "thinking hard",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
            "timestamp": "2026-01-01T00:00:01",
        },
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "ok"},
    ]
    wire = _map_to_wire(stored, "tui:old")
    for entry in wire:
        assert "reasoning_ms" not in entry, f"invented a thought span: {entry}"
        assert "duration_ms" not in entry, f"invented a call span: {entry}"

    parsed = [TranscriptMessage.model_validate(e) for e in wire]
    assert all(m.reasoning_ms is None and m.duration_ms is None for m in parsed)
