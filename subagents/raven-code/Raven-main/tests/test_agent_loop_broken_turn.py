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
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=3,
        restrict_to_workspace=True,
    )
    # Redirected into the temporary workspace: this fork buckets sessions under
    # the instance data dir, so a test left alone writes into the developer's own
    # ``~/.raven``. Safe here because the manager reads the attribute per call and
    # nothing has been resolved through it yet.
    agent.sessions.sessions_dir = workspace / "sessions"
    return agent


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
async def test_work_after_an_in_turn_compaction_still_reaches_the_session(workspace, monkeypatch):
    """Compaction replaces the transcript, and the rescue must follow it.

    The loop hands the caller the list it started with and then appends to it
    for the rest of the turn, so the two in-turn compaction sites replace that
    list's CONTENTS. Rebinding instead cut the rescue off at the compaction:
    every tool call and result after it was missing from the stored turn, which
    is exactly the long turn most likely to be interrupted.
    """
    from raven.agent.loop import main as loop_main

    agent = _agent(
        workspace,
        DyingProvider(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="call-before", name="list_dir", arguments={"path": "."})],
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 900_000},
                ),
                LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="call-after", name="list_dir", arguments={"path": "."})],
                    finish_reason="tool_calls",
                ),
                asyncio.CancelledError(),
            ]
        ),
    )
    agent._compaction.auto = True
    monkeypatch.setattr(loop_main.compaction, "should_compact", lambda *a, **k: True)

    compactions = {"n": 0, "dropped": 0}

    async def summarize(messages, model, **kwargs):
        # What the summary tier returns: a SHORTER list. Length is the whole
        # point -- a replacement that kept it would leave the caller's
        # persistence watermark accidentally in range and prove nothing.
        compactions["n"] += 1
        compactions["dropped"] = max(0, len(messages) - 1)

        return [{"role": "user", "content": "[summary of the turn so far]"}], True

    monkeypatch.setattr(agent, "_compact_context", summarize)

    with pytest.raises(asyncio.CancelledError):
        await agent._process_message(_make_msg("a long one"))

    assert compactions["n"] > 0, "the compaction path never ran, so this proves nothing"
    assert compactions["dropped"] > 0, "the stand-in compaction did not actually shorten the list"

    msgs = _persisted(workspace)
    stored_calls = [c["id"] for m in msgs for c in (m.get("tool_calls") or [])]
    # Before: saved by the checkpoint the swap takes ahead of itself. After:
    # saved by the rescue, off a watermark the swap re-anchored onto the
    # shorter list. Counted, not just present -- the checkpoint could as easily
    # have written the transcript twice as not at all.
    assert stored_calls.count("call-before") == 1, f"call-before stored {stored_calls.count('call-before')} times"
    assert stored_calls.count("call-after") == 1, "work after the compaction was dropped from the stored turn"
    results = {str(m.get("tool_call_id")) for m in msgs if m.get("role") == "tool"}
    assert set(stored_calls) <= results, "every stored tool call must have a stored result"
    assert msgs[-1].get("turn_ended", {}).get("status") == "cancelled"


@pytest.mark.asyncio
async def test_finished_turn_keeps_prompt_after_real_history_compaction(workspace, monkeypatch):
    """After-turn consumers receive the request even when compaction summarizes it."""

    class CompactionProvider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.turn_calls = 0
            self.summary_calls = 0

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
            if not tools and any("compacting an agent" in str(m.get("content", "")) for m in messages):
                self.summary_calls += 1
                return LLMResponse(content="summary of the active turn", finish_reason="stop")
            self.turn_calls += 1
            if self.turn_calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="read-large", name="read_file", arguments={"path": "large.txt"})],
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 900_000, "completion_tokens": 10},
                )
            return LLMResponse(content="done", finish_reason="stop")

        def get_default_model(self) -> str:
            return "stub"

    (workspace / "large.txt").write_text("x" * 30_000)
    provider = CompactionProvider()
    agent = _agent(workspace, provider)
    session = agent.sessions.get_or_create("tui:chat1")
    for index in range(8):
        session.record({"role": "user", "content": f"old question {index}"})
        session.record({"role": "assistant", "content": f"old answer {index}"})
    agent.sessions.save(session)

    agent._compaction.auto = True
    after_turn: dict[str, Any] = {}
    backend_messages: list[dict[str, Any]] = []

    async def capture_after_turn(session_key, payload):
        after_turn.update(payload)

    async def capture_backend_store(session_key, messages):
        backend_messages.extend(messages)

    monkeypatch.setattr(agent.context_engine, "after_turn", capture_after_turn)
    monkeypatch.setattr(agent, "_dispatch_backend_store", capture_backend_store)

    out = await agent._process_message(_make_msg("current question"))

    assert out is not None
    assert provider.summary_calls > 0, "the real compaction summary path never ran"
    messages = after_turn["messages"]
    assert messages[0].get("role") == "user"
    assert str(messages[0].get("content")).endswith("\n\ncurrent question")
    assert messages[-1].get("role") == "assistant"
    assert messages[-1].get("content") == "done"
    assert backend_messages[0].get("role") == "user"
    assert str(backend_messages[0].get("content")).endswith("\n\ncurrent question")


@pytest.mark.asyncio
async def test_a_finished_turn_writes_no_marker(workspace):
    agent = _agent(workspace, DyingProvider([LLMResponse(content="done", finish_reason="stop")]))
    out = await agent._process_message(_make_msg("hello"))
    assert out is not None
    assert all("turn_ended" not in m for m in _persisted(workspace))
