"""The Task Board's model and file store: what an update may say, and what survives a write."""

from __future__ import annotations

import json

import pytest
from loguru import logger

from raven.agent.task_board.board import (
    TEXT_MAX,
    TITLE_MAX,
    Board,
    Dispatch,
    Pending,
    TaskLink,
    UpdateRejectedError,
    apply_update,
    board_from_dict,
    board_to_dict,
    drop_turns,
    set_group_status,
)
from raven.agent.task_board.store import BOARD_FILENAME, copy_board, load_board, reset_board, save_board

SPAWN = Dispatch("spawn:locate_login_bug", "Locate the blank-page bug")
DAG = Dispatch("dag:20261029T100213Z-3eb3b470", "Review fixes")


def _counter():
    seen = {"n": 0}

    def mint(prefix: str) -> str:
        seen["n"] += 1
        return f"{prefix}_{seen['n']}"

    return mint


def _one_group(status: str = "active") -> Board:
    return apply_update(
        Board([], []),
        {"steps": [{"new_title": "Fix bug A", "text": "Found it", "status": status, "tasks": [SPAWN.handle]}]},
        [SPAWN],
        turn_at="T1",
        turn_id="turn-1",
        now_ms=5,
        mint=_counter(),
    )


def test_new_title_opens_a_group_holding_the_step():
    board = _one_group(status="waiting")

    group = board.groups[0]
    assert (group.id, group.title, group.status, group.created_at, group.updated_at) == (
        "g_1",
        "Fix bug A",
        "waiting",
        5,
        5,
    )
    step = group.steps[0]
    assert (step.id, step.turn_at, step.turn_id, step.text) == ("s_2", "T1", "turn-1", "Found it")
    assert step.tasks == [TaskLink("spawn", "locate_login_bug", "Locate the blank-page bug")]


def test_every_dispatch_is_placed_exactly_once():
    empty = Board([], [])
    one_step = {"new_title": "Fix bug A", "text": "x", "status": "active"}

    with pytest.raises(UpdateRejectedError) as missing:
        apply_update(empty, {"steps": [{**one_step, "tasks": []}]}, [SPAWN, DAG], turn_at="T1", turn_id=None, now_ms=1)
    assert str(missing.value) == "not placed: spawn:locate_login_bug, dag:20261029T100213Z-3eb3b470"

    with pytest.raises(UpdateRejectedError) as twice:
        apply_update(
            empty,
            {"steps": [{**one_step, "tasks": [SPAWN.handle]}, {**one_step, "tasks": [SPAWN.handle]}]},
            [SPAWN],
            turn_at="T1",
            turn_id=None,
            now_ms=1,
        )
    assert str(twice.value) == "placed more than once: spawn:locate_login_bug"

    with pytest.raises(UpdateRejectedError) as foreign:
        apply_update(
            empty,
            {"steps": [{**one_step, "tasks": [SPAWN.handle, "spawn:other"]}]},
            [SPAWN],
            turn_at="T1",
            turn_id=None,
            now_ms=1,
        )
    assert str(foreign.value) == "not dispatched this turn: spawn:other"


def test_unassigned_is_placed_with_the_turn_and_then_cleared():
    board = Board([], [Pending(DAG.handle, DAG.summary, "T0", None)])

    with pytest.raises(UpdateRejectedError, match=r"^not placed: dag:20261029T100213Z-3eb3b470$"):
        apply_update(
            board,
            {"steps": [{"new_title": "Fix bug A", "text": "x", "status": "active", "tasks": [SPAWN.handle]}]},
            [SPAWN],
            turn_at="T1",
            turn_id=None,
            now_ms=1,
        )

    after = apply_update(
        board,
        {"steps": [{"new_title": "Fix bug A", "text": "x", "status": "active", "tasks": [SPAWN.handle, DAG.handle]}]},
        [SPAWN],
        turn_at="T1",
        turn_id=None,
        now_ms=1,
    )
    assert after.unassigned == []
    assert after.groups[0].steps[0].tasks[1] == TaskLink("dag", "20261029T100213Z-3eb3b470", "Review fixes")
    assert board.unassigned, "the input board is never mutated"


def test_a_step_on_a_completed_group_reopens_it_unless_it_says_completed():
    done = set_group_status(_one_group(), "g_1", "completed", now_ms=6)
    assert done.groups[0].status == "completed"

    reopened = apply_update(
        done,
        {"steps": [{"group": "g_1", "text": "Back on it", "status": "active"}]},
        [],
        turn_at="T2",
        turn_id=None,
        now_ms=7,
    )
    assert (reopened.groups[0].status, reopened.groups[0].updated_at) == ("active", 7)
    assert [s.text for s in reopened.groups[0].steps] == ["Found it", "Back on it"]

    still_done = apply_update(
        done,
        {"steps": [{"group": "g_1", "text": "Archived the notes", "status": "completed"}]},
        [],
        turn_at="T2",
        turn_id=None,
        now_ms=7,
    )
    assert still_done.groups[0].status == "completed"


def test_steps_may_be_empty_only_when_nothing_was_dispatched():
    board = _one_group()

    with pytest.raises(UpdateRejectedError, match=r"^steps may be empty only when nothing was dispatched$"):
        apply_update(board, {"steps": []}, [DAG], turn_at="T2", turn_id=None, now_ms=9)

    unchanged = apply_update(board, {"steps": []}, [], turn_at="T2", turn_id=None, now_ms=9)
    assert board_to_dict(unchanged) == board_to_dict(board)


def test_group_and_new_title_are_exclusive_and_known():
    board = _one_group()

    def rejected(step: dict) -> str:
        with pytest.raises(UpdateRejectedError) as exc:
            apply_update(board, {"steps": [step]}, [], turn_at="T2", turn_id=None, now_ms=9)
        return str(exc.value)

    assert rejected({"group": "g_1", "new_title": "B", "text": "x", "status": "active"}) == (
        "a step needs exactly one of group and new_title"
    )
    assert rejected({"text": "x", "status": "active"}) == "a step needs exactly one of group and new_title"
    assert rejected({"group": "g_x", "text": "x", "status": "active"}) == "unknown group: g_x"
    assert rejected({"group": "g_1", "text": "x", "status": "done"}) == "unknown status: done"
    assert rejected({"new_title": "   ", "text": "x", "status": "active"}) == "new_title is empty"
    assert rejected({"group": "g_1", "text": "  ", "status": "active"}) == "text is empty"


def test_long_title_and_text_are_cut_to_their_caps():
    board = apply_update(
        Board([], []),
        {"steps": [{"new_title": "t" * 500, "text": "x" * 5000, "status": "active"}]},
        [],
        turn_at="T1",
        turn_id=None,
        now_ms=1,
    )
    assert len(board.groups[0].title) == TITLE_MAX
    assert len(board.groups[0].steps[0].text) == TEXT_MAX


def test_statuses_apply_after_steps():
    board = apply_update(
        _one_group(),
        {
            "steps": [{"group": "g_1", "text": "Merged", "status": "active"}],
            "statuses": [{"group": "g_1", "status": "completed"}],
        },
        [],
        turn_at="T2",
        turn_id=None,
        now_ms=9,
    )
    assert board.groups[0].status == "completed"

    with pytest.raises(UpdateRejectedError, match=r"^unknown group: g_9$"):
        apply_update(
            _one_group(), {"statuses": [{"group": "g_9", "status": "parked"}]}, [], turn_at="T2", turn_id=None, now_ms=9
        )


def test_a_retitle_renames_an_existing_group():
    board = apply_update(
        _one_group(),
        {"steps": [{"group": "g_1", "title": "Fix bug A: blank page", "text": "x", "status": "active"}]},
        [],
        turn_at="T2",
        turn_id=None,
        now_ms=9,
    )
    assert board.groups[0].title == "Fix bug A: blank page"


def test_set_group_status_completes_and_reopens():
    board = _one_group()

    done = set_group_status(board, "g_1", "completed", now_ms=20)
    assert (done.groups[0].status, done.groups[0].updated_at) == ("completed", 20)
    reopened = set_group_status(done, "g_1", "active", now_ms=21)
    assert reopened.groups[0].status == "active"

    with pytest.raises(KeyError):
        set_group_status(board, "g_9", "completed", now_ms=22)
    with pytest.raises(UpdateRejectedError, match=r"^unknown status: finished$"):
        set_group_status(board, "g_1", "finished", now_ms=22)


def test_drop_turns_drops_undone_steps_and_groups_left_empty():
    board = apply_update(
        _one_group(),
        {
            "steps": [
                {"group": "g_1", "text": "Second turn", "status": "active"},
                {"new_title": "Release notes", "text": "Draft", "status": "waiting"},
            ]
        },
        [],
        turn_at="T2",
        turn_id=None,
        now_ms=9,
        mint=_counter(),
    )

    board.unassigned = [Pending(DAG.handle, DAG.summary, "T2", None)]

    pruned = drop_turns(board, {"T2"})

    assert [g.title for g in pruned.groups] == ["Fix bug A"]
    assert [s.turn_at for s in pruned.groups[0].steps] == ["T1"]
    assert pruned.unassigned == []


def test_the_dict_shape_round_trips():
    board = _one_group()
    data = board_to_dict(board)

    assert data["version"] == 1
    assert data["groups"][0]["steps"][0]["tasks"] == [
        {"kind": "spawn", "id": "locate_login_bug", "summary": "Locate the blank-page bug"}
    ]
    assert board_to_dict(board_from_dict(json.loads(json.dumps(data)))) == data


def test_save_round_trips_and_leaves_no_temporary_file(tmp_path):
    board = _one_group()

    save_board(tmp_path, board)

    assert sorted(p.name for p in tmp_path.iterdir()) == [BOARD_FILENAME]
    assert board_to_dict(load_board(tmp_path)) == board_to_dict(board)


def test_a_missing_file_loads_as_an_empty_board(tmp_path):
    assert board_to_dict(load_board(tmp_path / "nowhere")) == {"version": 1, "groups": [], "unassigned": []}


def test_a_corrupt_file_loads_as_an_empty_board_and_says_so(tmp_path):
    (tmp_path / BOARD_FILENAME).write_text("{not json", encoding="utf-8")
    warnings: list[str] = []
    sink = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        board = load_board(tmp_path)
    finally:
        logger.remove(sink)

    assert board.groups == [] and board.unassigned == []
    assert any(BOARD_FILENAME in w for w in warnings)


def test_reset_and_copy(tmp_path):
    src, dst = tmp_path / "a", tmp_path / "b"
    save_board(src, _one_group())

    copy_board(src, dst)
    assert board_to_dict(load_board(dst)) == board_to_dict(load_board(src))

    reset_board(src)
    assert load_board(src).groups == []
    copy_board(tmp_path / "missing", tmp_path / "c")
    assert not (tmp_path / "c" / BOARD_FILENAME).exists()


def test_malformed_updates_are_refused_not_raised():
    board = _one_group()

    def rejected(args: dict) -> str:
        with pytest.raises(UpdateRejectedError) as exc:
            apply_update(board, args, [], turn_at="T2", turn_id=None, now_ms=9)
        return str(exc.value)

    assert rejected({"steps": {"group": "g_1"}}) == "steps must be a list"
    assert rejected({"steps": ["advanced the bug"]}) == "each step must be an object"
    assert rejected({"statuses": "g_1"}) == "statuses must be a list"
    assert rejected({"statuses": ["g_1"]}) == "each status change must be an object"
    assert rejected({"steps": [{"group": "g_1", "text": "x", "status": "active", "tasks": "spawn:a"}]}) == (
        "tasks must be a list of handles"
    )


def test_drop_turns_removes_only_the_named_turns():
    board = apply_update(
        _one_group(),
        {"steps": [{"group": "g_1", "text": "Second turn", "status": "active"}]},
        [],
        turn_at="T2",
        turn_id=None,
        now_ms=9,
    )

    after = drop_turns(board, {"T2"})

    assert [s.turn_at for s in after.groups[0].steps] == ["T1"]
    assert drop_turns(board, {"T9"}).groups == board.groups
