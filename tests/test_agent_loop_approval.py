from __future__ import annotations

import json

from raven.agent.loop import AgentLoop
from raven.agent.tools.shell import ExecTool
from raven.providers.base import LLMResponse, ToolCallRequest
from raven.sandbox import ExecResult, SandboxExecutor
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _Provider:
    def __init__(self) -> None:
        self.responses = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-a",
                        name="exec",
                        arguments={"command": "rm file.txt"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="The file was deleted.", finish_reason="stop"),
        ]

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "fake/model"


class _ParallelDeleteProvider(_Provider):
    def __init__(self) -> None:
        self.responses = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call-a",
                        name="exec",
                        arguments={"command": "rm file.txt"},
                    ),
                    ToolCallRequest(
                        id="call-b",
                        name="exec",
                        arguments={"command": "python3 -c \"import os; os.remove('file.txt')\""},
                    ),
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="The file was deleted.", finish_reason="stop"),
        ]


class _Executor(SandboxExecutor):
    def __init__(self) -> None:
        self.commands: list[str] = []

    @property
    def is_sandboxed(self) -> bool:
        return False

    async def exec(self, command: str, **kwargs) -> ExecResult:
        self.commands.append(command)
        return ExecResult(stdout="ok", stderr="", exit_code=0)


class _Responder:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.requests: list[dict] = []

    async def await_approval(self, **request) -> bool:
        self.requests.append(request)
        return self.answer


async def test_agent_loop_binds_tool_call_id_to_approval_request(tmp_path) -> None:
    agent = AgentLoop(provider=_Provider(), workspace=tmp_path, model="fake/model")
    executor = _Executor()
    responder = _Responder()
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))
    tool.start_approval_turn(responder, conversation_id="session-a", turn_id="turn-a")
    agent.tools.register(tool)

    result, _media = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(
                channel="tui",
                chat_id="default",
                sender_id="user",
                chat_type=ChatType.DM,
            ),
            text="delete file.txt",
            conversation="session-a",
        ),
        session_key="session-a",
    )

    assert result == "The file was deleted."
    assert responder.requests[0]["tool_call_id"] == "call-a"
    assert executor.commands == ["rm file.txt"]


async def test_agent_loop_stops_after_user_denies_delete(tmp_path) -> None:
    provider = _Provider()
    agent = AgentLoop(provider=provider, workspace=tmp_path, model="fake/model")
    executor = _Executor()
    responder = _Responder(answer=False)
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))
    tool.start_approval_turn(responder, conversation_id="session-a", turn_id="turn-a")
    agent.tools.register(tool)

    result, _media = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(
                channel="tui",
                chat_id="default",
                sender_id="user",
                chat_type=ChatType.DM,
            ),
            text="delete file.txt",
            conversation="session-a",
        ),
        session_key="session-a",
    )

    assert "no alternative method will be attempted" in result
    assert executor.commands == []
    assert len(provider.responses) == 1


async def test_agent_loop_skips_remaining_tool_calls_after_delete_denial(tmp_path) -> None:
    provider = _ParallelDeleteProvider()
    agent = AgentLoop(provider=provider, workspace=tmp_path, model="fake/model")
    executor = _Executor()
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))
    tool.start_approval_turn(
        _Responder(answer=False),
        conversation_id="session-a",
        turn_id="turn-a",
    )
    agent.tools.register(tool)

    result, _media = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(
                channel="tui",
                chat_id="default",
                sender_id="user",
                chat_type=ChatType.DM,
            ),
            text="delete file.txt",
            conversation="session-a",
        ),
        session_key="session-a",
    )

    assert "no alternative method will be attempted" in result
    assert executor.commands == []
    assert len(provider.responses) == 1


class _StreamingProvider(_Provider):
    """The delete-then-explain script, delivered through ``chat_stream``.

    Needed because passing ``on_token_delta`` puts the loop on its streaming
    path: with a provider that only answers ``chat_with_retry`` the call raises
    before reaching the block under test, and the "nothing entered the token
    stream" assertion would pass for the wrong reason.
    """

    async def chat_stream(self, **kwargs):
        from raven.providers.base import StreamDelta as _Delta

        response = self.responses.pop(0)
        if response.tool_calls:
            # The provider-side shape, not a convenient one: the accumulator
            # reads ``tool_calls[].function.{name,arguments}`` and ignores
            # anything else, so a flatter dict streams as an EMPTY response and
            # the tool never runs -- which is how this test first passed its
            # "nothing reached the token stream" assertion for the wrong reason.
            yield _Delta(
                content=None,
                tool_call_delta={
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call.id,
                            "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                        }
                        for index, call in enumerate(response.tool_calls)
                    ]
                },
            )
            yield _Delta(content=None, finish_reason="tool_calls")
            return
        yield _Delta(content=response.content)
        yield _Delta(content=None, finish_reason="stop")


async def test_a_denied_delete_reaches_the_client_as_a_notice_not_as_the_answer(tmp_path) -> None:
    """Runtime prose must not enter the model's own output buffer.

    Streamed as token deltas -- which is how it used to leave the loop -- the
    sentence lands in the buffer holding the model's answer, so a client renders
    it as the model speaking: run together with whatever was narrated just
    before, and wearing the answer's copy and branch actions. The notice carries
    the blocking tool's own first line as detail, because the canned sentence
    says an operation stopped and never which one.
    """
    from raven.spine.events import NoticeKind

    notices: list[tuple[NoticeKind, str]] = []
    deltas: list[str] = []

    async def on_notice(kind: NoticeKind, detail: str) -> None:
        notices.append((kind, detail))

    async def on_token_delta(text: str) -> None:
        deltas.append(text)

    agent = AgentLoop(provider=_StreamingProvider(), workspace=tmp_path, model="fake/model")
    tool = ExecTool(executor=_Executor(), working_dir=str(tmp_path))
    tool.start_approval_turn(_Responder(answer=False), conversation_id="session-a", turn_id="turn-a")
    agent.tools.register(tool)

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
            text="delete file.txt",
            conversation="session-a",
        ),
        session_key="session-a",
        on_notice=on_notice,
        on_token_delta=on_token_delta,
    )

    assert [kind for kind, _detail in notices] == [NoticeKind.ACTION_BLOCKED]
    assert notices[0][1], "the blocking tool's own first line must ride along as detail"
    assert deltas == [], "the runtime sentence must not enter the model's token stream"


async def test_a_surface_with_no_notice_outlet_still_says_something(tmp_path) -> None:
    """The fallback is the point: a channel that cannot draw a notice would
    otherwise end the turn in silence, and silence is the worse failure."""
    deltas: list[str] = []

    async def on_token_delta(text: str) -> None:
        deltas.append(text)

    agent = AgentLoop(provider=_StreamingProvider(), workspace=tmp_path, model="fake/model")
    tool = ExecTool(executor=_Executor(), working_dir=str(tmp_path))
    tool.start_approval_turn(_Responder(answer=False), conversation_id="session-a", turn_id="turn-a")
    agent.tools.register(tool)

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
            text="delete file.txt",
            conversation="session-a",
        ),
        session_key="session-a",
        on_token_delta=on_token_delta,
    )

    assert deltas and "no alternative method will be attempted" in deltas[0]


async def test_the_blocked_notice_reaches_the_spine_as_a_deliverable(tmp_path) -> None:
    """The other half of the same path: ``run_turn`` is the boundary that turns
    the loop's callback into a spine ``Notice``, and an outlet is what a client
    is actually attached to. Without this the callback could be wired and the
    event still never reach anyone."""
    from raven.spine.events import Notice, NoticeKind

    emitted: list = []

    async def emit(event) -> None:
        emitted.append(event)

    def drain() -> list:
        return []

    agent = AgentLoop(provider=_StreamingProvider(), workspace=tmp_path, model="fake/model")
    tool = ExecTool(executor=_Executor(), working_dir=str(tmp_path))
    tool.start_approval_turn(_Responder(answer=False), conversation_id="session-a", turn_id="turn-a")
    agent.tools.register(tool)

    await agent.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
            text="delete file.txt",
            conversation="session-a",
        ),
        emit,
        drain,
        stream=True,
    )

    notices = [e for e in emitted if isinstance(e, Notice)]
    blocked = [n for n in notices if n.kind is NoticeKind.ACTION_BLOCKED]
    assert len(blocked) == 1, f"expected exactly one blocked notice, got {notices}"
    assert blocked[0].detail, "the blocking tool's own first line must survive to the spine event"


def test_a_tool_that_failed_without_saying_anything_yields_no_detail() -> None:
    """``detail`` is optional on the wire for this reason: a tool can abort with
    no readable line at all, and inventing one would put whitespace in front of
    a person as if it were an explanation."""
    from raven.agent.loop.main import _first_line

    assert _first_line("Error: blocked by policy\nstack line") == "Error: blocked by policy"
    assert _first_line("\n   \n\t\n") == ""
    assert _first_line("") == ""
    assert _first_line(None) == ""
