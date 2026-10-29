"""The post-turn call that keeps the Task Board: attempts, refusals, parking, ordering, bounds."""

from __future__ import annotations

import asyncio

from raven.agent.task_board.board import Board, Dispatch, Pending, ProgressStep, TaskGroup
from raven.agent.task_board.runner import (
    MAX_ATTEMPTS,
    OPEN_STEPS_SHOWN,
    build_instruction,
    run_post_turn,
    should_run,
)
from raven.agent.task_board.service import TaskBoardService
from raven.agent.task_board.store import BOARD_FILENAME, load_board, save_board
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.session.manager import SessionManager

KEY = "web:alpha"
SPAWN = Dispatch("spawn:locate_login_bug", "Locate the blank-page bug")
WINDOW = [
    {"role": "system", "content": "You are Raven."},
    {"role": "user", "content": "Why is the page blank?"},
    {"role": "assistant", "content": "The token is empty; I sent a sub-agent to confirm."},
]


def _update(steps: list[dict], call_id: str = "t1") -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=call_id, name="task_board", arguments={"action": "update", "steps": steps})],
    )


GOOD = _update([{"new_title": "Fix bug A", "text": "Found the cause", "status": "waiting", "tasks": [SPAWN.handle]}])


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    async def __call__(self, rows: list[dict]) -> LLMResponse:
        self.seen.append(list(rows))
        reply = self.replies[min(len(self.seen) - 1, len(self.replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _service(tmp_path):
    return TaskBoardService(SessionManager(tmp_path), now_ms=lambda: 1_000)


async def _run(service, call, dispatches=(SPAWN,), turn_at="2026-10-29T10:02:13"):
    return await run_post_turn(
        service,
        session_key=KEY,
        call=call,
        window=WINDOW,
        dispatches=list(dispatches),
        turn_at=turn_at,
        turn_id="turn-1",
        now_ms=service.now_ms,
    )


def test_the_constants_are_the_specs():
    assert (MAX_ATTEMPTS, OPEN_STEPS_SHOWN) == (3, 2)


def test_should_run_after_an_answer_or_any_dispatch():
    assert should_run("completed", [])
    assert should_run("error", [SPAWN])
    assert not should_run("error", [])
    assert not should_run("interrupted", [])


async def test_an_accepted_update_is_written_and_announced(tmp_path):
    service = _service(tmp_path)
    heard = []

    async def sink(conversation, event):
        heard.append(event)

    service.set_sink(sink)
    board = await _run(service, Scripted(GOOD))

    assert board is not None and board.groups[0].title == "Fix bug A"
    assert load_board(service.session_dir(KEY)).groups[0].steps[0].turn_at == "2026-10-29T10:02:13"
    assert [e["type"] for e in heard] == ["tasks.board_updated"]


async def test_the_call_sees_the_window_then_one_instruction(tmp_path):
    call = Scripted(GOOD)

    await _run(_service(tmp_path), call)

    rows = call.seen[0]
    assert rows[: len(WINDOW)] == WINDOW
    assert len(rows) == len(WINDOW) + 1 and rows[-1]["role"] == "user"
    assert "spawn:locate_login_bug" in rows[-1]["content"]


async def test_a_rejected_update_is_asked_again_with_its_reason(tmp_path):
    missing = _update([{"new_title": "Fix bug A", "text": "Found it", "status": "waiting", "tasks": []}], "t0")
    call = Scripted(missing, GOOD)

    board = await _run(_service(tmp_path), call)

    assert board is not None
    second = call.seen[1]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "t0"
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "t0"
    assert second[-1]["content"].startswith("Rejected: not placed: spawn:locate_login_bug")


async def test_prose_three_times_parks_the_handles_in_unassigned(tmp_path):
    service = _service(tmp_path)
    call = Scripted(LLMResponse(content="Sure, updating the board now."))

    assert await _run(service, call) is None

    assert len(call.seen) == MAX_ATTEMPTS
    stored = load_board(service.session_dir(KEY))
    assert stored.groups == []
    assert stored.unassigned == [Pending(SPAWN.handle, SPAWN.summary, "2026-10-29T10:02:13", "turn-1")]


async def test_a_recall_inside_the_post_turn_call_counts_as_a_failure(tmp_path):
    recall = LLMResponse(
        content="", tool_calls=[ToolCallRequest(id="r", name="task_board", arguments={"action": "recall"})]
    )
    call = Scripted(recall, GOOD)

    board = await _run(_service(tmp_path), call)

    assert board is not None
    assert call.seen[1][-1]["content"].startswith("Only action=update is accepted here")


async def test_a_provider_error_parks_the_handles_at_once(tmp_path):
    for failure in (LLMResponse(content=None, finish_reason="error"), RuntimeError("endpoint down")):
        service = TaskBoardService(SessionManager(tmp_path / type(failure).__name__), now_ms=lambda: 1)
        call = Scripted(failure)

        assert await _run(service, call) is None
        assert len(call.seen) == 1
        assert [p.handle for p in load_board(service.session_dir(KEY)).unassigned] == [SPAWN.handle]


async def test_two_updates_of_one_session_run_in_order(tmp_path):
    service = _service(tmp_path)
    second = Dispatch("dag:20261029T110000Z-0badf00d", "Review fixes")
    first_call = Scripted(GOOD)

    async def slow_first(rows):
        await asyncio.sleep(0.05)
        return await first_call(rows)

    second_call = Scripted(
        _update(
            [{"new_title": "Review PR", "text": "Started the review", "status": "active", "tasks": [second.handle]}]
        )
    )

    await asyncio.gather(_run(service, slow_first), _run(service, second_call, dispatches=[second], turn_at="T2"))

    assert "Fix bug A" in second_call.seen[0][-1]["content"]
    assert [g.title for g in load_board(service.session_dir(KEY)).groups] == ["Fix bug A", "Review PR"]


def test_the_instruction_stays_bounded_on_a_long_session():
    groups = []
    for i in range(60):
        steps = [ProgressStep(f"s{i}_{j}", f"T{j}", None, f"group {i} step {j}", []) for j in range(7)]
        groups.append(TaskGroup(f"g_{i}", f"Matter {i}", "completed" if i % 2 else "active", i, i, steps))

    text = build_instruction(Board(groups, []), [SPAWN])

    assert "group 0 step 6" in text and "group 0 step 5" in text and "group 0 step 4" not in text
    assert "g_1: Matter 1" in text and "group 1 step" not in text


def test_unassigned_handles_are_listed_before_the_turns_own():
    left = Pending("dag:20261029T090000Z-00000001", "Earlier run", "T0", None)

    text = build_instruction(Board([], [left]), [SPAWN])

    assert text.index("dag:20261029T090000Z-00000001") < text.index("spawn:locate_login_bug")


def test_the_instruction_states_the_update_shape_and_statuses():
    text = build_instruction(Board([], []), [])

    for word in (
        "new_title",
        "group",
        "text",
        "status",
        "tasks",
        "statuses",
        "active",
        "waiting",
        "blocked",
        "parked",
        "completed",
    ):
        assert word in text, word


async def test_the_session_log_is_left_alone(tmp_path):
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create(KEY)
    session.add_message("user", "Why is the page blank?", timestamp="2026-10-29T10:02:13")
    sessions.save(session)
    before = sessions.session_path(KEY).read_bytes()

    await _run(TaskBoardService(sessions, now_ms=lambda: 1), Scripted(GOOD))

    assert sessions.session_path(KEY).read_bytes() == before


async def test_nothing_dispatched_and_an_empty_update_writes_nothing(tmp_path):
    service = _service(tmp_path)

    board = await _run(service, Scripted(_update([])), dispatches=[])

    assert board is not None and board.groups == []
    assert not (service.session_dir(KEY) / BOARD_FILENAME).exists()


async def test_an_existing_board_is_extended_not_replaced(tmp_path):
    service = _service(tmp_path)
    existing = Board(
        [TaskGroup("g_old", "Release notes", "active", 1, 1, [ProgressStep("s", "T0", None, "Draft", [])])], []
    )
    save_board(service.session_dir(KEY), existing)

    await _run(service, Scripted(GOOD))

    assert [g.title for g in load_board(service.session_dir(KEY)).groups] == ["Release notes", "Fix bug A"]


async def test_an_unexpected_failure_still_parks_the_handles(tmp_path, monkeypatch):
    import raven.agent.task_board.runner as runner

    def boom(*_a, **_k):
        raise RuntimeError("disk said no")

    monkeypatch.setattr(runner, "apply_update", boom)
    service = _service(tmp_path)

    assert await _run(service, Scripted(GOOD)) is None

    assert [p.handle for p in load_board(service.session_dir(KEY)).unassigned] == [SPAWN.handle]


async def test_malformed_arguments_are_asked_again_not_raised(tmp_path):
    malformed = LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id="m", name="task_board", arguments={"action": "update", "steps": ["x"]})],
    )
    call = Scripted(malformed, GOOD)

    board = await _run(_service(tmp_path), call)

    assert board is not None
    assert call.seen[1][-1]["content"].startswith("Rejected: each step must be an object")


def _gated(reply: LLMResponse) -> tuple[asyncio.Event, object]:
    gate = asyncio.Event()

    async def call(_rows):
        await gate.wait()
        return reply

    return gate, call


async def test_reader_actions_do_not_wait_for_a_running_update(tmp_path):
    service = _service(tmp_path)
    old = TaskGroup("g_old", "Release notes", "active", 1, 1, [ProgressStep("s", "T0", None, "Draft", [])])
    save_board(service.session_dir(KEY), Board([old], []))
    gate, call = _gated(GOOD)
    run = asyncio.create_task(_run(service, call))
    await asyncio.sleep(0.01)

    board = await asyncio.wait_for(service.set_status(KEY, "g_old", "completed"), timeout=1)

    assert board.groups[0].status == "completed"
    gate.set()
    await run
    stored = load_board(service.session_dir(KEY))
    assert [(g.title, g.status) for g in stored.groups] == [("Release notes", "completed"), ("Fix bug A", "waiting")]


async def test_an_update_for_an_undone_turn_is_dropped(tmp_path):
    service = _service(tmp_path)
    gate, call = _gated(GOOD)
    run = asyncio.create_task(_run(service, call, turn_at="T9"))
    await asyncio.sleep(0.01)

    await asyncio.wait_for(service.drop_turns(KEY, {"T9"}), timeout=1)
    gate.set()

    assert await run is None
    stored = load_board(service.session_dir(KEY))
    assert stored.groups == [] and stored.unassigned == []


async def test_an_update_that_outlives_a_clear_is_dropped(tmp_path):
    service = _service(tmp_path)
    gate, call = _gated(GOOD)
    run = asyncio.create_task(_run(service, call))
    await asyncio.sleep(0.01)

    await asyncio.wait_for(service.reset(KEY), timeout=1)
    gate.set()

    assert await run is None
    assert load_board(service.session_dir(KEY)).groups == []
