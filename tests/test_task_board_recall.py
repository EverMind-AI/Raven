"""Reading the Task Board back: listing, one thread, keyword search, lineage and turn text."""

from __future__ import annotations

import json

from raven.agent.subagent.history import dag_root, session_history_root
from raven.agent.task_board.board import Board, ProgressStep, TaskGroup, TaskLink
from raven.agent.task_board.lineage import descendants, task_status
from raven.agent.task_board.recall import (
    COMPLETED_RECALL_WEIGHT,
    CUT,
    RUNNERS_UP,
    list_groups,
    search,
    thread_text,
    turn_texts,
)
from raven.stint.record import STINTS_DIRNAME, RoundRecord, StintRecord, StintStore, make_stint_id
from raven.utils.tokens import estimate_message_tokens


def _group(
    gid: str, title: str, status: str, created: int, texts: list[str], tasks=None, summary: str = ""
) -> TaskGroup:
    steps = [
        ProgressStep(f"s_{gid}_{i}", f"2026-10-29T10:0{i}:00", None, text, f"In detail: {text}.", [])
        for i, text in enumerate(texts)
    ]
    if tasks:
        steps[-1].tasks = tasks
    return TaskGroup(gid, title, summary, status, created, created, steps)


def _no_status(_link: TaskLink) -> str:
    return "unknown"


def test_the_constants_are_the_specs():
    assert (COMPLETED_RECALL_WEIGHT, RUNNERS_UP) == (0.5, 2)


def test_listing_puts_open_groups_before_completed_ones():
    board = Board(
        [
            _group("g_a", "Fix bug A", "completed", 1, ["Merged"]),
            _group("g_b", "Release notes", "idle", 2, ["Draft written"]),
            _group(
                "g_c",
                "Review PR 1130",
                "needs_you",
                3,
                ["Posted the review"],
                summary="Three findings, awaiting a word",
            ),
        ],
        [],
    )

    out = list_groups(board, now_ms=3 + 5 * 60_000)

    lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert [line.split()[1] for line in lines] == ["g_c", "g_b", "g_a"]
    assert "[needs_you] Review PR 1130 -- Three findings, awaiting a word" in lines[0]
    assert "Release notes -- Draft written" in lines[1], "a group with no summary yet shows its last step's title"
    assert "5m ago" in lines[0]


def test_an_empty_board_says_so():
    assert "empty" in list_groups(Board([], []), now_ms=0)


def test_a_thread_names_each_task_with_its_live_status(tmp_path):
    root = session_history_root(tmp_path)
    root.mkdir(parents=True)
    (root / "nodes.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "locate_login_bug": {"kind": "spawn", "status": "completed"},
                    "a1": {"kind": "dag", "run_id": "RUN_A", "status": "completed"},
                    "a2": {"kind": "dag", "run_id": "RUN_A", "status": "running"},
                },
                "runs": [],
            }
        ),
        encoding="utf-8",
    )
    spawn = TaskLink("spawn", "locate_login_bug", "Locate the blank-page bug")
    run = TaskLink("dag", "RUN_A", "Review fixes")
    group = _group("g_1", "Fix bug A", "idle", 1, ["Found it", "Fixing"], tasks=[spawn, run], summary="Fix under way")
    board = Board([group], [])

    out = thread_text(board, "g_1", status_of=lambda link: task_status(tmp_path, link), lineage={})

    assert out.splitlines()[0] == "g_1 [idle] Fix bug A -- Fix under way"
    assert "- 2026-10-29T10:00:00: Found it -- In detail: Found it." in out
    assert "2026-10-29T10:01:00" in out
    assert "spawn:locate_login_bug (Locate the blank-page bug): completed" in out
    assert "dag:RUN_A (Review fixes): running" in out
    assert task_status(tmp_path, TaskLink("spawn", "nobody", "")) == "unknown"


def test_a_thread_names_the_runs_that_continued_a_placed_one():
    run = TaskLink("dag", "RUN_A", "Review fixes")
    board = Board([_group("g_1", "Fix bug A", "idle", 1, ["Fixing"], tasks=[run])], [])

    out = thread_text(board, "g_1", status_of=_no_status, lineage={"RUN_B": "RUN_A", "RUN_C": "RUN_A"})

    assert "continued as dag:RUN_B, dag:RUN_C" in out


def test_search_reads_group_and_step_summaries_too():
    upgrade = TaskGroup(
        "g_3",
        "Upgrade uv",
        "",
        "idle",
        3,
        3,
        [ProgressStep("s", "T", None, "Bumped it", "Pinned the resolver after the cache broke", [])],
    )
    board = Board(
        [
            _group("g_1", "Fix bug A", "idle", 1, ["Patched"], summary="Waiting on the lockfile bump"),
            _group("g_2", "Release notes", "idle", 2, ["Drafted"]),
            upgrade,
        ],
        [],
    )

    assert search(board, "lockfile", status_of=_no_status, lineage={}).splitlines()[0].startswith("g_1 ")
    assert search(board, "resolver", status_of=_no_status, lineage={}).splitlines()[0].startswith("g_3 ")


def test_search_finds_a_matter_by_its_words():
    board = Board(
        [
            _group("g_1", "Fix the blank page after login", "idle", 1, ["The restored session has an empty token"]),
            _group("g_2", "Release notes 0.26", "idle", 2, ["Draft written, waiting on highlights"]),
        ],
        [],
    )

    assert search(board, "blank page", status_of=_no_status, lineage={}).splitlines()[0].startswith("g_1 ")
    assert search(board, "release notes", status_of=_no_status, lineage={}).splitlines()[0].startswith("g_2 ")


def test_a_completed_group_ranks_below_an_open_one_on_equal_text():
    board = Board(
        [
            _group("g_done", "Upgrade the lockfile", "completed", 2, ["Bumped uv.lock"]),
            _group("g_open", "Upgrade the lockfile", "idle", 1, ["Bumped uv.lock"]),
        ],
        [],
    )

    assert search(board, "lockfile", status_of=_no_status, lineage={}).splitlines()[0].startswith("g_open ")


def test_search_names_up_to_two_runners_up_by_id():
    board = Board([_group(f"g_{i}", f"Review pass {i}", "idle", i, ["review findings"]) for i in range(4)], [])

    out = search(board, "review", status_of=_no_status, lineage={})

    others = [line for line in out.splitlines() if line.startswith("Other matches:")]
    assert len(others) == 1 and others[0].count("g_") == RUNNERS_UP


def test_search_with_nothing_matching_says_so():
    board = Board([_group("g_1", "Fix bug A", "idle", 1, ["Found it"])], [])

    assert "Nothing on the board matches" in search(board, "kubernetes", status_of=_no_status, lineage={})


def test_replan_successors_and_later_stint_rounds_resolve_to_the_placed_run(tmp_path):
    runs = dag_root(tmp_path)
    for run_id, graph in {
        "RUN_A": {"replan": {"run_id": "RUN_B"}},
        "RUN_B": {"replan": {"run_id": "RUN_E"}},
        "RUN_E": {},
        "RUN_X": {},
    }.items():
        (runs / run_id).mkdir(parents=True)
        (runs / run_id / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    store = StintStore(runs / STINTS_DIRNAME)
    store.write(
        StintRecord(
            stint_id=make_stint_id(),
            playbook="build",
            spec={},
            rounds=[
                RoundRecord(index=1, run_id="RUN_A"),
                RoundRecord(index=2, run_id="RUN_C"),
                RoundRecord(index=3, run_id="RUN_D"),
            ],
        )
    )

    assert descendants(tmp_path, ["RUN_A"]) == {"RUN_B": "RUN_A", "RUN_E": "RUN_A", "RUN_C": "RUN_A", "RUN_D": "RUN_A"}
    assert descendants(tmp_path, ["RUN_X"]) == {}
    assert descendants(tmp_path / "elsewhere", ["RUN_A"]) == {}


def test_turn_texts_report_a_turn_the_log_no_longer_holds():
    log = [
        {"role": "user", "content": "find the bug", "timestamp": "T1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c"}], "timestamp": "T1a"},
        {"role": "tool", "content": "...", "tool_call_id": "c", "timestamp": "T1b"},
        {"role": "assistant", "content": "It is the empty token.", "timestamp": "T1c"},
        {"role": "user", "content": "fix it", "timestamp": "T2"},
        {"role": "assistant", "content": "Fixed, tests pass.", "timestamp": "T2a"},
    ]

    out = turn_texts(log, ["T1", "T9"])

    assert "find the bug" in out and "It is the empty token." in out
    assert "Fixed, tests pass." not in out
    assert "T9: gone from the log" in out


def _tokens(text: str) -> int:
    return estimate_message_tokens({"role": "user", "content": text})


def _turns(*turns: tuple[str, str, str]) -> list[dict]:
    log: list[dict] = []
    for at, asked, replied in turns:
        log += [
            {"role": "user", "content": asked, "timestamp": at},
            {"role": "assistant", "content": replied, "timestamp": f"{at}a"},
        ]
    return log


def test_turn_texts_cut_a_long_turn_rather_than_leave_out_the_turns_after_it():
    log = _turns(
        ("T1", "Review PR 1150.", "finding " * 3000),
        ("T2", "And PR 1127?", "Blocked on one approval."),
        ("T3", "Fix F5 now.", "Sent an agent to fix it."),
    )

    out = turn_texts(log, ["T1", "T2", "T3"], max_tokens=1000)

    assert "Review PR 1150." in out
    assert "Blocked on one approval." in out and "Sent an agent to fix it." in out
    assert out.count(CUT) == 1
    assert _tokens(out) <= 1000


def test_turn_texts_keep_short_turns_whole_and_split_the_rest_evenly():
    log = _turns(("T1", "first", "alpha " * 3000), ("T2", "second", "Short reply."), ("T3", "third", "omega " * 3000))

    out = turn_texts(log, ["T1", "T2", "T3"], max_tokens=1200)

    first, second, third = out.split("\n\n")
    assert second == "T2:\n  asked: second\n  replied: Short reply."
    assert first.endswith(CUT) and third.endswith(CUT)
    assert abs(_tokens(first) - _tokens(third)) <= 10
    assert 1100 < _tokens(out) <= 1200, "what the short turn leaves goes to the long ones"


def test_a_turn_s_share_is_split_between_its_question_and_its_reply():
    out = turn_texts(_turns(("T1", "pasted log " * 2000, "the cause " * 2000)), ["T1"], max_tokens=600)

    asked = out.split("\n  asked: ")[1].split("\n  replied: ")[0]
    replied = out.split("\n  replied: ")[1]
    assert asked.endswith(CUT) and replied.endswith(CUT)
    assert abs(_tokens(asked) - _tokens(replied)) <= 10
    assert _tokens(out) <= 600


def test_turn_texts_keep_the_newest_turns_when_too_many_to_cut_down():
    log = _turns(*[(f"T{i}", f"question {i} " + "x" * 2000, "answer " + "y" * 2000) for i in range(20)])

    out = turn_texts(log, [f"T{i}" for i in range(20)], max_tokens=1000)

    kept = [i for i in range(20) if f"question {i} " in out]
    assert kept == list(range(20 - len(kept), 20)) and 0 < len(kept) < 20
    assert out.startswith(f"[{20 - len(kept)} earlier turn(s) left out to fit a budget of 1000 tokens]")
    assert _tokens(out) <= 1000


def test_a_group_waiting_on_the_user_lists_its_unanswered_questions_in_both_readings():
    waiting = _group("g_c", "Review PR 1130", "needs_you", 3, ["Posted the review"])
    waiting.steps[-1].questions = ["Merge it after the fix?", "Ping the author?"]
    board = Board([waiting, _group("g_b", "Release notes", "idle", 2, ["Draft written"])], [])

    lines = list_groups(board, now_ms=3).splitlines()
    thread = thread_text(board, "g_c", status_of=_no_status, lineage={})

    at = next(i for i, line in enumerate(lines) if line.startswith("- g_c "))
    assert lines[at + 1 : at + 5] == [
        "  Unanswered questions:",
        "  1. Merge it after the fix?",
        "  2. Ping the author?",
        lines[at + 4],
    ] and lines[at + 4].startswith("- g_b ")
    assert thread.endswith("\nUnanswered questions:\n1. Merge it after the fix?\n2. Ping the author?")


def test_only_a_waiting_group_s_latest_questions_are_unanswered():
    waiting = _group("g_c", "Review PR 1130", "needs_you", 3, ["Asked once", "Asked again"])
    waiting.steps[0].questions = ["An earlier one?"]
    waiting.steps[1].questions = ["The open one?"]
    done = _group("g_a", "Fix bug A", "completed", 1, ["Merged"])
    done.steps[-1].questions = ["Left over?"]
    board = Board([waiting, done], [])

    out = list_groups(board, now_ms=3) + thread_text(board, "g_a", status_of=_no_status, lineage={})

    assert "1. The open one?" in out
    assert "An earlier one?" not in out and "Left over?" not in out
