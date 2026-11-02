"""A turn is filed from what it said, not from the window its last call saw.

The in-turn window passes summarise the head, elide older tool bodies and
withdraw pictures so the next request fits. Filing the turn from that window
put the summary into the session as a user message, lost the stretch of the
turn the summary covered, and stored placeholders where the tool output was.
These drive the real ``_process_message`` path and read the session back.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.window import compaction, shrink
from raven.agent.window.turn_log import TURN_LOG_KEY, TurnLog
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.config.schema import CompactionConfig
from raven.context_engine.history_trimmer import HistoryTrimmer
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

_KEY = "test:c1"
_HUGE_USAGE = {"prompt_tokens": 4990, "completion_tokens": 10}
_BIG_ASSISTANT = "Working through the accumulated evidence. " * 200
_SUMMARY_TEXT = "Compacted handoff brief. " * 60


class _ScriptProvider(LLMProvider):
    """Scripted main-loop answers (an exception entry is raised); head-summary
    requests and calls made beside the turn are answered apart and never
    consume the script."""

    def __init__(self, script: list[Any]):
        super().__init__(api_key="test")
        self._script = list(script)
        self.summary_calls = 0
        self.main_calls: list[list[dict]] = []

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
        if messages and messages[0].get("content") == compaction.SUMMARY_INSTRUCTIONS:
            self.summary_calls += 1
            return LLMResponse(content=_SUMMARY_TEXT, finish_reason="stop")
        if tools is None:
            # A call from beside the turn (the session title, recall), not one of its iterations.
            return LLMResponse(content="aside", finish_reason="stop")
        self.main_calls.append([dict(m) for m in messages])
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


def _agent(workspace: Path, provider: LLMProvider, *, compact: bool = True) -> AgentLoop:
    cfg = CompactionConfig(enabled=True, reserved_tokens=199_100, preserve_recent_tokens=300) if compact else None
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=12),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            # Wide enough that the history slot has room and the Curator takes
            # its fast path; the reserve puts the in-turn trigger at 900 tokens.
            context_window_tokens=200_000,
            compaction_config=cfg,
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
        ),
    )


def _tool_step(i: int, usage: dict | None = None, content: str = "") -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=[ToolCallRequest(id=f"c{i}", name="no_such_tool", arguments={"n": i})],
        finish_reason="tool_calls",
        usage=dict(usage or {}),
    )


def _turn(text: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text=text,
    )


def _reloaded(agent: AgentLoop) -> list[dict[str, Any]]:
    agent.sessions.invalidate(_KEY)
    return list(agent.sessions.get_or_create(_KEY).messages)


def _texts(messages: list[dict[str, Any]]) -> list[str]:
    return [str(m.get("content") or "") for m in messages]


def _no_window_artifacts(messages: list[dict[str, Any]]) -> None:
    assert not any(t.startswith(compaction.SUMMARY_MARKER) for t in _texts(messages))
    assert shrink.ELIDED_TOOL_OUTPUT not in _texts(messages)
    assert not any(TURN_LOG_KEY in m for m in messages)
    assert HistoryTrimmer.structural_errors(messages) == []


def _seed_history(agent: AgentLoop) -> None:
    session = agent.sessions.get_or_create(_KEY)
    session.record({"role": "user", "content": "the earlier question"})
    session.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "s0", "type": "function", "function": {"name": "no_such_tool", "arguments": "{}"}}],
        }
    )
    session.record({"role": "tool", "tool_call_id": "s0", "name": "no_such_tool", "content": "earlier evidence " * 60})
    session.record({"role": "assistant", "content": "the earlier answer"})
    agent.sessions.save(session)


@pytest.mark.asyncio
async def test_a_head_summary_on_the_first_turn_is_not_filed(workspace):
    provider = _ScriptProvider(
        [
            _tool_step(1, _HUGE_USAGE, _BIG_ASSISTANT),
            _tool_step(2, _HUGE_USAGE, _BIG_ASSISTANT),
            LLMResponse(content="final answer", finish_reason="stop"),
        ]
    )
    agent = _agent(workspace, provider)

    await agent._process_message(_turn("the task statement"), session_key=_KEY)

    assert provider.summary_calls >= 1, "the turn has to have been summarised for this to test anything"
    assert any(t.startswith(compaction.SUMMARY_MARKER) for t in _texts(provider.main_calls[-1]))
    filed = _reloaded(agent)
    _no_window_artifacts(filed)
    assert [m["role"] for m in filed] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert [m.get("tool_call_id") for m in filed if m["role"] == "tool"] == ["c1", "c2"]
    assert filed[0]["content"] == "the task statement"
    assert filed[-1]["content"] == "final answer"


@pytest.mark.asyncio
async def test_a_summary_over_earlier_turns_leaves_the_session_whole(workspace):
    provider = _ScriptProvider(
        [
            _tool_step(1, _HUGE_USAGE, _BIG_ASSISTANT),
            _tool_step(2, _HUGE_USAGE, _BIG_ASSISTANT),
            LLMResponse(content="final answer", finish_reason="stop"),
        ]
    )
    agent = _agent(workspace, provider)
    _seed_history(agent)

    await agent._process_message(_turn("the follow-up"), session_key=_KEY)

    assert provider.summary_calls >= 1
    filed = _reloaded(agent)
    _no_window_artifacts(filed)
    assert filed[0]["content"] == "the earlier question"
    follow_up = _texts(filed).index("the follow-up")
    assert follow_up == 4
    assert [m["role"] for m in filed[follow_up:]] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_an_elided_tool_body_is_filed_as_the_tool_said_it(workspace):
    script: list[Any] = [_tool_step(i) for i in range(5)]
    script.append(
        LLMResponse(content="This model's maximum context length (8192 tokens) was exceeded", finish_reason="error")
    )
    script.append(LLMResponse(content="answer after the elision", finish_reason="stop"))
    provider = _ScriptProvider(script)
    agent = _agent(workspace, provider, compact=False)

    await agent._process_message(_turn("go"), session_key=_KEY)

    assert shrink.ELIDED_TOOL_OUTPUT in _texts(provider.main_calls[-1]), "the window really was elided"
    filed = _reloaded(agent)
    _no_window_artifacts(filed)
    results = [m for m in filed if m["role"] == "tool"]
    assert len(results) == 5
    assert all("no_such_tool" in r["content"] for r in results), "each is the tool's own answer"


@pytest.mark.asyncio
async def test_a_turn_cancelled_after_a_summary_keeps_what_came_after_it(workspace):
    provider = _ScriptProvider(
        [
            _tool_step(1, _HUGE_USAGE, _BIG_ASSISTANT),
            _tool_step(2, _HUGE_USAGE, _BIG_ASSISTANT),
            _tool_step(3),
            asyncio.CancelledError(),
        ]
    )
    agent = _agent(workspace, provider)

    with pytest.raises(asyncio.CancelledError):
        await agent._process_message(_turn("the task statement"), session_key=_KEY)

    assert provider.summary_calls >= 1
    filed = _reloaded(agent)
    _no_window_artifacts(filed)
    assert [m.get("tool_call_id") for m in filed if m["role"] == "tool"] == ["c1", "c2", "c3"]
    assert filed[-1].get("turn_ended", {}).get("status") == "cancelled"


def _picture(call_id: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": [
            {"type": "text", "text": f"render {call_id}"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }


def test_the_log_keeps_a_withdrawn_pictures_note_and_drops_what_the_window_made_up():
    base = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    log = TurnLog(base, len(base))
    window = base
    window.extend([_picture("a"), _picture("b"), {"role": "tool", "tool_call_id": "c", "content": "the body"}])
    log.follow(window)

    # The standing image window, in place: "a" loses its picture to a note.
    log.follow(window)
    shrink.window_images(window, 1)
    log.follow(window)
    made_up = {"role": "user", "content": f"{compaction.SUMMARY_MARKER}\n\nbrief"}
    window = [window[0], made_up, *window[2:]]
    log.follow(window)
    window.append({"role": "assistant", "content": "done"})

    turn = log.messages()[2:]
    assert [m.get("tool_call_id") for m in turn] == ["a", "b", "c", None]
    assert not any(p.get("type") == "image_url" for p in turn[0]["content"]), "a's note is what is kept"
    assert any(p.get("type") == "image_url" for p in turn[1]["content"])
    assert turn[3]["content"] == "done"
    assert not any(TURN_LOG_KEY in m for m in log.messages())


def test_an_elided_body_is_recorded_as_it_was():
    base = [{"role": "user", "content": "q"}]
    log = TurnLog(base, 1)
    for i in range(5):
        base.append({"role": "tool", "tool_call_id": f"t{i}", "content": f"body {i}"})
    log.follow(base)
    shrunk, elided = shrink.emergency_shrink(base)
    log.follow(shrunk)

    assert elided == 2
    assert [m["content"] for m in log.messages()[1:]] == [f"body {i}" for i in range(5)]


def test_a_rolled_back_message_leaves_the_record():
    base = [{"role": "user", "content": "q"}]
    log = TurnLog(base, 1)
    base.append({"role": "assistant", "content": "kept"})
    log.follow(base)
    base.append({"role": "assistant", "content": "a draft the hook sent back"})
    log.messages()

    log.drop(base[2:])
    del base[2:]
    base.append({"role": "assistant", "content": "the re-sample"})

    assert [m["content"] for m in log.messages()] == ["q", "kept", "the re-sample"]


def test_added_leaves_out_the_history_the_turn_opened_with():
    base = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier reply"},
        {"role": "user", "content": "q"},
    ]
    log = TurnLog(base, 3)
    base.append({"role": "assistant", "content": "this turn's reply"})
    log.follow(base)

    assert [m["content"] for m in log.messages()] == ["earlier", "earlier reply", "q", "this turn's reply"]
    assert [m["content"] for m in log.added()] == ["this turn's reply"]
    carried = [{"role": "user", "content": "q"}, {"role": "user", "content": "a correction", "_mid_turn_user": True}]
    assert [m["content"] for m in TurnLog(carried, 1).added()] == ["a correction"]


def test_a_rerun_seed_copied_from_another_attempt_is_recorded_afresh():
    base = [{"role": "user", "content": "q"}]
    first = TurnLog(base, 1)
    base.append({"role": "user", "content": "a correction", "_mid_turn_user": True})
    first.follow(base)

    seed = [dict(m) for m in first.messages()]
    second = TurnLog(seed, 1)

    assert [m["content"] for m in second.messages()] == ["q", "a correction"]


def _saved_tool_result(workspace: Path, body: str, **context: Any) -> str:
    from raven.config.raven import ContextConfig

    agent = AgentLoop(
        provider=_ScriptProvider([]),
        workspace=workspace,
        model="stub",
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(context_config=ContextConfig(**context)),
    )
    session = agent.sessions.get_or_create(_KEY)
    agent._save_turn(session, [{"role": "tool", "tool_call_id": "a", "name": "exec", "content": body}], 0)
    return session.messages[-1]["content"]


def test_a_tool_result_past_the_old_cap_is_filed_whole(workspace):
    body = "x" * 40_000
    assert _saved_tool_result(workspace, body) == body


def test_the_cap_is_the_configured_one_and_says_it_cut(workspace):
    from raven.session.refs import is_truncated

    saved = _saved_tool_result(workspace, "y" * 40_000, tool_result_max_chars=16_000)
    assert saved.startswith("y" * 16_000) and is_truncated(saved)


def test_a_cap_of_zero_files_everything(workspace):
    body = "z" * 1_200_000
    assert _saved_tool_result(workspace, body, tool_result_max_chars=0) == body
