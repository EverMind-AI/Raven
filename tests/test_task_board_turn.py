"""A host turn keeps its Task Board in one extra call, after the reply has gone out."""

from __future__ import annotations

import asyncio
import copy

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.task_board.store import BOARD_FILENAME, load_board
from raven.config.schema import TaskBoardConfig
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.observability import purpose
from raven.providers.base import LLMProvider
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.token_wise import usage_context

KEY = "test:c1"
ANSWER = "The restored session has an empty token."
BOARD_REPLY = LLMResponse(
    content="",
    tool_calls=[
        ToolCallRequest(
            id="tb1",
            name="task_board",
            arguments={
                "action": "update",
                "steps": [{"new_title": "Fix bug A", "text": "Found the empty token", "status": "waiting"}],
            },
        )
    ],
)


class Recording(LLMProvider):
    def __init__(self, replies: list[LLMResponse], gate: asyncio.Event | None = None):
        super().__init__(api_key="test")
        self.replies = replies
        self.gate = gate
        self.requests: list[dict] = []

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
        self.requests.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "tool_choice": tool_choice,
                "purpose": purpose.current(),
                "usage_session": usage_context.session_key(),
                "effort": reasoning_effort,
            }
        )
        if tool_choice is not None and self.gate is not None:
            await self.gate.wait()
        return self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]

    def get_default_model(self) -> str:
        return "stub"


def _agent(workspace, provider) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(task_board_config=TaskBoardConfig()),
    )


def _turn(text: str = "Why is the page blank after login?") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text=text,
    )


async def _drain(agent: AgentLoop) -> None:
    while agent._consolidation_tasks:
        await asyncio.gather(*list(agent._consolidation_tasks), return_exceptions=True)


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return str(content or "")


def _names(tools) -> list[str]:
    return [t["function"]["name"] for t in tools or []]


@pytest.fixture(autouse=True)
def _host(monkeypatch):
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)


async def test_a_host_turn_calls_once_more_with_its_window_and_tools(tmp_path):
    provider = Recording([LLMResponse(content=ANSWER), BOARD_REPLY])
    agent = _agent(tmp_path, provider)

    await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)

    assert len(provider.requests) == 2
    turn, extra = provider.requests
    assert extra["tool_choice"] == {"type": "function", "function": {"name": "task_board"}}
    assert _names(extra["tools"]) == _names(turn["tools"]) and "task_board" in _names(extra["tools"])
    assert extra["messages"][-1]["role"] == "user" and "task board" in _text(extra["messages"][-1])
    assert any(m["role"] == "assistant" and ANSWER in _text(m) for m in extra["messages"][:-1])
    assert [m["role"] for m in extra["messages"][: len(turn["messages"])]] == [m["role"] for m in turn["messages"]]
    assert (extra["purpose"], extra["usage_session"], extra["effort"]) == ("task_board", KEY, "low")
    assert load_board(agent.sessions.session_dir(KEY)).groups[0].title == "Fix bug A"


async def test_the_reply_goes_out_before_the_board_call_returns(tmp_path):
    gate = asyncio.Event()
    agent = _agent(tmp_path, Recording([LLMResponse(content=ANSWER), BOARD_REPLY], gate=gate))

    result = await agent._process_message(_turn(), session_key=KEY)

    assert result is not None and ANSWER in str(result)
    assert not (agent.sessions.session_dir(KEY) / BOARD_FILENAME).exists()
    gate.set()
    await _drain(agent)
    assert (agent.sessions.session_dir(KEY) / BOARD_FILENAME).exists()


async def test_the_step_is_keyed_by_the_turns_received_at(tmp_path):
    agent = _agent(tmp_path, Recording([LLMResponse(content=ANSWER), BOARD_REPLY]))

    await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)

    opening = next(m for m in agent.sessions.peek(KEY).messages if m["role"] == "user")
    step = load_board(agent.sessions.session_dir(KEY)).groups[0].steps[0]
    assert step.turn_at == opening["timestamp"]


async def test_a_turn_whose_tools_lack_the_board_makes_no_extra_call(tmp_path):
    provider = Recording([LLMResponse(content=ANSWER), BOARD_REPLY])
    agent = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True, disabled_tools=["task_board"]),
        engine=EngineWiring(task_board_config=TaskBoardConfig()),
    )

    await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)

    assert "task_board" not in _names(provider.requests[0]["tools"])
    assert len(provider.requests) == 1


async def test_a_sub_agent_process_makes_no_extra_call(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    provider = Recording([LLMResponse(content=ANSWER), BOARD_REPLY])
    agent = _agent(tmp_path, provider)

    await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)

    assert len(provider.requests) == 1
