"""A host turn keeps its Task Board in one extra call, after the reply has gone out."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.task_board import service as board_service
from raven.agent.task_board.store import BOARD_FILENAME, load_board
from raven.config.schema import TaskBoardConfig
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.contracts.tool import Tool
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
                "steps": [
                    {
                        "new_title": "Fix bug A",
                        "new_summary": "Blank page after login; cause found",
                        "title": "Found the empty token",
                        "summary": "The restored session carries an empty token.",
                        "status": "needs_you",
                        "questions": ["Which fix should I apply?"],
                    }
                ],
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
        if purpose.current() == "task_board" and self.gate is not None:
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
    # The turn's own choice: forcing the tool costs the cached history on Anthropic,
    # and several providers refuse a forced choice outright.
    assert extra["tool_choice"] == turn["tool_choice"]
    assert _names(extra["tools"]) == _names(turn["tools"]) and "task_board" in _names(extra["tools"])
    assert extra["messages"][-1]["role"] == "user" and "task board" in _text(extra["messages"][-1])
    assert any(m["role"] == "assistant" and ANSWER in _text(m) for m in extra["messages"][:-1])
    assert [m["role"] for m in extra["messages"][: len(turn["messages"])]] == [m["role"] for m in turn["messages"]]
    assert (extra["purpose"], extra["usage_session"], extra["effort"]) == ("task_board", KEY, turn["effort"])
    assert load_board(agent.sessions.session_dir(KEY)).groups[0].title == "Fix bug A"


async def test_the_board_call_runs_at_the_turns_own_effort(tmp_path, monkeypatch):
    provider = Recording([LLMResponse(content=ANSWER), BOARD_REPLY])
    agent = _agent(tmp_path, provider)
    monkeypatch.setattr(type(agent), "default_reasoning_effort", property(lambda _self: "high"))

    await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)

    turn, extra = provider.requests
    assert turn["effort"] == extra["effort"] == "high"


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


class _ReceiptSpawn(Tool):
    """Stands in for spawn: returns the receipt the dispatch reader looks for."""

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return "Start a sub-agent."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"node_id": {"type": "string"}, "task_summary": {"type": "string"}}}

    async def execute(self, node_id: str, task_summary: str = "", **_: Any) -> str:
        return f"Subagent [{task_summary}] started (id: 1a2b3c4d). I'll notify you when it completes."


def _update(**args: Any) -> LLMResponse:
    return LLMResponse(
        content="", tool_calls=[ToolCallRequest(id="tb", name="task_board", arguments={"action": "update", **args})]
    )


async def test_a_dispatch_from_an_earlier_turn_is_not_placed_again(tmp_path):
    spawn = ToolCallRequest(
        id="sp1", name="spawn", arguments={"node_id": "locate_bug", "task_summary": "Locate the bug"}
    )
    placed = {
        "new_title": "Fix the blank page",
        "new_summary": "A sub-agent is locating the cause",
        "title": "Sent a sub-agent to locate it",
        "summary": "Dispatched locate_bug.",
        "status": "idle",
        "tasks": ["spawn:locate_bug"],
    }
    provider = Recording(
        [
            LLMResponse(content="", tool_calls=[spawn]),
            LLMResponse(content="Started a sub-agent on it."),
            _update(steps=[placed]),
            LLMResponse(content="Here is the email."),
            _update(steps=[]),
        ]
    )
    agent = _agent(tmp_path, provider)
    agent.tools.register(_ReceiptSpawn())

    await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)
    await agent._process_message(_turn("Unrelated: draft a short email."), session_key=KEY)
    await _drain(agent)

    second = provider.requests[4]
    assert second["purpose"] == "task_board"
    instruction = _text(second["messages"][-1])
    handles = instruction[instruction.index("[BEGIN DISPATCHES]") : instruction.index("[END DISPATCHES]")]
    assert "spawn:locate_bug" not in handles, "a dispatch belongs to the turn that made it"
    assert len(provider.requests) == 5, "the second turn's update was refused and asked again"
    board = load_board(agent.sessions.session_dir(KEY))
    assert board.unassigned == [] and [len(g.steps) for g in board.groups] == [1]


@pytest.mark.parametrize(
    ("board", "channel", "asks_again"),
    [(True, "tui", False), (True, "weixin", True), (False, "tui", True)],
    ids=["board-on-the-page", "board-on-im", "board-off"],
)
async def test_the_host_stops_asking_open_decisions_again_where_the_page_draws_the_board(
    tmp_path, board, channel, asks_again
):
    provider = Recording([LLMResponse(content=ANSWER), BOARD_REPLY])
    agent = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(task_board_config=TaskBoardConfig(enabled=board)),
    )
    turn = TurnRequest(
        origin=Origin.USER,
        source=Source(channel=channel, chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text="Why is the page blank after login?",
    )

    await agent._process_message(turn, session_key=f"{channel}:c1")
    await _drain(agent)

    system = _text(provider.requests[0]["messages"][0])
    assert ("Ask on every reply" in system) is asks_again
    assert ("close every reply" in system) is asks_again


async def test_the_assembler_puts_the_conversations_notes_in_the_runtime_block():
    from raven.context_engine.assembler import ContextAssembler
    from raven.contracts.assembled import TokenBudget
    from raven.contracts.context import TurnContext

    assembler = ContextAssembler(
        [], get_tool_definitions=lambda: [], get_session_notices=lambda key: [f"note for {key}"]
    )
    budget = TokenBudget(
        context_length=200_000,
        reserved_output=8000,
        reserved_tools=4000,
        reserved_system=4000,
        available_history=184_000,
    )
    assembled = await assembler.assemble(
        "tui:default", [], budget, turn=TurnContext(current_message="hi", channel="tui", chat_id="default")
    )

    user = _text(assembled.messages[-1])
    assert user.index("note for tui:default") < user.index("\n\nhi")


async def test_a_question_set_aside_opens_the_next_turn_once_and_stays_out_of_the_history(tmp_path):
    provider = Recording(
        [
            LLMResponse(content=ANSWER),
            BOARD_REPLY,
            LLMResponse(content="Noted, moving on."),
            _update(steps=[]),
            LLMResponse(content="Nothing else is open."),
            _update(steps=[]),
        ]
    )
    agent = _agent(tmp_path, provider)
    await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)
    group = load_board(agent.sessions.session_dir(KEY)).groups[0]

    await agent.task_board.ignore_question(KEY, group.id, group.steps[-1].id, "Which fix should I apply?")
    await agent._process_message(_turn("Anything else?"), session_key=KEY)
    await _drain(agent)
    await agent._process_message(_turn("And now?"), session_key=KEY)
    await _drain(agent)

    turns = [r for r in provider.requests if r["purpose"] != "task_board"]
    boards = [r for r in provider.requests if r["purpose"] == "task_board"]
    note = 'the user set aside "Which fix should I apply?" (Fix bug A)'
    assert note in _text(turns[1]["messages"][-1]) and _text(turns[1]["messages"][-1]).endswith("Anything else?")
    assert any(note in _text(m) for m in boards[1]["messages"]), "the turn's own board call reads it too"
    assert note not in _text(turns[2]["messages"][-1])
    stored = agent.sessions.get_or_create(KEY).messages
    assert not any("set aside" in _text(m) for m in stored if m.get("role") == "user")


async def test_a_note_ack_the_disk_refuses_leaves_the_reply_and_shows_the_note_again(tmp_path, monkeypatch):
    provider = Recording(
        [
            LLMResponse(content=ANSWER),
            BOARD_REPLY,
            LLMResponse(content="Noted, moving on."),
            _update(steps=[]),
            LLMResponse(content="Nothing else is open."),
            _update(steps=[]),
        ]
    )
    agent = _agent(tmp_path, provider)
    await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)
    group = load_board(agent.sessions.session_dir(KEY)).groups[0]
    await agent.task_board.ignore_question(KEY, group.id, group.steps[-1].id, "Which fix should I apply?")
    save_notes = board_service.save_notes

    def disk_full(*_args: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(board_service, "save_notes", disk_full)
    result = await agent._process_message(_turn("Anything else?"), session_key=KEY)
    await _drain(agent)
    monkeypatch.setattr(board_service, "save_notes", save_notes)
    await agent._process_message(_turn("And now?"), session_key=KEY)
    await _drain(agent)

    assert result is not None and "Noted, moving on." in str(result)
    turns = [r for r in provider.requests if r["purpose"] != "task_board"]
    note = 'the user set aside "Which fix should I apply?" (Fix bug A)'
    assert note in _text(turns[2]["messages"][-1]), "a note never acknowledged is shown again"
