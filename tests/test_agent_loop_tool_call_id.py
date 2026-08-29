from __future__ import annotations

from typing import Any

from raven.agent.loop import AgentLoop
from raven.contracts.tool import Tool
from raven.providers.base import LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _Provider:
    def __init__(self, tool_name: str) -> None:
        self.responses = [
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="call-a", name=tool_name, arguments={})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "fake/model"


class _IdAwareTool(Tool):
    """A tool that wants to correlate its own side-channel output with the row
    the UI drew for this call -- what ``exec`` and ``run_subagent_dag`` both do."""

    def __init__(self) -> None:
        self.seen: list[str | None] = []

    @property
    def name(self) -> str:
        return "id_aware"

    @property
    def description(self) -> str:
        return "records the tool call id it was handed"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def set_tool_call_id(self, tool_call_id: str | None) -> None:
        self.seen.append(tool_call_id)

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _PlainTool(_IdAwareTool):
    """No ``set_tool_call_id`` -- the loop must dispatch it just the same."""

    set_tool_call_id = None  # type: ignore[assignment]

    @property
    def name(self) -> str:
        return "plain"


async def _drive(agent: AgentLoop) -> str:
    result, _media = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
            text="go",
            conversation="session-a",
        ),
        session_key="session-a",
    )
    return result


async def test_loop_hands_the_tool_call_id_to_a_tool_that_accepts_one(tmp_path) -> None:
    agent = AgentLoop(provider=_Provider("id_aware"), workspace=tmp_path, model="fake/model")
    tool = _IdAwareTool()
    agent.tools.register(tool)

    await _drive(agent)

    assert tool.seen == ["call-a"]


async def test_loop_dispatches_a_tool_that_takes_no_tool_call_id(tmp_path) -> None:
    agent = AgentLoop(provider=_Provider("plain"), workspace=tmp_path, model="fake/model")
    agent.tools.register(_PlainTool())

    assert await _drive(agent) == "done"
