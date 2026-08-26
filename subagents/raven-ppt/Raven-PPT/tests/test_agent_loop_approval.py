from __future__ import annotations

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


async def test_a_turn_with_nobody_to_ask_loses_the_command_and_not_the_turn(tmp_path) -> None:
    """An absent approval channel refuses the command and lets the turn go on.

    The two tests above end the turn, and should: a denial is a decision to respect
    rather than route around, so the next plan must not get the chance to translate
    it into an equivalent script. Nobody to ask is not that. No one refused
    anything, no equivalent route exists -- every command needing approval meets
    the same wall -- and the model has the rest of a task it can still do without a
    shell.

    Measured on two headless deck runs that ended here with eighteen of twenty
    pages unwritten: one was cropping a logo onto a white ground, the other reading
    its own materials. Both were told the operation would not be completed and no
    alternative would be attempted, by a turn in which nobody had been asked.
    """
    provider = _Provider()
    agent = AgentLoop(provider=provider, workspace=tmp_path, model="fake/model")
    executor = _Executor()
    # No `start_approval_turn`: this is a run with no approval transport at all.
    agent.tools.register(ExecTool(executor=executor, working_dir=str(tmp_path)))

    result, _media = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
            text="delete file.txt",
            conversation="session-a",
        ),
        session_key="session-a",
    )

    assert executor.commands == []
    assert "no alternative method will be attempted" not in result
    # The model was called again with the refusal, rather than the turn ending on it.
    assert provider.responses == []
