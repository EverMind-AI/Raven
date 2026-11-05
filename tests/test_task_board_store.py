"""The Task Board's model and file store: what an update may say, and what survives a write."""

from __future__ import annotations

import json

import pytest
from loguru import logger

from raven.agent.task_board.board import (
    GROUP_SUMMARY_WORDS,
    TITLE_WORDS,
    Board,
    Dispatch,
    Pending,
    ProgressStep,
    TaskGroup,
    TaskLink,
    UpdateRejectedError,
    apply_carry,
    apply_update,
    board_from_dict,
    board_to_dict,
    confine_update,
    drop_turns,
    ignore_question,
    owning_groups,
    salvage,
    set_group_status,
    word_count,
)
from raven.agent.task_board.store import BOARD_FILENAME, copy_board, load_board, reset_board, save_board

SPAWN = Dispatch("spawn:locate_login_bug", "Locate the blank-page bug")
DAG = Dispatch("dag:20261029T100213Z-3eb3b470", "Review fixes")
NEW = {"new_title": "Fix bug A", "new_summary": "Blank page after login"}
STEP = {"title": "Moved it", "summary": "Moved it along."}
ASKED = ["Which fix should I apply?", "Should the stale session be cleared too?"]


def _counter():
    seen = {"n": 0}

    def mint(prefix: str) -> str:
        seen["n"] += 1
        return f"{prefix}_{seen['n']}"

    return mint


def _one_group(status: str = "idle") -> Board:
    return apply_update(
        Board([], []),
        {
            "steps": [
                {
                    "new_title": "Fix bug A",
                    "new_summary": "Blank page after login; the cause is found",
                    "title": "Found it",
                    "summary": "The restored session has an empty token; three fixes proposed.",
                    "status": status,
                    "tasks": [SPAWN.handle],
                    **({"questions": ASKED} if status == "needs_you" else {}),
                }
            ]
        },
        [SPAWN],
        turn_at="T1",
        turn_id="turn-1",
        now_ms=5,
        mint=_counter(),
    )


def _words(n: int) -> str:
    return " ".join(["word"] * n)


OVER_20 = r"; the limit is 20: "
OVER_100 = r"; the limit is 100: "


def test_new_title_opens_a_group_holding_the_step():
    board = _one_group(status="needs_you")

    group = board.groups[0]
    assert (group.id, group.title, group.summary, group.status, group.created_at, group.updated_at) == (
        "g_1",
        "Fix bug A",
        "Blank page after login; the cause is found",
        "needs_you",
        5,
        5,
    )
    step = group.steps[0]
    assert (step.id, step.turn_at, step.turn_id, step.title, step.summary) == (
        "s_2",
        "T1",
        "turn-1",
        "Found it",
        "The restored session has an empty token; three fixes proposed.",
    )
    assert step.tasks == [TaskLink("spawn", "locate_login_bug", "Locate the blank-page bug")]


def test_every_dispatch_is_placed_exactly_once():
    empty = Board([], [])
    one_step = {**NEW, **STEP, "status": "idle"}

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
            {"steps": [{**NEW, **STEP, "status": "idle", "tasks": [SPAWN.handle]}]},
            [SPAWN],
            turn_at="T1",
            turn_id=None,
            now_ms=1,
        )

    after = apply_update(
        board,
        {"steps": [{**NEW, **STEP, "status": "idle", "tasks": [SPAWN.handle, DAG.handle]}]},
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
        {
            "steps": [
                {"group": "g_1", "title": "Back on it", "summary": "The user found a second case.", "status": "idle"}
            ]
        },
        [],
        turn_at="T2",
        turn_id=None,
        now_ms=7,
    )
    assert (reopened.groups[0].status, reopened.groups[0].updated_at) == ("idle", 7)
    assert [s.title for s in reopened.groups[0].steps] == ["Found it", "Back on it"]

    still_done = apply_update(
        done,
        {"steps": [{"group": "g_1", **STEP, "status": "completed"}]},
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


def test_a_step_names_its_group_once_and_carries_a_title_and_a_summary():
    board = _one_group()

    def rejected(step: dict) -> str:
        with pytest.raises(UpdateRejectedError) as exc:
            apply_update(board, {"steps": [step]}, [], turn_at="T2", turn_id=None, now_ms=9)
        return str(exc.value)

    assert rejected({"group": "g_1", **NEW, **STEP, "status": "idle"}) == (
        "steps[0]: a step needs exactly one of group and new_title"
    )
    assert rejected({**STEP, "status": "idle"}) == "steps[0]: a step needs exactly one of group and new_title"
    assert rejected({"group": "g_x", **STEP, "status": "idle"}) == "steps[0]: unknown group: g_x"
    assert (
        rejected({"group": "g_1", **STEP, "status": "done"})
        == "steps[0]: unknown status: done; use needs_you, running, idle or completed"
    )
    assert rejected({**NEW, "new_title": "   ", **STEP, "status": "idle"}) == "steps[0]: new_title is empty"
    assert rejected({"new_title": "Fix bug B", **STEP, "status": "idle"}) == "steps[0]: new_summary is empty"
    assert rejected({"group": "g_1", "new_summary": "x", **STEP, "status": "idle"}) == (
        "steps[0]: new_summary goes with new_title; change an existing group's summary in groups"
    )
    assert rejected({"group": "g_1", **STEP, "title": "  ", "status": "idle"}) == "steps[0]: title is empty"
    assert rejected({"group": "g_1", "title": "Moved it", "status": "idle"}) == "steps[0]: summary is empty"


def test_word_count_counts_each_word_of_a_spaced_script_once():
    assert word_count("Fix the blank page after login") == 6
    assert word_count("ui-web feat/composer-resizable-height") == 2
    assert word_count("v2.1 -- don't panic!") == 3
    assert word_count("") == 0
    assert word_count(" -- ") == 0


def test_titles_and_group_summaries_keep_to_their_word_limits():
    assert (TITLE_WORDS, GROUP_SUMMARY_WORDS) == (20, 100)
    board = _one_group()

    def apply(args: dict) -> Board:
        return apply_update(board, args, [], turn_at="T2", turn_id=None, now_ms=9)

    fits = apply(
        {
            "steps": [
                {
                    "new_title": _words(20),
                    "new_summary": _words(100),
                    "title": _words(20),
                    "summary": "x " * 2500,
                    "status": "idle",
                }
            ]
        }
    )
    assert fits.groups[1].steps[0].summary == ("x " * 2500).strip(), "a step's summary has no limit"

    for step, refusal in [
        ({**NEW, "new_title": _words(21), **STEP}, rf"^steps\[0\]: new_title is 21 words long{OVER_20}word word"),
        ({**NEW, "new_summary": _words(101), **STEP}, rf"^steps\[0\]: new_summary is 101 words long{OVER_100}word"),
        ({"group": "g_1", **STEP, "title": _words(21)}, rf"^steps\[0\]: title is 21 words long{OVER_20}word word"),
    ]:
        with pytest.raises(UpdateRejectedError, match=refusal):
            apply({"steps": [{**step, "status": "idle"}]})


def test_groups_change_a_group_s_own_title_summary_and_status():
    board = apply_update(
        _one_group(),
        {
            "steps": [
                {
                    "group": "g_1",
                    "title": "Fixed it",
                    "summary": "Patched the restore.",
                    "status": "needs_you",
                    "questions": ["Does the page load for you now?"],
                }
            ],
            "groups": [{"group": "g_1", "title": "Fix the blank page", "summary": "Fix in; the user checks it"}],
        },
        [],
        turn_at="T2",
        turn_id=None,
        now_ms=9,
    )
    group = board.groups[0]
    assert (group.title, group.summary, group.status, group.updated_at) == (
        "Fix the blank page",
        "Fix in; the user checks it",
        "needs_you",
        9,
    )
    assert [s.title for s in group.steps] == ["Found it", "Fixed it"]

    parked = apply_update(
        board,
        {"groups": [{"group": "g_1", "summary": "Put off until Monday", "status": "idle"}]},
        [],
        turn_at="T3",
        turn_id=None,
        now_ms=10,
    )
    group = parked.groups[0]
    assert (group.summary, group.status, group.updated_at, len(group.steps)) == (
        "Put off until Monday",
        "idle",
        10,
        2,
    )


def test_a_groups_entry_must_change_something_it_may_change():
    board = _one_group()

    def rejected(args: dict) -> str:
        with pytest.raises(UpdateRejectedError) as exc:
            apply_update(board, args, [], turn_at="T2", turn_id=None, now_ms=9)
        return str(exc.value)

    assert rejected({"groups": [{"group": "g_9", "summary": "x"}]}) == "groups[0]: unknown group: g_9"
    assert rejected({"groups": [{"group": "g_1"}]}) == (
        "groups[0]: a groups entry needs at least one of title, summary and status"
    )
    assert (
        rejected({"groups": [{"group": "g_1", "status": "done"}]})
        == "groups[0]: unknown status: done; use needs_you, running, idle or completed"
    )
    assert rejected({"groups": [{"group": "g_1", "summary": " "}]}) == "groups[0]: summary is empty"
    assert rejected({"groups": [{"group": "g_1", "summary": "a"}, {"group": "g_1", "title": "b"}]}) == (
        "groups[1]: g_1 appears twice in groups"
    )
    assert rejected(
        {"steps": [{"group": "g_1", **STEP, "status": "idle"}], "groups": [{"group": "g_1", "status": "idle"}]}
    ) == ("groups[0]: g_1 has a step this turn; set its status on the step")
    with pytest.raises(UpdateRejectedError, match=rf"^groups\[0\]: title is 21 words long{OVER_20}"):
        apply_update(
            board, {"groups": [{"group": "g_1", "title": _words(21)}]}, [], turn_at="T2", turn_id=None, now_ms=9
        )
    with pytest.raises(UpdateRejectedError, match=rf"^groups\[0\]: summary is 101 words long{OVER_100}"):
        apply_update(
            board, {"groups": [{"group": "g_1", "summary": _words(101)}]}, [], turn_at="T2", turn_id=None, now_ms=9
        )


def test_a_group_takes_one_step_per_update():
    """A second step would win the status and leave the first one's questions on a
    step the page no longer reads as the latest."""
    board = _one_group()
    args = {
        "steps": [
            {"group": "g_1", **STEP, "status": "needs_you", "questions": ASKED[:1]},
            {"group": "g_1", **STEP, "status": "running"},
        ]
    }

    with pytest.raises(UpdateRejectedError) as exc:
        apply_update(board, args, [], turn_at="T2", turn_id=None, now_ms=9)

    assert str(exc.value) == "steps[1]: g_1 appears twice in steps; give each group one step per turn"


def test_a_refusal_names_every_rule_the_update_breaks_not_only_the_first():
    """Told one fault per refusal, the model meets the next only on its next call, and
    three calls end the update: a fault behind an over-long summary surfaced on the last."""
    args = {
        "steps": [{"group": "g_1", **STEP, "status": "running", "tasks": [DAG.handle]}],
        "groups": [{"group": "g_1", "status": "running", "summary": _words(130)}],
    }

    with pytest.raises(UpdateRejectedError) as exc:
        apply_update(_one_group(), args, [DAG], turn_at="T2", turn_id=None, now_ms=9)

    first, second = str(exc.value).splitlines()
    assert first == "groups[0]: summary is 130 words long; the limit is 100: word word word word word..."
    assert second == "groups[0]: g_1 has a step this turn; set its status on the step"


def test_a_refusal_names_the_entry_each_fault_is_in():
    args = {"steps": [{"group": "g_1", **STEP, "status": "idle"}, {**NEW, **STEP, "title": " ", "status": "idle"}]}

    with pytest.raises(UpdateRejectedError) as exc:
        apply_update(_one_group(), args, [], turn_at="T2", turn_id=None, now_ms=9)

    assert str(exc.value) == "steps[1]: title is empty"


def test_a_step_of_an_unknown_status_is_not_also_refused_for_its_questions():
    """Questions are judged against the status; an unknown one has nothing to judge them by."""
    step = {"group": "g_1", **STEP, "status": "waiting", "questions": ["Which fix should I apply?"]}

    with pytest.raises(UpdateRejectedError) as exc:
        apply_update(_one_group(), {"steps": [step]}, [], turn_at="T2", turn_id=None, now_ms=9)

    assert str(exc.value) == "steps[0]: unknown status: waiting; use needs_you, running, idle or completed"


def test_a_group_of_the_wrong_type_is_refused_not_raised():
    args = {
        "steps": [{"group": ["g_1"], **STEP, "status": "idle"}],
        "groups": [{"group": {"id": "g_1"}, "summary": "Moved"}],
    }

    with pytest.raises(UpdateRejectedError) as exc:
        apply_update(_one_group(), args, [], turn_at="T2", turn_id=None, now_ms=9)

    assert str(exc.value).splitlines() == [
        "steps[0]: unknown group: ['g_1']",
        "groups[0]: unknown group: {'id': 'g_1'}",
    ]


def test_allow_long_takes_text_past_its_limit_as_written_and_lets_no_other_fault_by():
    args = {
        "steps": [
            {
                "group": "g_1",
                "title": _words(25),
                "summary": "Asked.",
                "status": "needs_you",
                "questions": [_words(110)],
            },
            {**NEW, "new_title": _words(22), "new_summary": _words(105), **STEP, "status": "idle"},
        ],
        "groups": [{"group": "g_1", "summary": _words(130)}],
    }

    after = apply_update(_one_group(), args, [], turn_at="T2", turn_id=None, now_ms=9, allow_long=True)

    fix, new = after.groups
    assert (fix.summary, fix.steps[-1].title, fix.steps[-1].questions) == (_words(130), _words(25), [_words(110)])
    assert (new.title, new.summary) == (_words(22), _words(105))
    with pytest.raises(UpdateRejectedError) as exc:
        apply_update(
            _one_group(),
            {**args, "groups": [{"group": "g_1", "status": "idle"}]},
            [],
            turn_at="T2",
            turn_id=None,
            now_ms=9,
            allow_long=True,
        )
    assert str(exc.value) == "groups[0]: g_1 has a step this turn; set its status on the step"


def test_a_salvage_keeps_the_step_and_its_work_and_drops_only_the_groups_field_at_fault():
    """A status the step already sets is let go; the summary written beside it stays."""
    board = _one_group()
    last = {
        "steps": [{"group": "g_1", **STEP, "status": "running", "tasks": [DAG.handle]}],
        "groups": [{"group": "g_1", "status": "running", "summary": _words(130)}],
    }

    kept = salvage(board, last)

    assert kept == {**last, "groups": [{"group": "g_1", "summary": _words(130)}]}
    group = apply_update(board, kept, [DAG], turn_at="T2", turn_id=None, now_ms=9, allow_long=True).groups[0]
    assert (group.status, group.summary, group.steps[-1].tasks[0].id) == ("running", _words(130), DAG.handle[4:])


def test_a_salvage_keeps_one_entry_per_group_and_none_for_a_group_the_board_lacks():
    last = {
        "steps": [],
        "groups": [
            {"group": "g_9", "summary": "Gone"},
            {"group": "g_1", "status": "needs_you", "title": "Fix the blank page"},
            {"group": "g_1", "summary": "A second entry"},
            "not an entry",
        ],
    }

    assert salvage(_one_group(), last)["groups"] == [{"group": "g_1", "title": "Fix the blank page"}]


def test_a_salvage_leaves_a_fault_in_a_step_or_a_placement_to_be_refused():
    board = _one_group()
    last = {"steps": [{"group": "g_1", **STEP, "status": "idle"}], "groups": [{"group": "g_1", "status": "idle"}]}

    kept = salvage(board, last)

    assert kept["groups"] == []
    with pytest.raises(UpdateRejectedError, match=r"^not placed: dag:20261029T100213Z-3eb3b470$"):
        apply_update(board, kept, [DAG], turn_at="T2", turn_id=None, now_ms=9, allow_long=True)


def test_set_group_status_completes_and_reopens():
    board = _one_group()

    done = set_group_status(board, "g_1", "completed", now_ms=20)
    assert (done.groups[0].status, done.groups[0].updated_at) == ("completed", 20)
    reopened = set_group_status(done, "g_1", "idle", now_ms=21)
    assert reopened.groups[0].status == "idle"

    with pytest.raises(KeyError):
        set_group_status(board, "g_9", "completed", now_ms=22)
    with pytest.raises(
        UpdateRejectedError, match=r"^unknown status: finished; use needs_you, running, idle or completed$"
    ):
        set_group_status(board, "g_1", "finished", now_ms=22)


def test_drop_turns_drops_undone_steps_and_groups_left_empty():
    board = apply_update(
        _one_group(),
        {
            "steps": [
                {"group": "g_1", **STEP, "status": "idle"},
                {
                    "new_title": "Release notes",
                    "new_summary": "Notes for 0.3",
                    **STEP,
                    "status": "needs_you",
                    "questions": ASKED,
                },
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
    assert data["groups"][0]["summary"] == "Blank page after login; the cause is found"
    assert data["groups"][0]["steps"][0]["summary"].startswith("The restored session")
    assert data["groups"][0]["steps"][0]["tasks"] == [
        {"kind": "spawn", "id": "locate_login_bug", "summary": "Locate the blank-page bug"}
    ]
    assert board_to_dict(board_from_dict(json.loads(json.dumps(data)))) == data


def test_a_board_written_before_titles_loads_its_step_text_as_title_and_summary():
    data = {
        "version": 1,
        "groups": [
            {
                "id": "g_1",
                "title": "Fix bug A",
                "status": "idle",
                "created_at": 1,
                "updated_at": 1,
                "steps": [{"id": "s_1", "turn_at": "T1", "turn_id": None, "text": "Found it", "tasks": []}],
            }
        ],
        "unassigned": [],
    }

    group = board_from_dict(data).groups[0]

    assert group.summary == ""
    assert (group.steps[0].title, group.steps[0].summary) == ("Found it", "Found it")


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
    assert any(BOARD_FILENAME in w and "set aside as" in w for w in warnings)


@pytest.mark.parametrize(
    "text", ["{not json", '{"groups": [{"title": "no id"}]}', "\udcff"], ids=["json", "shape", "bytes"]
)
def test_an_unreadable_board_is_set_aside_not_lost(tmp_path, text):
    """The next write would replace the only copy; set aside, it can still be read by hand."""
    path = tmp_path / BOARD_FILENAME
    path.write_bytes(text.encode("utf-8", "surrogateescape"))

    assert load_board(tmp_path).groups == []
    save_board(tmp_path, _one_group())

    aside = list(tmp_path.glob(f"{BOARD_FILENAME}.unreadable-*"))
    assert [p.read_bytes() for p in aside] == [text.encode("utf-8", "surrogateescape")]
    assert load_board(tmp_path).groups[0].title == "Fix bug A"


def test_a_board_that_cannot_be_opened_is_left_where_it_is(tmp_path):
    """Not a bad file but a bad read: there may be nothing wrong with it."""
    (tmp_path / BOARD_FILENAME).mkdir()

    assert load_board(tmp_path).groups == []
    assert (tmp_path / BOARD_FILENAME).is_dir()
    assert list(tmp_path.glob(f"{BOARD_FILENAME}.unreadable-*")) == []


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
    assert rejected({"groups": "g_1"}) == "groups must be a list"
    assert rejected({"groups": ["g_1"]}) == "each groups entry must be an object"
    assert rejected({"steps": [{"group": "g_1", **STEP, "status": "idle", "tasks": "spawn:a"}]}) == (
        "steps[0]: tasks must be a list of handles"
    )


def test_drop_turns_removes_only_the_named_turns():
    board = apply_update(
        _one_group(),
        {"steps": [{"group": "g_1", **STEP, "status": "idle"}]},
        [],
        turn_at="T2",
        turn_id=None,
        now_ms=9,
    )

    after = drop_turns(board, {"T2"})

    assert [s.turn_at for s in after.groups[0].steps] == ["T1"]
    assert drop_turns(board, {"T9"}).groups == board.groups


def _asked_on_t2(board: Board) -> Board:
    """T2 leaves the T1 group waiting on the user, under a summary it rewrote."""
    return apply_update(
        board,
        {
            "steps": [{"group": "g_1", **STEP, "status": "needs_you", "questions": ASKED[:1]}],
            "groups": [{"group": "g_1", "summary": "Cause found; which fix?"}],
        },
        [],
        turn_at="T2",
        turn_id=None,
        now_ms=9,
    )


def test_an_undo_puts_back_what_the_undone_turn_changed_on_a_group():
    before = _one_group()

    undone = drop_turns(_asked_on_t2(before), {"T2"})

    group = undone.groups[0]
    assert (group.status, group.summary, group.updated_at) == ("idle", "Blank page after login; the cause is found", 5)
    assert board_to_dict(undone, revisions=True) == board_to_dict(before, revisions=True)


def test_an_undo_keeps_what_the_reader_set_after_the_undone_turn():
    completed = set_group_status(_asked_on_t2(_one_group()), "g_1", "completed", now_ms=12)

    group = drop_turns(completed, {"T2"}).groups[0]

    assert (group.status, group.updated_at) == ("completed", 12)
    assert group.summary == "Blank page after login; the cause is found"


def test_an_undo_puts_back_an_older_steps_questions_the_carry_changed():
    before = _one_group("needs_you")
    settled = apply_carry(before, {"groups": [{"group": "g_1", "questions": []}]}, ["g_1"], turn_at="T2", now_ms=9)

    undone = drop_turns(settled, {"T2"})

    group = undone.groups[0]
    assert (group.status, group.steps[0].questions) == ("needs_you", ASKED)
    assert board_to_dict(undone, revisions=True) == board_to_dict(before, revisions=True)


def test_an_undo_removes_a_question_the_carry_added_to_an_older_step():
    before = _one_group("needs_you")
    added = apply_carry(
        before,
        {"groups": [{"group": "g_1", "questions": [*ASKED, "Which branch?"]}]},
        ["g_1"],
        turn_at="T2",
        now_ms=9,
    )

    undone = drop_turns(added, {"T2"})

    assert undone.groups[0].steps[0].questions == ASKED


def test_an_undo_does_not_overwrite_a_question_the_reader_set_aside_after():
    before = _one_group("needs_you")
    step_id = before.groups[0].steps[0].id
    narrowed = apply_carry(
        before, {"groups": [{"group": "g_1", "questions": ASKED[:1]}]}, ["g_1"], turn_at="T2", now_ms=9
    )
    reader = ignore_question(narrowed, "g_1", step_id, ASKED[0], now_ms=10)

    undone = drop_turns(reader, {"T2"})

    assert undone.groups[0].steps[0].questions == []


def test_an_undo_puts_back_a_groups_entry_the_turn_made_without_a_step():
    renamed = apply_update(
        _one_group(),
        {"groups": [{"group": "g_1", "title": "Fix the login bug"}]},
        [],
        turn_at="T2",
        turn_id=None,
        now_ms=9,
    )

    group = drop_turns(renamed, {"T2"}).groups[0]

    assert (group.title, group.updated_at) == ("Fix bug A", 5)


def test_an_undo_of_two_turns_unwinds_both():
    t3 = apply_update(
        _asked_on_t2(_one_group()),
        {
            "steps": [{"group": "g_1", **STEP, "status": "running"}],
            "groups": [{"group": "g_1", "title": "Fix the login bug"}],
        },
        [],
        turn_at="T3",
        turn_id=None,
        now_ms=11,
    )

    group = drop_turns(t3, {"T2", "T3"}).groups[0]

    assert (group.title, group.summary, group.status, group.updated_at) == (
        "Fix bug A",
        "Blank page after login; the cause is found",
        "idle",
        5,
    )


def test_revisions_live_in_the_file_and_stay_off_the_wire(tmp_path):
    board = _asked_on_t2(_one_group())

    save_board(tmp_path, board)

    assert "revisions" not in board_to_dict(board)["groups"][0]
    assert load_board(tmp_path).groups[0].revisions == board.groups[0].revisions
    assert drop_turns(load_board(tmp_path), {"T2"}).groups[0].status == "idle"


def test_a_group_with_no_status_on_file_reads_idle():
    data = board_to_dict(_one_group())
    del data["groups"][0]["status"]

    assert board_from_dict(data).groups[0].status == "idle"


@pytest.mark.parametrize("former", ["active", "waiting", "blocked", "parked"])
def test_a_status_from_before_the_four_is_refused(former):
    board = _one_group()

    with pytest.raises(
        UpdateRejectedError, match=rf"^steps\[0\]: unknown status: {former}; use needs_you, running, idle or completed$"
    ):
        apply_update(
            board,
            {"steps": [{"group": board.groups[0].id, **STEP, "status": former}]},
            [],
            turn_at="T2",
            turn_id=None,
            now_ms=9,
        )


def test_needs_you_goes_on_a_step_never_in_groups():
    with pytest.raises(
        UpdateRejectedError, match=r"^groups\[0\]: needs_you goes on a step, with the questions it asks$"
    ):
        apply_update(
            _one_group(),
            {"groups": [{"group": "g_1", "status": "needs_you"}]},
            [],
            turn_at="T2",
            turn_id=None,
            now_ms=9,
        )


def test_a_needs_you_step_keeps_the_questions_its_reply_asked():
    board = apply_update(
        Board([], []),
        {"steps": [{**NEW, **STEP, "status": "needs_you", "questions": [f" {q} " for q in ASKED]}]},
        [],
        turn_at="T1",
        turn_id=None,
        now_ms=1,
    )

    assert board.groups[0].steps[0].questions == ASKED
    assert board_to_dict(board)["groups"][0]["steps"][0]["questions"] == ASKED
    assert board_from_dict(board_to_dict(board)).groups[0].steps[0].questions == ASKED


def test_a_step_from_before_questions_reads_none():
    data = board_to_dict(_one_group())
    del data["groups"][0]["steps"][0]["questions"]

    assert board_from_dict(data).groups[0].steps[0].questions == []


@pytest.mark.parametrize(
    ("step", "refusal"),
    [
        (
            {"status": "needs_you"},
            r"^steps\[0\]: a needs_you step lists the questions its reply asked the user in questions$",
        ),
        (
            {"status": "needs_you", "questions": []},
            r"^steps\[0\]: a needs_you step lists the questions its reply asked the user in questions$",
        ),
        (
            {"status": "running", "questions": ["Which fix?"]},
            r"^steps\[0\]: questions go only with needs_you: a reply that asks the user something is needs_you$",
        ),
        ({"status": "needs_you", "questions": "Which fix?"}, r"^steps\[0\]: questions must be a list of questions$"),
        (
            {"status": "needs_you", "questions": ["Which fix?", 3]},
            r"^steps\[0\]: questions must be a list of questions$",
        ),
        ({"status": "needs_you", "questions": ["Which fix?", " "]}, r"^steps\[0\]: questions\[1\] is empty$"),
        (
            {"status": "needs_you", "questions": [_words(101)]},
            rf"^steps\[0\]: questions\[0\] is 101 words long{OVER_100}word word",
        ),
    ],
)
def test_questions_come_with_needs_you_and_only_with_it(step, refusal):
    with pytest.raises(UpdateRejectedError, match=refusal):
        apply_update(Board([], []), {"steps": [{**NEW, **STEP, **step}]}, [], turn_at="T1", turn_id=None, now_ms=1)


def test_a_question_of_a_hundred_words_is_kept():
    question = _words(100)

    board = apply_update(
        Board([], []),
        {"steps": [{**NEW, **STEP, "status": "needs_you", "questions": [question]}]},
        [],
        turn_at="T1",
        turn_id=None,
        now_ms=1,
    )

    assert board.groups[0].steps[0].questions == [question]


def test_one_update_moves_several_groups_at_once():
    def matter(gid: str, status: str, questions=()) -> TaskGroup:
        step = ProgressStep(f"s_{gid}", "T0", None, "Earlier", "Earlier.", [], list(questions))
        return TaskGroup(gid, f"Matter {gid}", f"Where {gid} stands", status, 1, 1, [step])

    board = Board([matter("g_a", "idle"), matter("g_b", "running"), matter("g_c", "needs_you", ["Bump it now?"])], [])

    out = apply_update(
        board,
        {
            "steps": [
                {"group": "g_a", **STEP, "status": "needs_you", "questions": ["Which fix?"]},
                {"group": "g_b", **STEP, "status": "completed"},
                {**NEW, **STEP, "status": "running"},
            ],
            "groups": [{"group": "g_c", "status": "idle"}],
        },
        [],
        turn_at="T1",
        turn_id=None,
        now_ms=9,
    )

    new = next(g for g in out.groups if g.id not in {"g_a", "g_b", "g_c"})
    assert {g.id: g.status for g in out.groups} == {
        "g_a": "needs_you",
        "g_b": "completed",
        "g_c": "idle",
        new.id: "running",
    }
    assert [(len(g.steps), g.updated_at) for g in out.groups] == [(2, 9), (2, 9), (1, 9), (1, 9)]


def _asking(*questions_per_step: list[str]) -> Board:
    steps = [
        ProgressStep(f"s_{i}", "T0", None, "Asked", "Asked.", [], list(q)) for i, q in enumerate(questions_per_step)
    ]
    return Board([TaskGroup("g_1", "Fix bug A", "Where it stands", "needs_you", 1, 1, steps)], [])


def test_ignoring_a_question_sets_it_aside_and_the_last_one_leaves_the_group_idle():
    board = _asking(["Which fix?", "Clear the session too?"])

    once = ignore_question(board, "g_1", "s_0", "Which fix?", now_ms=9)
    assert (once.groups[0].steps[0].questions, once.groups[0].status) == (["Clear the session too?"], "needs_you")
    assert board.groups[0].steps[0].questions == ["Which fix?", "Clear the session too?"]

    twice = ignore_question(once, "g_1", "s_0", "Clear the session too?", now_ms=10)
    assert (twice.groups[0].steps[0].questions, twice.groups[0].status, twice.groups[0].updated_at) == ([], "idle", 10)


def test_a_question_set_aside_on_an_older_step_leaves_the_latest_one_waiting():
    board = _asking(["An older one?"], ["Which fix?"])

    out = ignore_question(board, "g_1", "s_0", "An older one?", now_ms=9)

    assert [s.questions for s in out.groups[0].steps] == [[], ["Which fix?"]]
    assert out.groups[0].status == "needs_you"


def test_a_question_the_step_no_longer_lists_changes_nothing():
    board = _asking(["Which fix?"])

    assert ignore_question(board, "g_1", "s_0", "Gone already?", now_ms=9) is board


@pytest.mark.parametrize(("group", "step"), [("g_x", "s_0"), ("g_1", "s_x")])
def test_setting_aside_a_question_of_an_unknown_group_or_step_is_a_key_error(group, step):
    with pytest.raises(KeyError):
        ignore_question(_asking(["Which fix?"]), group, step, "Which fix?", now_ms=9)


def test_a_question_asked_twice_is_set_aside_only_on_the_step_named():
    board = _asking(["Which fix?"], ["Which fix?"])

    out = ignore_question(board, "g_1", "s_0", "Which fix?", now_ms=9)

    assert [s.questions for s in out.groups[0].steps] == [[], ["Which fix?"]]
    assert out.groups[0].status == "needs_you"


def _two_matters() -> Board:
    """g_1 placed the subagent run, g_2 the graph run; each is its own matter."""
    fix = _one_group().groups[0]
    release = TaskGroup(
        "g_2",
        "Release notes",
        "Drafting the notes",
        "running",
        1,
        1,
        [
            ProgressStep(
                "s_9", "T0", None, "Sent a review", "Reviewing.", [TaskLink("dag", DAG.handle[4:], DAG.summary)]
            )
        ],
    )
    return Board([fix, release], [])


def test_a_delegated_result_belongs_to_the_group_whose_step_placed_its_run():
    board = _two_matters()

    assert owning_groups(board, [SPAWN.handle], []) == ["g_1"]
    assert owning_groups(board, [DAG.handle], []) == ["g_2"]
    assert owning_groups(board, ["spawn:" + DAG.handle[4:]], []) == [], "a handle is its kind and its id"
    assert owning_groups(board, ["spawn:never_placed"], []) == [], "a run still unassigned names no group"


def test_a_moved_on_group_belongs_to_the_turn_while_the_board_holds_it():
    board = _two_matters()

    assert owning_groups(board, [], ["g_2"]) == ["g_2"]
    assert owning_groups(board, [], ["g_gone"]) == []
    assert owning_groups(board, [DAG.handle], ["g_1"]) == ["g_1", "g_2"], "the board's order, each once"


def test_a_confined_update_drops_what_it_writes_on_another_matter():
    own = {"group": "g_1", **STEP, "status": "idle"}
    args = {
        "action": "update",
        "steps": [own, {"group": "g_2", **STEP, "status": "idle"}, {**NEW, **STEP, "status": "idle"}],
        "groups": [{"group": "g_1", "summary": "Fixed."}, {"group": "g_2", "summary": "Rewritten"}],
    }

    kept = confine_update(args, ["g_1"])

    assert kept == {"action": "update", "steps": [own], "groups": [{"group": "g_1", "summary": "Fixed."}]}
    assert len(args["steps"]) == 3, "the arguments handed in are left as they were"


def test_a_confined_update_keeps_every_step_that_places_work_and_that_group_s_own_fields():
    """Work the turn started has to land somewhere, a new matter or another included;
    the group it lands on is then one this turn moved."""
    elsewhere = {"group": "g_2", **STEP, "status": "running", "tasks": [SPAWN.handle]}
    opened = {**NEW, **STEP, "status": "running", "tasks": [DAG.handle]}
    args = {"steps": [elsewhere, opened], "groups": [{"group": "g_2", "title": "Release notes, v2"}]}

    assert confine_update(args, ["g_1"]) == args


def test_an_update_with_no_owning_group_is_left_whole():
    args = {"steps": [{"group": "g_2", **STEP, "status": "idle"}, {**NEW, **STEP, "status": "idle"}]}

    assert confine_update(args, []) == args


def test_a_malformed_update_reaches_the_rules_unconfined():
    """The model is told what is wrong with what it sent, not with what was left of it."""
    assert confine_update({"steps": "one step"}, ["g_1"]) == {"steps": "one step"}
    assert confine_update({"steps": ["x"], "groups": "g_2"}, ["g_1"]) == {"steps": ["x"], "groups": "g_2"}
