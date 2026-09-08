"""The approval round-trip through a real agent turn.

The gate sits at the registry door, so these run ``_process_message`` end to
end: a refusal continues the turn (the model reads it and answers), the one
click that ends a turn is "deny and stop", and a blocked call's parallel
siblings never execute.
"""

from __future__ import annotations

import os
from pathlib import Path

from raven.agent.loop import AgentLoop
from raven.agent.tools.shell import ExecTool
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.contracts.permissions import ApprovalChoice, ApprovalOutcome
from raven.permissions.turn import start_permission_turn
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
            LLMResponse(content="Understood, moving on.", finish_reason="stop"),
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
            LLMResponse(content="Understood, moving on.", finish_reason="stop"),
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
    def __init__(self, choice: ApprovalChoice = ApprovalChoice.ALLOW) -> None:
        self.choice = choice
        self.requests: list[dict] = []

    async def await_approval(self, **request) -> ApprovalOutcome:
        self.requests.append(request)
        return ApprovalOutcome(choice=self.choice)


def _request() -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel="tui",
            chat_id="default",
            sender_id="user",
            chat_type=ChatType.DM,
        ),
        text="delete file.txt",
        conversation="session-a",
    )


def _agent(provider: _Provider, tmp_path, executor: _Executor) -> AgentLoop:
    # The suite's baseline config pins mode=full (conftest); these tests are
    # about the approval round-trip, so they run the product default instead.
    config = Path(os.environ["HOME"]) / ".raven" / "config.json"
    config.write_text('{"permissions": {"mode": "ask"}}')
    agent = AgentLoop(provider=provider, workspace=tmp_path, model="fake/model")
    agent.tools.register(ExecTool(executor=executor, working_dir=str(tmp_path)))
    return agent


async def test_approved_delete_executes_and_the_turn_finishes(tmp_path) -> None:
    executor = _Executor()
    responder = _Responder()
    agent = _agent(_Provider(), tmp_path, executor)
    start_permission_turn(responder, conversation_id="session-a", turn_id="turn-a")

    result, _media = await agent._process_message(_request(), session_key="session-a")

    assert result == "Understood, moving on."
    assert executor.commands == ["rm file.txt"]
    assert responder.requests[0]["conversation_id"] == "session-a"
    assert responder.requests[0]["turn_id"] == "turn-a"
    assert responder.requests[0]["command"] == "rm file.txt"
    assert responder.requests[0]["tool_call_id"] == "call-a"


async def test_denied_delete_continues_the_turn(tmp_path) -> None:
    provider = _Provider()
    executor = _Executor()
    agent = _agent(provider, tmp_path, executor)
    start_permission_turn(_Responder(ApprovalChoice.DENY), conversation_id="session-a", turn_id="turn-a")

    result, _media = await agent._process_message(_request(), session_key="session-a")

    assert result == "Understood, moving on."
    assert executor.commands == []
    assert provider.responses == []


async def test_denied_delete_skips_the_parallel_sibling(tmp_path) -> None:
    provider = _ParallelDeleteProvider()
    executor = _Executor()
    agent = _agent(provider, tmp_path, executor)
    start_permission_turn(_Responder(ApprovalChoice.DENY), conversation_id="session-a", turn_id="turn-a")

    result, _media = await agent._process_message(_request(), session_key="session-a")

    assert result == "Understood, moving on."
    assert executor.commands == []
    assert provider.responses == []


async def test_deny_and_stop_ends_the_turn(tmp_path) -> None:
    provider = _Provider()
    executor = _Executor()
    agent = _agent(provider, tmp_path, executor)
    start_permission_turn(_Responder(ApprovalChoice.DENY_STOP), conversation_id="session-a", turn_id="turn-a")

    result, _media = await agent._process_message(_request(), session_key="session-a")

    assert "no alternative method will be attempted" in result
    assert executor.commands == []
    assert len(provider.responses) == 1
