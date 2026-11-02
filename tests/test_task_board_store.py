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
    apply_update,
    board_from_dict,
    board_to_dict,
    drop_turns,
    ignore_question,
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
        "a step needs exactly one of group and new_title"
    )
    assert rejected({**STEP, "status": "idle"}) == "a step needs exactly one of group and new_title"
    assert rejected({"group": "g_x", **STEP, "status": "idle"}) == "unknown group: g_x"
    assert (
        rejected({"group": "g_1", **STEP, "status": "done"})
        == "unknown status: done; use needs_you, running, idle or completed"
    )
    assert rejected({**NEW, "new_title": "   ", **STEP, "status": "idle"}) == "new_title is empty"
    assert rejected({"new_title": "Fix bug B", **STEP, "status": "idle"}) == "new_summary is empty"
    assert rejected({"group": "g_1", "new_summary": "x", **STEP, "status": "idle"}) == (
        "new_summary goes with new_title; change an existing group's summary in groups"
    )
    assert rejected({"group": "g_1", **STEP, "title": "  ", "status": "idle"}) == "title is empty"
    assert rejected({"group": "g_1", "title": "Moved it", "status": "idle"}) == "summary is empty"


def test_word_count_counts_each_word_of_a_spaced_script_once():
    assert word_count("Fix the blank page after login") == 6
    assert word_count("ui-web feat/composer-resizable-height") == 2
    assert word_count("v2.1 -- don't panic!") == 3
    assert word_count("") == 0
    assert word_count(" -- ") == 0


def test_titles_and_group_summaries_keep_to_their_word_limits():
    assert (TITLE_WORDS, GROUP_SUMMARY_WORDS) == (20, 50)
    board = _one_group()

    def apply(args: dict) -> Board:
        return apply_update(board, args, [], turn_at="T2", turn_id=None, now_ms=9)

    fits = apply(
        {
            "steps": [
                {
                    "new_title": _words(20),
                    "new_summary": _words(50),
                    "title": _words(20),
                    "summary": "x " * 2500,
                    "status": "idle",
                }
            ]
        }
    )
    assert fits.groups[1].steps[0].summary == ("x " * 2500).strip(), "a step's summary has no limit"

    for step, refusal in [
        ({**NEW, "new_title": _words(21), **STEP}, r"^new_title is 21 words long; the limit is 20: word word"),
        ({**NEW, "new_summary": _words(51), **STEP}, r"^new_summary is 51 words long; the limit is 50: word word"),
        ({"group": "g_1", **STEP, "title": _words(21)}, r"^title is 21 words long; the limit is 20: word word"),
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

    assert rejected({"groups": [{"group": "g_9", "summary": "x"}]}) == "unknown group: g_9"
    assert rejected({"groups": [{"group": "g_1"}]}) == "a groups entry needs at least one of title, summary and status"
    assert (
        rejected({"groups": [{"group": "g_1", "status": "done"}]})
        == "unknown status: done; use needs_you, running, idle or completed"
    )
    assert rejected({"groups": [{"group": "g_1", "summary": " "}]}) == "groups summary is empty"
    assert rejected({"groups": [{"group": "g_1", "summary": "a"}, {"group": "g_1", "title": "b"}]}) == (
        "g_1 appears twice in groups"
    )
    assert rejected(
        {"steps": [{"group": "g_1", **STEP, "status": "idle"}], "groups": [{"group": "g_1", "status": "idle"}]}
    ) == ("g_1 has a step this turn; set its status on the step")
    with pytest.raises(UpdateRejectedError, match=r"^groups title is 21 words long; the limit is 20: "):
        apply_update(
            board, {"groups": [{"group": "g_1", "title": _words(21)}]}, [], turn_at="T2", turn_id=None, now_ms=9
        )
    with pytest.raises(UpdateRejectedError, match=r"^groups summary is 51 words long; the limit is 50: "):
        apply_update(
            board, {"groups": [{"group": "g_1", "summary": _words(51)}]}, [], turn_at="T2", turn_id=None, now_ms=9
        )


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
    assert rejected({"groups": "g_1"}) == "groups must be a list"
    assert rejected({"groups": ["g_1"]}) == "each groups entry must be an object"
    assert rejected({"steps": [{"group": "g_1", **STEP, "status": "idle", "tasks": "spawn:a"}]}) == (
        "tasks must be a list of handles"
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


def test_a_group_with_no_status_on_file_reads_idle():
    data = board_to_dict(_one_group())
    del data["groups"][0]["status"]

    assert board_from_dict(data).groups[0].status == "idle"


@pytest.mark.parametrize("former", ["active", "waiting", "blocked", "parked"])
def test_a_status_from_before_the_four_is_refused(former):
    board = _one_group()

    with pytest.raises(
        UpdateRejectedError, match=rf"^unknown status: {former}; use needs_you, running, idle or completed$"
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
    with pytest.raises(UpdateRejectedError, match=r"^g_1: needs_you goes on a step, with the questions it asks$"):
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
        ({"status": "needs_you"}, r"^a needs_you step lists the questions its reply asked the user in questions$"),
        (
            {"status": "needs_you", "questions": []},
            r"^a needs_you step lists the questions its reply asked the user in questions$",
        ),
        (
            {"status": "running", "questions": ["Which fix?"]},
            r"^questions go only with needs_you: a reply that asks the user something is needs_you$",
        ),
        ({"status": "needs_you", "questions": "Which fix?"}, r"^questions must be a list of questions$"),
        ({"status": "needs_you", "questions": ["Which fix?", 3]}, r"^questions must be a list of questions$"),
        ({"status": "needs_you", "questions": ["Which fix?", " "]}, r"^a question is empty$"),
        (
            {"status": "needs_you", "questions": [_words(51)]},
            r"^a question is 51 words long; the limit is 50: word word",
        ),
    ],
)
def test_questions_come_with_needs_you_and_only_with_it(step, refusal):
    with pytest.raises(UpdateRejectedError, match=refusal):
        apply_update(Board([], []), {"steps": [{**NEW, **STEP, **step}]}, [], turn_at="T1", turn_id=None, now_ms=1)


def test_a_question_of_fifty_words_is_kept():
    question = _words(50)

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
