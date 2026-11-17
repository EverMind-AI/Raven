"""Pins which groups a turn's update must answer for, and the rules its `matters` answers meet.

Candidates: the turn's owners first, then every group its own words name by id, by a linked
Task or by a title anchor. Rules: every candidate answered, a moved group stepped, a step on an
existing group answered moved, and each `evidence` found word for word in the turn's words.
"""

from __future__ import annotations

import time

from raven.agent.task_board.board import Board, ProgressStep, TaskGroup, TaskLink
from raven.agent.task_board.matters import anchors, candidates, matters_problems, normalized, quoted


def test_anchors_are_the_identifying_tokens_of_a_title():
    assert anchors("Review PR #1268 `merge_mid_turn` in ui-web/src/app.ts v2.3.1 for 4096") == [
        "#1268",
        "merge_mid_turn",
        "ui-web/src/app.ts",
        "v2.3.1",
        "4096",
    ]


def test_plain_words_are_not_anchors():
    assert anchors("Fix the login bug") == []
    assert anchors("Review PR #12 and e.g the notes") == ["#12"]


def _board() -> Board:
    """g_a placed spawn:locate_login_bug; g_b is "Release notes #1270"; g_c, "Ship 2.4.0", is done."""
    run = TaskLink("spawn", "locate_login_bug", "Locate the blank-page bug")
    fix = TaskGroup("g_a", "Fix bug A", "", "running", 1, 1, [ProgressStep("s_a", "T0", None, "Sent", ".", [run])])
    notes = TaskGroup("g_b", "Release notes #1270", "", "idle", 1, 1, [])
    ship = TaskGroup("g_c", "Ship 2.4.0", "", "completed", 1, 1, [])
    return Board([fix, notes, ship], [])


def test_owners_come_first_then_named_groups_in_board_order():
    assert candidates(_board(), ["g_b"], "shipping 2.4.0 now, and g_a is next") == ["g_b", "g_a", "g_c"]


def test_a_group_is_named_by_id_by_a_linked_task_and_by_a_title_anchor():
    assert candidates(_board(), [], "g_a moved") == ["g_a"]
    assert candidates(_board(), [], "locate_login_bug finished") == ["g_a"]
    assert candidates(_board(), [], "see #1270") == ["g_b"]


def test_an_anchor_glued_to_an_ascii_word_names_nothing():
    assert candidates(_board(), [], "PR#12700 and x1270y and g_ab") == []


def test_a_title_word_names_nothing():
    assert candidates(_board(), [], "the release notes look fine") == []


def test_an_owner_named_again_is_listed_once():
    assert candidates(_board(), ["g_a"], "g_a and g_a") == ["g_a"]


WORDS = "Merged **#1268** after `ci` went green.\n\nShould I also [close #1270](https://x/1270)?"


def test_a_quote_survives_markdown_whitespace_case_and_links():
    words = normalized(WORDS)
    assert quoted("merged #1268 after ci went green", words)
    assert quoted("Should I also close #1270?", words)
    assert quoted("Merged #1268 ... went green", words)
    assert quoted("Merged #1268 \u2026 went green", words)


def test_a_paraphrase_a_stitched_quote_and_a_short_piece_fail():
    words = normalized(WORDS)
    assert not quoted("#1268 was merged", words)
    assert not quoted("Merged #1268, should I also close #1270?", words)
    assert not quoted("abc ... xyz", words)
    assert not quoted("", words)


def _step(group: str) -> dict:
    return {"group": group, "title": "t", "summary": "s", "status": "idle"}


def _faults(args: dict, cands: list[str], known: set[str]) -> list[str]:
    return [str(p) for p in matters_problems(args, cands, known, WORDS)]


def test_every_candidate_needs_an_answer():
    args = {"steps": [_step("g_a")], "matters": [{"group": "g_a", "moved": True, "evidence": "merged #1268"}]}

    assert _faults(args, ["g_a", "g_b"], {"g_a", "g_b"}) == ["matters: answer g_b -- did the turn move it?"]


def test_moved_needs_a_step_and_a_step_needs_moved():
    args = {"steps": [_step("g_c")], "matters": [{"group": "g_a", "moved": True, "evidence": "merged #1268"}]}

    assert _faults(args, ["g_a"], {"g_a", "g_c"}) == [
        "matters says the turn moved g_a, but no step is on it",
        'g_c has a step, but matters does not say the turn moved it; add {"group": "g_c", "moved": true,'
        ' "evidence": "<quote>"} with this turn\'s words, or drop the step',
    ]


def test_evidence_must_be_this_turns_words():
    args = {"steps": [_step("g_a")], "matters": [{"group": "g_a", "moved": True, "evidence": "it shipped"}]}

    assert _faults(args, ["g_a"], {"g_a"}) == [
        "matters: the evidence for g_a is not in this turn's message or reply; quote this turn's own words,"
        " or answer moved: false"
    ]


def test_a_new_group_step_needs_no_entry_and_a_clean_update_passes():
    new = {"new_title": "x", "new_summary": "y", "title": "t", "summary": "s", "status": "idle"}
    args = {
        "steps": [_step("g_a"), new],
        "matters": [{"group": "g_a", "moved": True, "evidence": "merged #1268"}, {"group": "g_b", "moved": False}],
    }

    assert _faults(args, ["g_a", "g_b"], {"g_a", "g_b"}) == []


def test_a_malformed_matters_is_one_fault():
    for bad in (None, "g_a", [{"group": "g_a"}], [{"group": "g_a", "moved": "yes"}]):
        assert _faults({"steps": [], "matters": bad}, ["g_a"], {"g_a"}) == [
            'matters: must be a list of {"group": "<id>", "moved": true|false, "evidence": "<quote>"} entries,'
            " one per listed group"
        ]


def test_the_last_entry_for_a_group_is_the_one_that_counts():
    args = {
        "steps": [],
        "matters": [{"group": "g_a", "moved": True, "evidence": "merged #1268"}, {"group": "g_a", "moved": False}],
    }

    assert _faults(args, ["g_a"], {"g_a"}) == []


def _seconds(work) -> float:
    started = time.perf_counter()
    work()
    return time.perf_counter() - started


def test_a_long_run_in_a_title_is_read_in_linear_time():
    """A title is the model's text: a run that holds a start at every character but never
    completes a match must not cost a scan of the rest of the run per start."""
    assert _seconds(lambda: anchors("a." * 20_000)) < 1.0
    assert _seconds(lambda: anchors("a" * 40_000)) < 1.0


def test_a_long_run_of_unclosed_brackets_is_normalized_in_linear_time():
    assert _seconds(lambda: normalized("[" * 100_000)) < 1.0


def test_a_windows_path_in_a_title_is_an_anchor():
    assert anchors(r"Fix the ACP launcher in D:\projects\Raven\raven\acp_client") == [
        "D:/projects/Raven/raven/acp_client"
    ]
    assert anchors(r"Install under %LOCALAPPDATA%\raven") == ["%LOCALAPPDATA%/raven"]


def test_a_path_anchor_stops_at_the_colon_after_it():
    assert anchors("raven/agent/window: picture withdrawal") == ["raven/agent/window"]
    assert anchors("Crash at raven/agent/loop/turn_path.py:3772") == ["raven/agent/loop/turn_path.py", "3772"]
    assert anchors("Mount D:/x first") == ["D:/x"]
    assert anchors("See https://example.org/a/b") == ["example.org/a/b"]


def test_a_path_named_without_the_colon_after_it_names_its_group():
    board = Board(
        [
            TaskGroup("g_w", "raven/agent/window: picture withdrawal", "", "idle", 1, 1, []),
            TaskGroup("g_l", "Crash at raven/agent/loop/turn_path.py:3772", "", "idle", 1, 1, []),
        ],
        [],
    )

    assert candidates(board, [], "touched raven/agent/window and raven/agent/loop/turn_path.py") == ["g_w", "g_l"]


def test_a_path_names_its_group_with_either_separator():
    board = Board([TaskGroup("g_w", r"Port the launcher in C:\tools\raven", "", "idle", 1, 1, [])], [])

    assert candidates(board, [], r"ported the launcher in C:\tools\raven today") == ["g_w"]
    assert candidates(board, [], "ported the launcher in C:/tools/raven today") == ["g_w"]


def test_a_quote_matches_a_path_written_with_either_separator():
    assert quoted(r"C:\tools\raven", normalized("I ported C:/tools/raven today"))
    assert quoted("C:/tools/raven", normalized(r"I ported C:\tools\raven today"))
