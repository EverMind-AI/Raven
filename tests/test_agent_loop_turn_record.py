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

from raven.agent.hook import AgentHook, HookDecision
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, HostWiring, ToolWiring, TurnPolicy
from raven.agent.window import compaction, shrink
from raven.agent.window.turn_log import TURN_LOG_KEY, TurnLog
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.config.schema import CompactionConfig
from raven.context_engine.history_trimmer import HistoryTrimmer
from raven.contracts.tool import Tool
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


def _agent(workspace: Path, provider: LLMProvider, *, compact: bool = True, hooks: Any = None) -> AgentLoop:
    cfg = CompactionConfig(enabled=True, reserved_tokens=199_100, preserve_recent_tokens=300) if compact else None
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=12),
        tools=ToolWiring(restrict_to_workspace=True),
        host=HostWiring(hooks=hooks),
        engine=EngineWiring(
            # Wide enough that the history slot has room; the reserve puts the
            # in-turn trigger at 900 tokens.
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


def _call_rows() -> list[dict[str, Any]]:
    call = {"id": "c", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
    return [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c", "content": "r"},
        {"role": "assistant", "content": "done"},
    ]


def test_the_cut_keeps_the_system_message_the_opening_and_the_turns_rows():
    base = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "now"},
    ]
    log = TurnLog(base, len(base))
    base += _call_rows()

    assert [m["content"] for m in log.cut_to_turn(base)] == ["s", "now", "", "r", "done"]


def test_the_cut_restores_an_opening_a_head_summary_replaced_and_keeps_the_summary():
    """The summary stands for what it replaced, this turn's early rows and a message sent
    while it ran among them, so it stays beside the opening the log puts back."""
    base = [{"role": "system", "content": "s"}, {"role": "user", "content": "old"}, {"role": "user", "content": "now"}]
    log = TurnLog(base, len(base))
    base += _call_rows()
    log.follow(base)
    summary = {"role": "user", "content": f"{compaction.SUMMARY_MARKER}\n\nbrief"}
    window = [base[0], summary, *base[4:]]
    log.follow(window)

    assert [m["content"] for m in log.cut_to_turn(window)] == ["s", "now", summary["content"], "r", "done"]


def test_a_restored_opening_carries_no_picture_the_window_no_longer_holds():
    opening = {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }
    base = [{"role": "system", "content": "s"}, opening]
    log = TurnLog(base, len(base))
    base += _call_rows()
    log.follow(base)
    shrink.window_images(base, 0, reason="refused", any_role=True)
    log.follow(base)
    summary = {"role": "user", "content": f"{compaction.SUMMARY_MARKER}\n\nbrief"}
    window = [base[0], summary, *base[3:]]
    log.follow(window)

    cut = log.cut_to_turn(window)

    assert _pictures(cut) == 0
    assert "what is this?" in str(cut[1]["content"])
    assert _pictures([opening]) == 1, "the log's own record keeps the picture"


def test_a_restored_opening_keeps_the_form_the_window_last_sent():
    """A picture window withdrew the opening's picture and kept a newer one, then a head summary
    took the opening's place: the opening comes back as the window last held it, not with the
    picture the window had already given up."""
    opening = {
        "role": "user",
        "content": [
            {"type": "text", "text": "compare these"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }
    base = [{"role": "system", "content": "s"}, opening]
    log = TurnLog(base, len(base))
    base.append(
        {
            "role": "tool",
            "tool_call_id": "c",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}],
        }
    )
    log.follow(base)
    shrink.window_images(base, 1, any_role=True)
    log.follow(base)
    summary = {"role": "user", "content": f"{compaction.SUMMARY_MARKER}\n\nbrief"}
    window = [base[0], summary, *base[2:]]
    log.follow(window)

    cut = log.cut_to_turn(window)

    assert _pictures(cut) == 1
    assert "compare these" in str(cut[1]["content"])
    assert cut[2] is summary


def test_a_restored_opening_loses_a_picture_withdrawn_after_the_summary():
    """The other order: a head summary took the opening's place while it still carried its
    picture, and a refusal later withdrew every picture the window held. The log's last form
    of the opening still has one, so the cut takes it out."""
    opening = {
        "role": "user",
        "content": [
            {"type": "text", "text": "compare these"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }
    base = [{"role": "system", "content": "s"}, opening]
    log = TurnLog(base, len(base))
    base.append(
        {
            "role": "tool",
            "tool_call_id": "c",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}],
        }
    )
    log.follow(base)
    summary = {"role": "user", "content": f"{compaction.SUMMARY_MARKER}\n\nbrief"}
    window = [base[0], summary, *base[2:]]
    log.follow(window)
    shrink.window_images(window, 0, reason="refused", any_role=True)
    log.follow(window)

    cut = log.cut_to_turn(window)

    assert _pictures(cut) == 0
    assert "compare these" in str(cut[1]["content"])
    note = cut[1]["content"][1]["text"]
    assert "summary" in note and "refused" not in note, "nothing refused the opening's picture; a summary took it"


def test_a_rerun_logs_cut_holds_only_the_rerun():
    base = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    first = TurnLog(base, 2)
    base += [
        {"role": "assistant", "content": "first attempt's research"},
        {"role": "user", "content": "a correction", "_mid_turn_user": True},
    ]
    first.follow(base)
    kept = first.messages()
    seed = [dict(m) for m in (*kept[:2], kept[3])]
    second = TurnLog(seed, 2)
    seed.append({"role": "assistant", "content": "the rerun's answer"})

    assert [m["content"] for m in second.cut_to_turn(seed)] == ["s", "q", "a correction", "the rerun's answer"]


def _pictures(messages: list[dict[str, Any]]) -> int:
    return sum(
        1
        for m in messages
        if isinstance(m.get("content"), list)
        for part in m["content"]
        if isinstance(part, dict) and part.get("type") == "image_url"
    )


def test_the_cut_keeps_the_windows_copy_of_an_opening_whose_picture_was_withdrawn():
    """An endpoint that refused the opening's picture refuses it again if the cut puts it back."""
    opening = {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }
    base = [{"role": "system", "content": "s"}, opening]
    log = TurnLog(base, len(base))
    base.append({"role": "assistant", "content": "a cat"})
    log.follow(base)
    shrink.window_images(base, 0, reason="refused", any_role=True)
    log.follow(base)

    cut = log.cut_to_turn(base)

    assert _pictures(cut) == 0
    assert cut[1] is base[1], "the window's own copy of the opening is what goes"
    assert [m["role"] for m in cut] == ["system", "user", "assistant"]


def test_the_cut_keeps_an_opening_withdrawn_while_a_later_picture_stays():
    """A picture window that kept only the newest picture: the opening's stays withdrawn
    rather than coming back beside the one the window still carries."""
    opening = {
        "role": "user",
        "content": [
            {"type": "text", "text": "compare these"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }
    base = [{"role": "system", "content": "s"}, opening]
    log = TurnLog(base, len(base))
    base.append(
        {
            "role": "tool",
            "tool_call_id": "c",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}],
        }
    )
    log.follow(base)
    shrink.window_images(base, 1, any_role=True)
    log.follow(base)

    cut = log.cut_to_turn(base)

    assert _pictures(cut) == 1
    assert cut[1] is base[1]


def test_a_window_with_no_row_of_the_turn_is_sent_whole():
    base = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    log = TurnLog(base, len(base))
    other = [{"role": "system", "content": "s"}, {"role": "user", "content": "elsewhere"}]

    assert log.cut_to_turn(other) == other


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


_HUGE_BODY = "\n".join(["x" * 100] * 50)


class _HugeTool(Tool):
    name = "huge_tool"
    description = "Return a long deterministic result."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return _HUGE_BODY


@pytest.mark.asyncio
async def test_a_tool_result_cut_to_the_cap_is_the_same_bytes_on_the_wire_and_in_the_log(workspace):
    """The next turn's prefix cache only reuses what this turn sent.

    Cutting only the session write leaves the model holding the full result and
    the replay holding the cut one; the bytes diverge at the cap and everything
    after it is re-prefilled. The cap has to land before the model reads it, and
    the full body is filed beside the preview so the tail is still reachable.
    """
    from raven.config.raven import ContextConfig
    from raven.session.refs import TOOL_RESULT_TRUNCATION_MARKER, is_truncated
    from raven.session.tool_output import tool_output_dir

    provider = _ScriptProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="huge_tool", arguments={})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
            LLMResponse(content="second", finish_reason="stop"),
        ]
    )
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=4),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(context_config=ContextConfig(tool_result_max_bytes=1_000, tool_result_max_chars=0)),
    )
    agent.tools.register(_HugeTool())

    await agent._process_message(_turn("go"), session_key=_KEY)
    await agent._process_message(_turn("again"), session_key=_KEY)

    sent = [m for m in provider.main_calls[1] if m.get("role") == "tool"][-1]
    replayed = [m for m in provider.main_calls[2] if m.get("role") == "tool"][-1]
    saved = [m for m in _reloaded(agent) if m.get("role") == "tool"][-1]
    assert sent["content"] == replayed["content"]
    assert sent["content"] == saved["content"]
    assert is_truncated(saved["content"])
    assert TOOL_RESULT_TRUNCATION_MARKER in saved["content"]
    assert "[END UNTRUSTED" in saved["content"]
    assert "x" * 2_000 not in saved["content"]
    assert "Full output saved to:" in saved["content"]

    files = sorted(tool_output_dir(workspace).glob("tool_*.txt"))
    assert len(files) == 1
    assert str(files[0].resolve()) in saved["content"]
    assert files[0].read_text(encoding="utf-8") == _HUGE_BODY


_SIXTY_K = "x" * 60_000


class _SixtyKTool(Tool):
    """Returns a body larger than the model-facing budget, on every call."""

    name = "sixty_k"
    description = "Return a long deterministic result."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return _SIXTY_K


class _HangingSixtyKTool(_SixtyKTool):
    """Answers the first call, then hangs -- a turn cancelled mid-call."""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **kwargs: Any) -> str:
        self.calls += 1
        if self.calls > 1:
            raise asyncio.CancelledError()
        return _SIXTY_K


def _big_step(i: int) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=f"c{i}", name="sixty_k", arguments={})],
        finish_reason="tool_calls",
    )


def _managed(workspace: Path) -> list[Path]:
    from raven.session.tool_output import tool_output_dir

    directory = tool_output_dir(workspace)
    return sorted(directory.glob("tool_*.txt")) if directory.is_dir() else []


def _tool_rows(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("role") == "tool"]


def _wire_body(message: dict[str, Any]) -> str:
    """The text one provider payload row carries, whether it is a str or a text-block list."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")


@pytest.mark.asyncio
async def test_a_cache_strategy_marks_the_preview_not_the_full_tool_body(workspace):
    """The projection has to run before the strategies, not after them.

    ``CacheOptimizer`` is on by default and rewrites the newest non-system
    messages' content from a str into a text-block list to hang a cache
    breakpoint on it. A projection that ran after that rewrite no longer
    recognised the row as a tool result: the last call carried the
    60,000-character body while the session filed the 51,704-character preview,
    and the next turn replayed bytes the previous call never sent. Every call
    has to carry the filed bytes, and what the strategy wraps has to be the
    bounded preview rather than the body.
    """
    from raven.token_wise.cache_optimizer import CacheOptimizer

    provider = _ScriptProvider(
        [_big_step(i) for i in range(4)]
        + [
            LLMResponse(content="done", finish_reason="stop"),
            LLMResponse(content="again", finish_reason="stop"),
        ]
    )
    agent = _agent(workspace, provider, compact=False)
    agent.tools.register(_SixtyKTool())
    # The real strategy, so the rewrite is the one production performs; only
    # the capability probe is pinned, because the test model is not a wire id.
    agent.strategies.register(CacheOptimizer(max_breakpoints=4, supports_caching=lambda _model: True))

    await agent._process_message(_turn("go"), session_key=_KEY)

    last_call = _tool_rows(provider.main_calls[-1])
    assert len(last_call) == 4
    assert isinstance(last_call[-1]["content"], list), "the cache strategy really rewrote the tail row"

    filed = {m["tool_call_id"]: _wire_body(m) for m in _tool_rows(_reloaded(agent))}
    assert len(filed) == 4
    assert all(len(body) < len(_SIXTY_K) for body in filed.values()), "the session files previews, not the 60 KB bodies"
    assert all("Full output saved to:" in body for body in filed.values())
    for call in provider.main_calls:
        for row in _tool_rows(call):
            assert _wire_body(row) == filed[row["tool_call_id"]], "every call carries the bytes the session files"

    files = _managed(workspace)
    assert len(files) == 4
    assert all(any(str(f.resolve()) in body for body in filed.values()) for f in files), "no managed file is orphaned"
    assert all(f.read_text(encoding="utf-8") == _SIXTY_K for f in files)

    # The next turn replays the bytes the last call sent, block list and all.
    await agent._process_message(_turn("again"), session_key=_KEY)
    replayed = {m["tool_call_id"]: _wire_body(m) for m in _tool_rows(provider.main_calls[-1])}
    assert replayed == filed


@pytest.mark.asyncio
async def test_a_body_a_shrink_elided_is_filed_as_the_preview_its_call_sent(workspace):
    """The record keeps a body the window elided; it must keep it bounded.

    Four over-budget results fill the window, the fifth call is refused as too
    long, and the emergency shrink replaces the oldest body with a placeholder.
    The model's last read is that placeholder, and the record still holds the
    body the placeholder stood in for -- so the persist has to project it, or it
    files the full body while every call sent a preview and the next replay
    starts on bytes no call ever read.
    """
    from raven.session.tool_output import TOOL_OUTPUT_PREFIX

    script: list[Any] = [_big_step(i) for i in range(4)]
    script.append(
        LLMResponse(content="This model's maximum context length (8192 tokens) was exceeded", finish_reason="error")
    )
    script.append(LLMResponse(content="answer after the elision", finish_reason="stop"))
    provider = _ScriptProvider(script)
    agent = _agent(workspace, provider, compact=False)
    agent.tools.register(_SixtyKTool())

    await agent._process_message(_turn("go"), session_key=_KEY)

    assert shrink.ELIDED_TOOL_OUTPUT in _texts(provider.main_calls[-1]), "the window really was elided"
    first_read = _tool_rows(provider.main_calls[1])[0]["content"]
    assert "Full output saved to:" in first_read, "the first read was the bounded preview"
    last_read = _tool_rows(provider.main_calls[-1])[0]["content"]
    assert last_read == shrink.ELIDED_TOOL_OUTPUT, "the model's last read of that row was the placeholder"

    reloaded = _reloaded(agent)
    filed = _tool_rows(reloaded)
    _no_window_artifacts(reloaded)
    assert len(filed) == 4
    assert filed[0]["content"] == first_read, "the elided body is filed as the preview its calls sent"
    assert len(filed[0]["content"]) < len(_SIXTY_K), "the full body is not what the session files"
    sent = {m["content"] for call in provider.main_calls for m in _tool_rows(call)}
    assert all(m["content"] in sent for m in filed), "every filed tool body is a body a call sent"

    files = _managed(workspace)
    assert len(files) == 4
    assert all(any(str(f.resolve()) in m["content"] for m in filed) for f in files), "no managed file is orphaned"
    assert all(f.name.startswith(TOOL_OUTPUT_PREFIX) for f in files)


def _four_big_step() -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=f"c{i}", name="sixty_k", arguments={}) for i in range(4)],
        finish_reason="tool_calls",
        usage=dict(_HUGE_USAGE),
    )


@pytest.mark.asyncio
async def test_a_body_shrunk_before_its_first_call_is_filed_as_the_stand_in(workspace):
    """A fresh result the proactive pass elides is filed as the stand-in it was sent as.

    Four over-budget results arrive in one step. The next iteration's proactive
    pass keeps the newest three and replaces the oldest with the placeholder
    before any call projects it. The record still holds the full body, so filing
    its preview would store bytes no call read and name a managed file no call
    used; the session has to keep the stand-in that call sent instead.
    """
    from raven.session.tool_output import TOOL_OUTPUT_PREFIX

    provider = _ScriptProvider(
        [
            _four_big_step(),
            LLMResponse(content="done", finish_reason="stop"),
            LLMResponse(content="again", finish_reason="stop"),
        ]
    )
    agent = _agent(workspace, provider)
    agent.tools.register(_SixtyKTool())

    await agent._process_message(_turn("go"), session_key=_KEY)

    reads = _tool_rows(provider.main_calls[1])
    assert [m["tool_call_id"] for m in reads] == ["c0", "c1", "c2", "c3"]
    assert reads[0]["content"] == shrink.ELIDED_TOOL_OUTPUT, "the first result was elided before its first call"
    assert all("Full output saved to:" in m["content"] for m in reads[1:]), "the kept rows were sent as previews"

    reloaded = _reloaded(agent)
    filed = _tool_rows(reloaded)
    assert [m["tool_call_id"] for m in filed] == ["c0", "c1", "c2", "c3"]
    assert not any(TURN_LOG_KEY in m for m in reloaded)
    assert HistoryTrimmer.structural_errors(reloaded) == []
    assert filed[0]["content"] == shrink.ELIDED_TOOL_OUTPUT, "the elided row is filed as the stand-in its call sent"
    sent = {m["content"] for call in provider.main_calls for m in _tool_rows(call)}
    assert all(m["content"] in sent for m in filed), "every filed tool body is a body a call sent"
    assert [m["content"] for m in filed[1:]] == [m["content"] for m in reads[1:]]

    files = _managed(workspace)
    assert len(files) == 3, "the elided row writes no managed file"
    assert all(any(str(f.resolve()) in m["content"] for m in filed) for f in files), "no managed file is orphaned"
    assert all(f.name.startswith(TOOL_OUTPUT_PREFIX) for f in files)

    # The next turn replays the bytes the last call sent, the stand-in included.
    await agent._process_message(_turn("again"), session_key=_KEY)
    assert [m["content"] for m in _tool_rows(provider.main_calls[2])] == [m["content"] for m in filed]


@pytest.mark.asyncio
async def test_a_turn_cancelled_mid_call_files_the_preview_not_the_full_body(workspace):
    """A rescue has to project the record too, or a stop writes an orphan.

    The first result is over budget and its preview is written when the second
    call is prepared; that call never returns, so the turn is filed from the
    record. Filing the body whole both stores bytes no call read and leaves the
    managed file it already wrote unreferenced.
    """
    provider = _ScriptProvider([_big_step(0), _big_step(1)])
    agent = _agent(workspace, provider, compact=False)
    agent.tools.register(_HangingSixtyKTool())

    with pytest.raises(asyncio.CancelledError):
        await agent._process_message(_turn("go"), session_key=_KEY)

    first_read = _tool_rows(provider.main_calls[1])[0]["content"]
    assert "Full output saved to:" in first_read
    reloaded = _reloaded(agent)
    filed = _tool_rows(reloaded)
    _no_window_artifacts(reloaded)
    assert filed[0]["content"] == first_read, "the cancelled turn files what the model read"
    assert len(filed[0]["content"]) < len(_SIXTY_K)
    assert filed[1]["content"] == "[interrupted] this call never returned"
    assert reloaded[-1].get("turn_ended", {}).get("status") == "cancelled"

    files = _managed(workspace)
    assert len(files) == 1, "the preview file the first call wrote is the one the session names"
    assert str(files[0].resolve()) in filed[0]["content"]
    assert files[0].read_text(encoding="utf-8") == _SIXTY_K


class _CancelBeforeTheNextCall(AgentHook):
    """Stops the turn between a tool result landing and the call that would read it."""

    name = "cancel_before_the_next_call"

    def __init__(self) -> None:
        self.iterations = 0

    async def before_iteration(self, ctx):
        self.iterations += 1
        if self.iterations == 2:
            raise asyncio.CancelledError()
        return HookDecision()


@pytest.mark.asyncio
async def test_a_result_cancelled_before_its_first_call_is_filed_as_its_preview(workspace):
    """The third filing branch: a row no call ever carried.

    The 60 KB result lands, and the turn is stopped before the next call that
    would project it. No call sent the body and no window pass replaced it, so
    the record's only form for it is the projection itself. Filing the full body
    would store bytes no model read, and leave the managed file the bound wrote
    unreferenced.
    """
    provider = _ScriptProvider([_big_step(0)])
    agent = _agent(workspace, provider, compact=False, hooks=[_CancelBeforeTheNextCall()])
    agent.tools.register(_SixtyKTool())

    with pytest.raises(asyncio.CancelledError):
        await agent._process_message(_turn("go"), session_key=_KEY)

    assert _tool_rows(provider.main_calls[-1]) == [], "no call ever carried the result"

    reloaded = _reloaded(agent)
    filed = _tool_rows(reloaded)
    assert len(filed) == 1
    assert filed[0]["content"] != _SIXTY_K
    assert len(filed[0]["content"]) < len(_SIXTY_K)
    assert "Full output saved to:" in filed[0]["content"]
    assert reloaded[-1].get("turn_ended", {}).get("status") == "cancelled"

    files = _managed(workspace)
    assert len(files) == 1, "the bound wrote one file, and the filed row is what names it"
    assert str(files[0].resolve()) in filed[0]["content"]
    assert files[0].read_text(encoding="utf-8") == _SIXTY_K


@pytest.mark.asyncio
async def test_a_tool_body_already_in_history_is_replayed_as_stored(workspace):
    """Only this turn's results are projected; history is replayed byte for byte.

    Projecting a stored body would hand every later turn a preview under a fresh
    managed path, so the prefix would move each time and a replayed session
    would still disagree with its own record. A body already filed as a preview
    is already the bounded form, and one filed before the bound existed stays
    what the record says it is.
    """
    from raven.session.tool_output import ToolOutputLimits, bound_tool_result

    provider = _ScriptProvider([LLMResponse(content="done", finish_reason="stop")])
    agent = _agent(workspace, provider, compact=False)
    stored_preview = bound_tool_result("y" * 60_000, workspace, limits=ToolOutputLimits())
    assert "Full output saved to:" in stored_preview
    before = _managed(workspace)
    assert len(before) == 1

    session = agent.sessions.get_or_create(_KEY)
    session.record({"role": "user", "content": "the earlier question"})
    session.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "old1", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
                {"id": "old2", "type": "function", "function": {"name": "web_fetch", "arguments": "{}"}},
            ],
        }
    )
    session.record({"role": "tool", "tool_call_id": "old1", "name": "exec", "content": _SIXTY_K})
    session.record({"role": "tool", "tool_call_id": "old2", "name": "web_fetch", "content": stored_preview})
    session.record({"role": "assistant", "content": "the earlier answer"})
    agent.sessions.save(session)

    await agent._process_message(_turn("go"), session_key=_KEY)

    sent = {m["tool_call_id"]: m["content"] for m in _tool_rows(provider.main_calls[0])}
    assert sent["old1"] == _SIXTY_K, "a body already in history is sent as stored"
    assert sent["old2"] == stored_preview, "a stored preview is not bounded a second time"
    assert _managed(workspace) == before, "replaying history writes no new managed file"
    reloaded = {m["tool_call_id"]: m["content"] for m in _tool_rows(_reloaded(agent))}
    assert reloaded["old1"] == _SIXTY_K
    assert reloaded["old2"] == stored_preview


class _TwoBodyTool(Tool):
    """Answers with a different over-budget body on each call."""

    name = "two_body"
    description = "Return one long body, then another."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **kwargs: Any) -> str:
        self.calls += 1
        return ("a" if self.calls == 1 else "b") * 60_000


@pytest.mark.asyncio
async def test_two_results_sharing_a_tool_call_id_keep_their_own_previews(workspace):
    """One step can advertise two calls under one id; neither may evict the other.

    Keying the preview cache on the id alone made each result overwrite the
    other, so every call re-bounded both under fresh managed paths and the filed
    turn stopped matching the payload. The body is part of the key.
    """
    provider = _ScriptProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="same", name="two_body", arguments={}),
                    ToolCallRequest(id="same", name="two_body", arguments={}),
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    agent = _agent(workspace, provider, compact=False)
    agent.tools.register(_TwoBodyTool())

    await agent._process_message(_turn("go"), session_key=_KEY)

    sent = _tool_rows(provider.main_calls[-1])
    assert len(sent) == 2
    assert "a" * 1_000 in sent[0]["content"] and "b" * 1_000 not in sent[0]["content"]
    assert "b" * 1_000 in sent[1]["content"] and "a" * 1_000 not in sent[1]["content"]
    filed = _tool_rows(_reloaded(agent))
    assert [m["content"] for m in filed] == [m["content"] for m in sent]

    files = _managed(workspace)
    assert len(files) == 2, "one managed file per distinct body, not per call"
    bodies = {f.read_text(encoding="utf-8") for f in files}
    assert bodies == {"a" * 60_000, "b" * 60_000}
    assert all(any(str(f.resolve()) in m["content"] for m in filed) for f in files)


@pytest.mark.asyncio
async def test_a_history_row_reusing_a_call_id_is_not_projected(workspace):
    """The projection gate is the row's identity, not a bare ``tool_call_id``.

    History can carry a tool call under the same id a later turn advertises.
    Gating on the id made the stored row look like this turn's own, so the
    payload re-bounded it under a fresh managed path while the session kept the
    original bytes; the next replay then diverged at that row.
    """
    body = "h" * 60_000
    provider = _ScriptProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="dup", name="sixty_k", arguments={})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    agent = _agent(workspace, provider, compact=False)
    agent.tools.register(_SixtyKTool())

    session = agent.sessions.get_or_create(_KEY)
    session.record({"role": "user", "content": "the earlier question"})
    session.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "dup", "type": "function", "function": {"name": "sixty_k", "arguments": "{}"}}],
        }
    )
    session.record({"role": "tool", "tool_call_id": "dup", "name": "sixty_k", "content": body})
    session.record({"role": "assistant", "content": "the earlier answer"})
    agent.sessions.save(session)

    await agent._process_message(_turn("go"), session_key=_KEY)

    assert [m["content"] for m in _tool_rows(provider.main_calls[0])] == [body]
    last_read = _tool_rows(provider.main_calls[1])
    assert last_read[0]["content"] == body, "the history row is replayed as stored"
    assert "Full output saved to:" in last_read[1]["content"], "the new row is bounded"

    files = _managed(workspace)
    assert len(files) == 1, "reusing the id writes no file for the history row"

    reloaded = _tool_rows(_reloaded(agent))
    assert [m["content"] for m in reloaded] == [body, last_read[1]["content"]]
