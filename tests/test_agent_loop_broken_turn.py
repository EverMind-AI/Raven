"""A turn that dies still leaves its work in the session.

``_save_turn`` only ran on the happy path, so a cancelled or crashed turn lost
everything back to the question that started it -- the reader watched a page of
streamed work vanish on the next reload. These tests drive the real
``_process_message`` path with providers that die at chosen points and assert
what the JSONL keeps: the tail of the turn, a synthetic result for any tool
call left open, and one closing marker that says why the transcript stops.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class DyingProvider(LLMProvider):
    """Answers ``script`` in order; a BaseException entry is raised instead."""

    def __init__(self, script: list[Any]):
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
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _make_msg(content: str = "hello") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="chat1", sender_id="user", chat_type=ChatType.DM),
        text=content,
    )


def _agent(workspace: Path, provider: LLMProvider) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=3),
        tools=ToolWiring(restrict_to_workspace=True),
    )


def _persisted(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "sessions" / "tui" / "chat1.jsonl"
    assert path.exists(), "the broken turn was not persisted at all"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in records if r.get("_type") != "metadata"]


@pytest.mark.asyncio
async def test_a_cancelled_turn_keeps_its_question_and_says_who_ended_it(workspace):
    agent = _agent(workspace, DyingProvider([asyncio.CancelledError()]))
    with pytest.raises(asyncio.CancelledError):
        await agent._process_message(_make_msg("do the thing"))

    msgs = _persisted(workspace)
    assert any(m.get("role") == "user" and "do the thing" in str(m.get("content")) for m in msgs)
    marker = msgs[-1]
    assert marker.get("turn_ended", {}).get("status") == "cancelled"
    assert marker.get("timestamp"), "the marker closes the fold, so it needs the wall clock"


@pytest.mark.asyncio
async def test_a_crash_that_escapes_the_loop_leaves_a_failure_marker(workspace, monkeypatch):
    """A provider error is absorbed by the loop and becomes the answer; what
    this guards is the class that ESCAPES -- a bug past the provider try, a
    dying context engine -- which used to take the whole turn's record with it."""
    agent = _agent(workspace, DyingProvider([LLMResponse(content="unused", finish_reason="stop")]))

    async def explode(*args: Any, **kwargs: Any):
        raise RuntimeError("the engine room flooded")

    monkeypatch.setattr(agent, "_run_agent_loop", explode)
    with pytest.raises(RuntimeError):
        await agent._process_message(_make_msg("hello"))

    marker = _persisted(workspace)[-1]
    assert marker.get("turn_ended", {}).get("status") == "failed"
    assert "the engine room flooded" in marker["turn_ended"].get("reason", "")


@pytest.mark.asyncio
async def test_an_open_tool_call_is_closed_before_the_history_is_stored(workspace):
    """An assistant tool call with no result is a history strict providers
    reject on the NEXT turn, so the rescue closes it with an honest synthetic
    result rather than storing a shape that poisons the session."""
    agent = _agent(
        workspace,
        DyingProvider(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="call-a", name="list_dir", arguments={"path": "."})],
                    finish_reason="tool_calls",
                ),
                asyncio.CancelledError(),
            ]
        ),
    )
    with pytest.raises(asyncio.CancelledError):
        await agent._process_message(_make_msg("look around"))

    msgs = _persisted(workspace)
    assert msgs[-1].get("turn_ended", {}).get("status") == "cancelled"
    calls = [c["id"] for m in msgs for c in (m.get("tool_calls") or [])]
    results = {str(m.get("tool_call_id")) for m in msgs if m.get("role") == "tool"}
    assert set(calls) <= results, "every stored tool call must have a stored result"


@pytest.mark.asyncio
async def test_a_finished_turn_writes_no_marker(workspace):
    agent = _agent(workspace, DyingProvider([LLMResponse(content="done", finish_reason="stop")]))
    out = await agent._process_message(_make_msg("hello"))
    assert out is not None
    assert all("turn_ended" not in m for m in _persisted(workspace))
