"""The Task Board page's reader: every host conversation's Task Groups as cards, with their category."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from raven.agent.subagent.history import dag_root
from raven.agent.task_board import overview
from raven.agent.task_board.board import Board, ProgressStep, TaskGroup, TaskLink
from raven.agent.task_board.overview import Card, answer, category_of, scan
from raven.agent.task_board.store import save_board


def _group(gid: str, status: str, *links: list[str], updated: int = 10, title: str = "") -> TaskGroup:
    steps = [
        ProgressStep(
            f"s_{gid}_{i}", f"T{i}", None, f"step {i}", f"detail {i}", [TaskLink(*h.split(":", 1), h) for h in hs]
        )
        for i, hs in enumerate(links or ([],))
    ]
    return TaskGroup(gid, title or f"Matter {gid}", f"Where {gid} stands", status, 1, updated, steps)


def _session(
    root: Path,
    name: str,
    *groups: TaskGroup,
    key: str | None = None,
    meta: dict | None = None,
    first_user: str = "hello",
) -> Path:
    group_dir = root / "web"
    group_dir.mkdir(parents=True, exist_ok=True)
    record = {"_type": "metadata", "key": key or f"web:{name}", "metadata": {"title": f"Chat {name}", **(meta or {})}}
    lines = [json.dumps(record), json.dumps({"role": "user", "content": first_user, "timestamp": "T0"})]
    (group_dir / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    session_dir = group_dir / name
    if groups:
        save_board(session_dir, Board(list(groups), []))
    return session_dir


def _row(status: str, *, started: int | None = None, nodes: int = 1) -> dict:
    """A run as ``tasks.list`` answers it, reduced to what the reader reads."""
    return {"status": status, "started_at": started, "nodes": [{} for _ in range(nodes)]}


def _cards(root: Path, rows: dict | None = None, *, include_completed: bool = False) -> list[dict]:
    rows = rows or {}
    return answer(scan(root), lambda s: rows.get(s.key, {}), include_completed=include_completed)["cards"]


def test_category_follows_the_rule_order():
    assert category_of("completed", True) == "completed"
    assert category_of("needs_you", True) == "needs_you"
    assert category_of("needs_you", False) == "needs_you"
    assert category_of("idle", True) == "running"
    assert category_of("running", True) == "running"
    assert category_of("idle", False) == "idle"
    assert category_of("running", False) == "idle"


def test_a_running_spawn_makes_its_card_running_since_its_start(tmp_path):
    _session(tmp_path, "a", _group("g1", "idle", ["spawn:n1"]))

    (card,) = _cards(tmp_path, {"web:a": {("spawn", "n1"): _row("running", started=1000)}})

    assert (card["category"], card["running_since_ms"]) == ("running", 1000)


def test_a_run_the_sidebar_reads_interrupted_is_not_running(tmp_path):
    _session(tmp_path, "a", _group("g1", "idle", ["spawn:n1"]))

    (card,) = _cards(tmp_path, {"web:a": {("spawn", "n1"): _row("interrupted", started=1000)}})

    assert (card["category"], card["running_since_ms"]) == ("idle", None)


def test_a_running_run_with_no_clock_still_runs(tmp_path):
    _session(tmp_path, "a", _group("g1", "idle", ["dag:R1"]))

    (card,) = _cards(tmp_path, {"web:a": {("dag", "R1"): _row("running")}})

    assert (card["category"], card["running_since_ms"]) == ("running", None)


def test_a_dag_run_runs_while_it_or_a_run_that_continued_it_runs(tmp_path):
    session_dir = _session(tmp_path, "a", _group("g1", "idle", ["dag:R1"]))
    graph = dag_root(session_dir) / "R1" / "graph.json"
    graph.parent.mkdir(parents=True)
    graph.write_text(json.dumps({"replan": {"run_id": "R2"}}), encoding="utf-8")
    rows = {("dag", "R1"): _row("cancelled", started=500, nodes=2), ("dag", "R2"): _row("running", started=2000)}

    (card,) = _cards(tmp_path, {"web:a": rows})

    assert (card["category"], card["running_since_ms"], card["sub_agents"]) == ("running", 2000, 3)


def test_counters_count_events_and_each_sub_agent_once(tmp_path):
    _session(tmp_path, "a", _group("g1", "idle", ["spawn:n1", "spawn:gone"], ["dag:R1"]))
    rows = {("spawn", "n1"): _row("completed"), ("dag", "R1"): _row("completed", nodes=2)}

    (card,) = _cards(tmp_path, {"web:a": rows})

    assert (card["events"], card["sub_agents"]) == (2, 4)
    assert (card["category"], card["running_since_ms"]) == ("idle", None)


def test_a_run_placed_on_several_events_counts_once(tmp_path):
    _session(tmp_path, "a", _group("g1", "needs_you", ["spawn:n1"], ["spawn:n1"], ["spawn:n1"]))

    (card,) = _cards(tmp_path, {"web:a": {("spawn", "n1"): _row("completed")}})

    assert (card["events"], card["sub_agents"]) == (3, 1)


def test_completed_cards_come_only_when_asked_and_counts_always(tmp_path):
    _session(tmp_path, "a", _group("done", "completed"), _group("ask", "needs_you"), _group("go", "idle"))

    sessions = scan(tmp_path)
    hidden = answer(sessions, lambda s: {}, include_completed=False)
    shown = answer(sessions, lambda s: {}, include_completed=True)

    assert sorted(c["group_id"] for c in hidden["cards"]) == ["ask", "go"]
    assert (hidden["completed_count"], hidden["needs_you_count"]) == (1, 1)
    assert sorted(c["group_id"] for c in shown["cards"]) == ["ask", "done", "go"]
    assert (shown["completed_count"], shown["needs_you_count"]) == (1, 1)


def test_cards_come_newest_first_across_conversations(tmp_path):
    _session(tmp_path, "a", _group("old", "idle", updated=5))
    _session(tmp_path, "b", _group("new", "idle", updated=9), _group("mid", "needs_you", updated=7))

    assert [c["group_id"] for c in _cards(tmp_path)] == ["new", "mid", "old"]


def test_only_host_conversations_with_a_board_are_read(tmp_path):
    _session(tmp_path, "host", _group("g1", "idle"))
    _session(tmp_path, "boardless")
    _session(tmp_path, "child", _group("g2", "idle"), key="sub:child")
    _session(tmp_path, "shelved", _group("g3", "idle"), meta={"archived": True})

    assert [(c["session_key"], c["group_id"]) for c in _cards(tmp_path)] == [("web:host", "g1")]


def test_the_last_metadata_record_is_the_one_read(tmp_path):
    _session(tmp_path, "renamed", _group("g1", "idle"))
    _session(tmp_path, "shelved", _group("g2", "idle"))
    for name, later in (
        ("renamed", {"title": "Better name"}),
        ("shelved", {"title": "Chat shelved", "archived": True}),
    ):
        transcript = tmp_path / "web" / f"{name}.jsonl"
        record = {"_type": "metadata", "key": f"web:{name}", "metadata": later}
        transcript.write_text(transcript.read_text(encoding="utf-8") + json.dumps(record) + "\n", encoding="utf-8")

    assert [(c["session_key"], c["session_title"]) for c in _cards(tmp_path)] == [("web:renamed", "Better name")]


def test_a_broken_session_is_skipped_not_fatal(tmp_path):
    _session(tmp_path, "good", _group("g1", "idle"))
    orphan = _session(tmp_path, "orphan", _group("g2", "idle"))
    (orphan.parent / "orphan.jsonl").unlink()
    _session(tmp_path, "garbled", _group("g3", "idle"))
    (tmp_path / "web" / "garbled.jsonl").write_text("not json\n", encoding="utf-8")

    assert [c["group_id"] for c in _cards(tmp_path)] == ["g1"]


def test_a_bad_line_does_not_hide_a_conversation_the_rail_lists(tmp_path):
    _session(tmp_path, "a", _group("g1", "idle"))
    transcript = tmp_path / "web" / "a.jsonl"
    transcript.write_text("{torn\n" + transcript.read_text(encoding="utf-8"), encoding="utf-8")

    assert [(c["group_id"], c["session_title"]) for c in _cards(tmp_path)] == [("g1", "Chat a")]


def test_an_unchanged_session_is_not_read_again(tmp_path, monkeypatch):
    session_dir = _session(tmp_path, "a", _group("g1", "idle"))
    reads: list[Path] = []
    real = overview.load_board

    def counting(path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(overview, "load_board", counting)

    scan(tmp_path)
    scan(tmp_path)
    assert len(reads) == 1

    board_file = session_dir / "task_board.json"
    stat = board_file.stat()
    os.utime(board_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    scan(tmp_path)
    assert len(reads) == 2


def test_run_state_is_read_on_every_answer(tmp_path):
    _session(tmp_path, "a", _group("g1", "idle", ["spawn:n1"]))
    asked: list[str] = []
    state = {"status": "running"}

    def rows_of(session):
        asked.append(session.key)
        return {("spawn", "n1"): _row(state["status"], started=1)}

    first = answer(scan(tmp_path), rows_of, include_completed=False)
    state["status"] = "interrupted"
    second = answer(scan(tmp_path), rows_of, include_completed=False)

    assert asked == ["web:a", "web:a"]
    assert [first["cards"][0]["category"], second["cards"][0]["category"]] == ["running", "idle"]


def test_a_session_with_no_runs_is_never_asked_for_them(tmp_path):
    _session(tmp_path, "a", _group("g1", "needs_you"))
    asked: list[str] = []

    answer(scan(tmp_path), lambda s: asked.append(s.key) or {}, include_completed=False)

    assert asked == []


def test_runs_that_cannot_be_read_leave_the_cards_standing(tmp_path):
    _session(tmp_path, "a", _group("g1", "idle", ["spawn:n1"]))

    def broken(_session):
        raise OSError("gone")

    (card,) = answer(scan(tmp_path), broken, include_completed=False)["cards"]

    assert (card["group_id"], card["category"], card["sub_agents"]) == ("g1", "idle", 1)


def test_cards_carry_title_and_summary_but_never_a_step_summary(tmp_path):
    _session(tmp_path, "a", _group("g1", "needs_you", title="Fix the blank page"))

    (card,) = _cards(tmp_path)

    assert set(card) == {f.name for f in dataclasses.fields(Card)}
    assert (card["title"], card["summary"], card["session_title"]) == (
        "Fix the blank page",
        "Where g1 stands",
        "Chat a",
    )
    assert "detail 0" not in json.dumps(card)


@pytest.mark.parametrize(
    ("first_user", "expected"),
    [
        ("fix the login page", "fix the login page"),
        ("fix the\n  login page", "fix the login page"),
        ("x" * 130, "x" * 120),
    ],
)
def test_an_untitled_session_is_named_by_its_first_message(tmp_path, first_user, expected):
    _session(tmp_path, "a", _group("g1", "idle"), meta={"title": ""}, first_user=first_user)

    (card,) = _cards(tmp_path)

    assert card["session_title"] == expected
