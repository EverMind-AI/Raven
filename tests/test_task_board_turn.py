"""A host turn keeps its Task Board in one extra call, after the reply has gone out."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.task_board import files as board_files
from raven.agent.task_board import service as board_service
from raven.agent.task_board.board import Board, ProgressStep, TaskGroup, TaskLink
from raven.agent.task_board.store import BOARD_FILENAME, load_board, save_board
from raven.config.schema import TaskBoardConfig
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.contracts.tool import Tool
from raven.observability import purpose
from raven.providers.base import LLMProvider
from raven.spine.message import ChatType, Source
from raven.spine.turn import AnswerlessTurnError, Origin, TurnRequest
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
        # A gather of tasks that already finished completes without yielding, and the
        # done callbacks that take them out of the set wait for the loop's next turn.
        await asyncio.sleep(0)


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


async def test_the_board_call_runs_at_no_effort_when_the_turn_ran_at_none(tmp_path, monkeypatch):
    provider = Recording([LLMResponse(content=ANSWER), BOARD_REPLY])
    provider.generation = dataclasses.replace(provider.generation, reasoning_effort="high")
    agent = _agent(tmp_path, provider)
    monkeypatch.setattr(type(agent), "default_reasoning_effort", property(lambda _self: None))

    await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)

    turn, extra = provider.requests
    assert turn["effort"] is None
    assert extra["effort"] is None


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
        return "Start a subagent."

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
        "new_summary": "A subagent is locating the cause",
        "title": "Sent a subagent to locate it",
        "summary": "Dispatched locate_bug.",
        "status": "idle",
        "tasks": ["spawn:locate_bug"],
    }
    provider = Recording(
        [
            LLMResponse(content="", tool_calls=[spawn]),
            LLMResponse(content="Started a subagent on it."),
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


def _delivering(agent: AgentLoop) -> list[str]:
    """Give the message tool a channel to send on; what it sent, in order."""
    sent: list[str] = []

    async def deliver(content: str, _media: list[str]) -> None:
        sent.append(content)

    agent.tools.get("message").set_send_callback(deliver)
    return sent


ASKS_THROUGH_THE_MESSAGE_TOOL = LLMResponse(
    content="",
    tool_calls=[ToolCallRequest(id="m1", name="message", arguments={"content": "Which fix should I apply?"})],
)


async def test_a_turn_the_message_tool_answered_keeps_the_board_though_it_ends_on_the_marker(tmp_path):
    """Not a Silent Turn: the reader has the answer the message tool sent, and the question
    it asked belongs on the board."""
    provider = Recording([ASKS_THROUGH_THE_MESSAGE_TOOL, LLMResponse(content="[SILENT]"), BOARD_REPLY])
    agent = _agent(tmp_path, provider)
    sent = _delivering(agent)

    await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)

    assert sent == ["Which fix should I apply?"]
    assert [r["purpose"] for r in provider.requests] == ["main", "main", "task_board"]


async def test_a_silent_turn_makes_no_board_call(tmp_path):
    """The host answered with its silence marker and started nothing: no step can
    say what moved, so the post-turn call is not made at all."""
    provider = Recording([LLMResponse(content="[SILENT]"), BOARD_REPLY])
    agent = _agent(tmp_path, provider)

    await agent._process_message(_turn("Anything new on the login bug?"), session_key=KEY)
    await _drain(agent)

    assert [r["purpose"] for r in provider.requests] == ["main"]
    assert not (agent.sessions.session_dir(KEY) / BOARD_FILENAME).exists()


def _placed_and_another(agent: AgentLoop) -> None:
    """g_a placed the subagent run an earlier turn started; g_b is another matter."""
    run = TaskLink("spawn", "locate_bug", "Locate the bug")
    placed = ProgressStep("s_a", "T0", None, "Sent a subagent", "Dispatched it.", [run])
    drafted = ProgressStep("s_b", "T0", None, "Drafted", "Drafted the notes.", [])
    groups = [
        TaskGroup("g_a", "Fix the blank page", "A subagent is locating it", "running", 1, 1, [placed]),
        TaskGroup("g_b", "Release notes", "Drafting the notes", "idle", 1, 1, [drafted]),
    ]
    save_board(agent.sessions.session_dir(KEY), Board(groups, []))


def _both_moved() -> LLMResponse:
    return _update(
        steps=[{"group": g, "title": f"Moved {g}", "summary": "Moved.", "status": "idle"} for g in ("g_a", "g_b")]
    )


def _scope(instruction: str) -> str:
    return instruction[instruction.index("[END DISPATCHES]") : instruction.index("How to write the update:")]


async def test_a_delegated_result_keeps_its_turns_board_to_the_group_that_placed_the_run(tmp_path):
    provider = Recording([LLMResponse(content="The subagent found the empty token."), _both_moved()])
    agent = _agent(tmp_path, provider)
    _placed_and_another(agent)
    result = TurnRequest(
        origin=Origin.SUBAGENT,
        source=Source(channel="test", chat_id="c1", sender_id="subagent", chat_type=ChatType.DM),
        text="[Subagent 'Locate the bug' completed successfully]\n\nResult: the restored token is empty.",
        delegated={"kind": "spawn", "label": "Locate the bug", "status": "ok", "node_id": "locate_bug"},
    )

    await agent._process_message(result, session_key=KEY, origin=Origin.SUBAGENT)
    await _drain(agent)

    assert "g_a" in _scope(_text(provider.requests[1]["messages"][-1]))
    assert [len(g.steps) for g in load_board(agent.sessions.session_dir(KEY)).groups] == [2, 1]


async def test_a_move_on_keeps_its_turns_board_to_the_group_it_named(tmp_path):
    provider = Recording([LLMResponse(content=ANSWER), _both_moved()])
    agent = _agent(tmp_path, provider)
    _placed_and_another(agent)
    await agent.task_board.move_on(KEY, "g_b")

    await agent._process_message(_turn("Draft them for Friday."), session_key=KEY)
    await _drain(agent)

    assert "g_b" in _scope(_text(provider.requests[1]["messages"][-1]))
    assert [len(g.steps) for g in load_board(agent.sessions.session_dir(KEY)).groups] == [1, 2]


async def test_a_turn_the_user_started_with_nothing_picked_keeps_every_group_it_moved(tmp_path):
    provider = Recording([LLMResponse(content=ANSWER), _both_moved()])
    agent = _agent(tmp_path, provider)
    _placed_and_another(agent)

    await agent._process_message(_turn("Where do the fix and the notes stand?"), session_key=KEY)
    await _drain(agent)

    assert _scope(_text(provider.requests[1]["messages"][-1])) == "[END DISPATCHES]\n\n"
    assert [len(g.steps) for g in load_board(agent.sessions.session_dir(KEY)).groups] == [2, 2]


async def _consolidated(_session) -> bool:
    return True


async def test_new_starts_the_next_conversation_with_an_empty_board(tmp_path, monkeypatch):
    """`/new` clears the conversation in place, under the same key: the board and the
    notes beside it go with the messages, or the next turn's update, recall and the page
    would carry the last conversation's matters into this one."""
    agent = _agent(tmp_path, Recording([LLMResponse(content=ANSWER)]))
    monkeypatch.setattr(agent.memory_consolidator, "consolidate_unconsolidated", _consolidated)
    _placed_and_another(agent)
    await agent.task_board.move_on(KEY, "g_b")

    reply = await agent._process_message(_turn("/new"), session_key=KEY)

    assert reply == ("New session started.", [])
    assert load_board(agent.sessions.session_dir(KEY)).groups == []
    assert agent.task_board.turn_notes(KEY) == []


async def test_new_starts_the_next_conversation_when_the_board_cannot_be_emptied(tmp_path, monkeypatch):
    """The messages are already gone; a board that cannot be emptied must not turn the
    clear into a failure."""
    agent = _agent(tmp_path, Recording([LLMResponse(content=ANSWER)]))
    monkeypatch.setattr(agent.memory_consolidator, "consolidate_unconsolidated", _consolidated)
    _placed_and_another(agent)

    def refuse(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    for name in ("write_text", "remove"):
        monkeypatch.setattr(board_files, name, refuse)

    reply = await agent._process_message(_turn("/new"), session_key=KEY)

    assert reply == ("New session started.", [])
    assert agent.sessions.get_or_create(KEY).messages == []


async def test_an_update_still_on_its_way_at_new_writes_nothing(tmp_path, monkeypatch):
    gate = asyncio.Event()
    agent = _agent(tmp_path, Recording([LLMResponse(content=ANSWER), BOARD_REPLY], gate=gate))
    monkeypatch.setattr(agent.memory_consolidator, "consolidate_unconsolidated", _consolidated)

    await agent._process_message(_turn(), session_key=KEY)
    await agent._process_message(_turn("/new"), session_key=KEY)
    gate.set()
    await _drain(agent)

    assert load_board(agent.sessions.session_dir(KEY)).groups == []


async def test_a_resident_host_removes_a_deleted_conversations_board(tmp_path):
    agent = _agent(tmp_path, Recording([LLMResponse(content=ANSWER)]))
    _placed_and_another(agent)
    await agent.start_plugin_services()
    try:
        agent.sessions.delete(KEY)
    finally:
        await agent.stop_plugin_services()

    assert not (agent.sessions.session_dir(KEY) / BOARD_FILENAME).exists()


async def test_an_update_still_on_its_way_at_delete_writes_nothing(tmp_path):
    gate = asyncio.Event()
    agent = _agent(tmp_path, Recording([LLMResponse(content=ANSWER), BOARD_REPLY], gate=gate))
    await agent.start_plugin_services()
    try:
        await agent._process_message(_turn(), session_key=KEY)
        agent.sessions.delete(KEY)
        gate.set()
        await _drain(agent)
    finally:
        await agent.stop_plugin_services()

    assert not (agent.sessions.session_dir(KEY) / BOARD_FILENAME).exists()


async def test_the_hosts_recall_asks_its_own_loop_what_still_runs(tmp_path, monkeypatch):
    """A graph run the loop still executes reads as running in recall, as on the page --
    not as one a restart cut off."""
    from raven.agent.subagent.dag_store import DagRunStore, index_guard
    from raven.agent.subagent.history import dag_root, nodes_root, session_history_root
    from raven.agent.subagent.prompt_backend import LocalFileBackend

    run = "20261029T100213Z-3eb3b470"
    agent = _agent(tmp_path, Recording([LLMResponse(content=ANSWER)]))
    session_dir = agent.sessions.session_dir(KEY)
    nodes_root(session_dir).mkdir(parents=True, exist_ok=True)
    store = DagRunStore(
        LocalFileBackend(),
        str(dag_root(session_dir)),
        run,
        nodes_root=str(nodes_root(session_dir)),
        registry_root=str(session_history_root(session_dir)),
    )
    graph = {"task_summary": "review", "nodes": [{"id": "n1", "subagent": "Raven", "prompt_template": "do"}]}
    async with index_guard(store.registry_root):
        await store.init(json.dumps(graph), node_ids=["n1"])
    started = ProgressStep("s_a", "T0", None, "Started a review", "Started.", [TaskLink("dag", run, "Review fixes")])
    save_board(session_dir, Board([TaskGroup("g_a", "Review the fix", "", "running", 1, 1, [started])], []))
    monkeypatch.setattr(type(agent), "active_dag_run_ids", lambda _self: {run})
    tool = agent.tools.get("task_board")
    tool.set_context("test", "c1", KEY)

    out = await tool.execute(action="recall", group_id="g_a")

    assert f"dag:{run} (Review fixes): running" in out


async def test_a_delete_the_disk_refuses_keeps_the_board_and_the_update_on_its_way(tmp_path, monkeypatch):
    """Refused only the transcript's unlink -- an open handle on Windows -- the store answers
    false with the conversation intact, and the board stays with it."""
    import pathlib

    gate = asyncio.Event()
    agent = _agent(tmp_path, Recording([LLMResponse(content=ANSWER), BOARD_REPLY], gate=gate))
    await agent.start_plugin_services()
    try:
        await agent._process_message(_turn(), session_key=KEY)
        transcript = agent.sessions.session_path(KEY)
        real_unlink = pathlib.Path.unlink

        def refuse(self, *args, **kwargs):
            if self == transcript:
                raise PermissionError(13, "Access is denied")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "unlink", refuse)
        assert agent.sessions.delete(KEY) is False
        monkeypatch.undo()
        gate.set()
        await _drain(agent)
    finally:
        await agent.stop_plugin_services()

    assert transcript.exists()
    assert load_board(agent.sessions.session_dir(KEY)).groups[0].title == "Fix bug A"


SPAWN_CALL = LLMResponse(
    content="",
    tool_calls=[
        ToolCallRequest(id="s1", name="spawn", arguments={"node_id": "locate_bug", "task_summary": "Locate the bug"})
    ],
)
PROVIDER_DOWN = LLMResponse(content=None, finish_reason="error")


class ByPurpose(Recording):
    """Answers the board's call with ``board_reply`` and the turns' own calls in order,
    so a board call held at the gate never takes a reply a later turn's call was due."""

    def __init__(self, turn_replies: list[LLMResponse], board_reply: LLMResponse, gate: asyncio.Event | None = None):
        super().__init__(turn_replies, gate)
        self.board_reply = board_reply
        self.turn_calls = 0

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
        self.requests.append({"purpose": purpose.current(), "usage_session": usage_context.session_key()})
        if purpose.current() == "task_board":
            if self.gate is not None:
                await self.gate.wait()
            return self.board_reply
        self.turn_calls += 1
        return self.replies[min(self.turn_calls - 1, len(self.replies) - 1)]


def _spawning_agent(tmp_path, provider) -> AgentLoop:
    agent = _agent(tmp_path, provider)
    agent.tools.register(_ReceiptSpawn())
    return agent


def _terminal_turn(text: str = "Why is the page blank after login?") -> TurnRequest:
    source = Source(channel="tui", chat_id="c1", sender_id="user", chat_type=ChatType.DM, surface="tui")
    return TurnRequest(origin=Origin.USER, source=source, text=text)


class _AddressedSpawn(_ReceiptSpawn):
    """A spawn that keeps the surface each turn addressed it with."""

    def __init__(self) -> None:
        super().__init__()
        self.surfaces: list[str | None] = []

    def set_context(self, channel: str, chat_id: str, session_key: str, surface: str | None = None) -> None:
        self.surfaces.append(surface)


async def test_a_dispatch_on_the_terminal_is_addressed_and_noted_as_the_terminal(tmp_path):
    """The terminal shares the page's channel and draws no board: the run it starts is
    addressed back to the terminal, and the note after its receipt keeps the rule that
    open decisions close the reply."""
    from raven.context_engine.segments.render import reply_rules_note

    provider = Recording([SPAWN_CALL, LLMResponse(content=ANSWER)])
    agent = _agent(tmp_path, provider)
    spawn = _AddressedSpawn()
    agent.tools.register(spawn)

    await agent._process_message(_terminal_turn(), session_key="tui:c1")
    await _drain(agent)

    receipt = next(m for m in provider.requests[1]["messages"] if m.get("role") == "tool")
    assert spawn.surfaces == ["tui"]
    assert _text(receipt).endswith(reply_rules_note(board_shown=False))


async def test_a_turn_that_fails_after_a_spawn_parks_what_it_dispatched(tmp_path):
    """No call follows a provider that just failed: the run waits in unassigned, keyed by
    the turn, for the next update to place."""
    provider = Recording([SPAWN_CALL, PROVIDER_DOWN])
    agent = _spawning_agent(tmp_path, provider)

    with pytest.raises(AnswerlessTurnError):
        await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)

    opening = next(m for m in agent.sessions.peek(KEY).messages if m["role"] == "user")
    board = load_board(agent.sessions.session_dir(KEY))
    assert [(p.handle, p.turn_at) for p in board.unassigned] == [("spawn:locate_bug", opening["timestamp"])]
    assert "task_board" not in [r["purpose"] for r in provider.requests]


SPAWN_AND_ASK = LLMResponse(
    content="",
    tool_calls=[
        ToolCallRequest(id="s1", name="spawn", arguments={"node_id": "locate_bug", "task_summary": "Locate the bug"}),
        ToolCallRequest(id="m1", name="message", arguments={"content": "Searching; which fix do you want?"}),
    ],
)


async def test_a_turn_whose_provider_failed_after_the_message_tool_answered_parks_what_it_dispatched(tmp_path):
    """The reader has the tool's answer, so the turn does not raise; it still failed, and no
    call follows a provider that just failed: the run waits in unassigned, as it would
    after a failure that raises."""
    provider = Recording([SPAWN_AND_ASK, PROVIDER_DOWN])
    agent = _spawning_agent(tmp_path, provider)
    sent = _delivering(agent)

    await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)

    assert sent == ["Searching; which fix do you want?"]
    assert "task_board" not in [r["purpose"] for r in provider.requests]
    assert [p.handle for p in load_board(agent.sessions.session_dir(KEY)).unassigned] == ["spawn:locate_bug"]


async def test_a_turn_stopped_after_a_spawn_parks_what_it_dispatched(tmp_path):
    held = asyncio.Event()

    class HoldsTheSecondCall(Recording):
        async def chat(self, messages, **kw):
            if self.requests:
                self.requests.append({"purpose": purpose.current()})
                await held.wait()
            return await super().chat(messages, **kw)

    provider = HoldsTheSecondCall([SPAWN_CALL, LLMResponse(content=ANSWER)])
    agent = _spawning_agent(tmp_path, provider)

    task = asyncio.create_task(agent._process_message(_turn(), session_key=KEY))
    while len(provider.requests) < 2:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _drain(agent)

    assert [p.handle for p in load_board(agent.sessions.session_dir(KEY)).unassigned] == ["spawn:locate_bug"]


async def test_a_failed_turns_park_waits_for_the_update_still_in_its_call(tmp_path):
    """Parked beside an update still in its call, the run would be a handle that update
    was never told of and has to place, and the update would be refused for it."""
    gate = asyncio.Event()
    provider = ByPurpose([LLMResponse(content=ANSWER), SPAWN_CALL, PROVIDER_DOWN], BOARD_REPLY, gate=gate)
    agent = _spawning_agent(tmp_path, provider)

    await agent._process_message(_turn(), session_key=KEY)
    with pytest.raises(AnswerlessTurnError):
        await agent._process_message(_turn("Find out why"), session_key=KEY)
    gate.set()
    await _drain(agent)

    board = load_board(agent.sessions.session_dir(KEY))
    assert [g.title for g in board.groups] == ["Fix bug A"]
    assert [p.handle for p in board.unassigned] == ["spawn:locate_bug"]


async def test_a_turn_that_raises_after_a_spawn_parks_what_it_dispatched(tmp_path, monkeypatch):
    """The third way a turn ends with no answer: an error nothing below the turn caught."""
    agent = _spawning_agent(tmp_path, Recording([SPAWN_CALL, LLMResponse(content=ANSWER)]))
    loop = agent._run_agent_loop

    async def breaks_after(*args: Any, **kwargs: Any) -> Any:
        await loop(*args, **kwargs)
        raise RuntimeError("the turn broke after its loop")

    monkeypatch.setattr(agent, "_run_agent_loop", breaks_after)

    with pytest.raises(RuntimeError):
        await agent._process_message(_turn(), session_key=KEY)
    await _drain(agent)

    assert [p.handle for p in load_board(agent.sessions.session_dir(KEY)).unassigned] == ["spawn:locate_bug"]


@pytest.mark.parametrize("edit", ["clear", "undo"])
async def test_a_failed_turns_park_writes_nothing_after_a_history_edit(tmp_path, monkeypatch, edit):
    """Held in the queue behind an update, the park meets the clear or the undo that
    landed meanwhile: the turn it is about is gone, and so is what it dispatched."""
    gate = asyncio.Event()
    provider = ByPurpose([LLMResponse(content=ANSWER), SPAWN_CALL, PROVIDER_DOWN], BOARD_REPLY, gate=gate)
    agent = _spawning_agent(tmp_path, provider)
    monkeypatch.setattr(agent.memory_consolidator, "consolidate_unconsolidated", _consolidated)

    await agent._process_message(_turn(), session_key=KEY)
    with pytest.raises(AnswerlessTurnError):
        await agent._process_message(_turn("Find out why"), session_key=KEY)
    if edit == "clear":
        await agent._process_message(_turn("/new"), session_key=KEY)
    else:
        failed = [m for m in agent.sessions.peek(KEY).messages if m["role"] == "user"][-1]
        await agent.task_board.drop_turns(KEY, {failed["timestamp"]})
    gate.set()
    await _drain(agent)

    assert load_board(agent.sessions.session_dir(KEY)).unassigned == []
