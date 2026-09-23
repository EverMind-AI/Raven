"""What waits on a person, and how their ruling gets back into the project."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.stint import backlog as backlog_mod
from raven.stint import decisions as decisions_mod


def _project(tmp_path: Path, body: str = "") -> Path:
    project = tmp_path / "game"
    (project / ".stint").mkdir(parents=True)
    if body:
        (project / ".stint" / "HUMAN_DECISIONS.md").write_text(body, encoding="utf-8")
    return project


DOCUMENT = """# HUMAN_DECISIONS.md

## Awaiting a person

- [ ] Q01 (round 02) the duel's scoring rule | blocks: 7, 12
- [x] Q02 (round 01) whether it needs networking
- [ ] Q03 (round 03) which font licence applies
"""


def test_reading_gives_the_state_the_round_and_what_each_holds_up(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)
    questions = decisions_mod.read(project)
    assert [q.id for q in questions] == ["Q01", "Q02", "Q03"]
    assert questions[0].blocks == [7, 12]
    assert questions[0].round == 2
    assert questions[1].answered is True
    assert [q.id for q in decisions_mod.pending(project)] == ["Q01", "Q03"]


def test_a_project_with_no_decisions_file_has_no_questions(tmp_path: Path) -> None:
    assert decisions_mod.read(_project(tmp_path)) == []


def test_asking_mints_the_next_id_and_keeps_the_others(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)
    question = decisions_mod.ask(project, "does the boss drop loot", round_index=4, blocks=[9])
    assert question.id == "Q04"
    again = decisions_mod.read(project)
    assert [q.id for q in again] == ["Q01", "Q02", "Q03", "Q04"]
    assert again[-1].blocks == [9]


def test_asking_a_project_that_has_no_file_yet_creates_one(tmp_path: Path) -> None:
    project = _project(tmp_path)
    decisions_mod.ask(project, "which engine", round_index=0)
    assert [q.id for q in decisions_mod.read(project)] == ["Q01"]


def test_an_answer_is_queued_rather_than_written(tmp_path: Path) -> None:
    """A role may be mid-turn in that same checkout; the round boundary is when it lands."""
    project = _project(tmp_path, DOCUMENT)
    state_dir = tmp_path / "state"
    decisions_mod.enqueue(state_dir, "Q01", "two rounds, total score")
    assert [q.id for q in decisions_mod.pending(project)] == ["Q01", "Q03"], "the document is untouched until merge"
    assert [entry["id"] for entry in decisions_mod.queued(state_dir)] == ["Q01"]


def test_answering_twice_keeps_the_later_ruling(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    decisions_mod.enqueue(state_dir, "Q01", "first thought")
    decisions_mod.enqueue(state_dir, "Q01", "second thought")
    queued = decisions_mod.queued(state_dir)
    assert len(queued) == 1
    assert queued[0]["answer"] == "second thought"


@pytest.mark.parametrize("question_id, answer", [("", "text"), ("Q01", "   ")])
def test_an_empty_id_or_ruling_is_refused(tmp_path: Path, question_id: str, answer: str) -> None:
    with pytest.raises(ValueError):
        decisions_mod.enqueue(tmp_path / "state", question_id, answer)


def test_merging_ticks_the_box_records_the_ruling_and_drains_the_queue(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)
    state_dir = tmp_path / "state"
    decisions_mod.enqueue(state_dir, "Q01", "two rounds, total score")
    applied = decisions_mod.merge(project, state_dir, 5)

    assert applied == ["Q01"]
    text = (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8")
    assert "- [x] Q01" in text
    assert "**Ruling** (round 05): two rounds, total score" in text
    assert "- [ ] Q03" in text, "an unanswered question is left alone"
    assert decisions_mod.queued(state_dir) == [], "the queue is drained so a merge never runs twice"


def test_merging_unblocks_the_tasks_the_question_was_holding(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)
    state_dir = tmp_path / "state"
    backlog = backlog_mod.Backlog(
        tasks=[
            backlog_mod.Task(id=7, title="waiting on a person", state=backlog_mod.OPEN),
            backlog_mod.Task(id=12, title="also waiting", state=backlog_mod.ASSIGNED),
        ]
    )
    for task_id in (7, 12):
        backlog_mod.apply(backlog, "block", role=backlog_mod.PLANNER, task_id=task_id, blocker="human:Q01")
    backlog_mod.save(project, backlog)

    decisions_mod.enqueue(state_dir, "Q01", "two rounds, total score")
    decisions_mod.merge(project, state_dir, 5)

    after = backlog_mod.load(project)
    assert after.get(7).state == backlog_mod.OPEN
    assert after.get(12).state == backlog_mod.ASSIGNED, "unblocking returns a task to the work it was in"


def test_a_task_also_waiting_on_another_task_stays_blocked(tmp_path: Path) -> None:
    """Answering a person does not clear a dependency, and pretending it did would hide one."""
    project = _project(tmp_path, DOCUMENT)
    state_dir = tmp_path / "state"
    backlog = backlog_mod.Backlog(
        tasks=[backlog_mod.Task(id=7, title="waiting on both"), backlog_mod.Task(id=9, title="the other")]
    )
    backlog_mod.apply(backlog, "block", role=backlog_mod.PLANNER, task_id=7, blocker="human:Q01")
    backlog_mod.apply(backlog, "block", role=backlog_mod.PLANNER, task_id=7, blocker="task:9")
    backlog_mod.save(project, backlog)

    decisions_mod.enqueue(state_dir, "Q01", "settled")
    decisions_mod.merge(project, state_dir, 5)

    after = backlog_mod.load(project)
    assert after.get(7).state == backlog_mod.BLOCKED
    assert after.get(7).blocked_by == ["task:9"]


def test_merging_an_id_nobody_asked_leaves_the_document_alone(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)
    state_dir = tmp_path / "state"
    before = (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8")
    decisions_mod.enqueue(state_dir, "Q99", "answer to nothing")
    assert decisions_mod.merge(project, state_dir, 5) == []
    assert (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8") == before


def test_merging_an_already_answered_question_does_not_double_up(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)
    state_dir = tmp_path / "state"
    decisions_mod.enqueue(state_dir, "Q02", "answered long ago")
    assert decisions_mod.merge(project, state_dir, 5) == []


def test_a_corrupt_queue_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    """A merge that took the round down would be worse than a lost answer."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    decisions_mod.queue_path(state_dir).write_text("{not json", encoding="utf-8")
    assert decisions_mod.queued(state_dir) == []
    assert decisions_mod.merge(_project(tmp_path, DOCUMENT), state_dir, 1) == []


def test_the_queue_file_is_a_list_of_rulings(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    decisions_mod.enqueue(state_dir, "Q01", "a ruling")
    written = json.loads(decisions_mod.queue_path(state_dir).read_text(encoding="utf-8"))
    assert written[0]["id"] == "Q01"
    assert written[0]["answer"] == "a ruling"
    assert written[0]["at"]


def test_a_provisional_ruling_rides_with_the_question_until_a_person_rules(tmp_path: Path) -> None:
    """Soft mode: the Planner decides and tells; the line carries both, and the ruling survives the answer."""
    project = tmp_path / "game"
    question = decisions_mod.ask(
        project, "which clock is canonical | really", round_index=3, provisional="the engine tick"
    )
    assert question.provisional == "the engine tick"
    read = decisions_mod.read(project)[0]
    assert (read.text, read.provisional, read.blocks) == ("which clock is canonical / really", "the engine tick", [])
    assert read.to_dict()["provisional"] == "the engine tick"

    state_dir = tmp_path / "state"
    decisions_mod.enqueue(state_dir, question.id, "wall clock, actually")
    decisions_mod.merge(project, state_dir, 4)
    after = decisions_mod.read(project)[0]
    assert after.answered and after.provisional == "the engine tick"
    assert "wall clock, actually" in (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8")


def test_a_question_with_blocks_and_a_ruling_parses_both(tmp_path: Path) -> None:
    project = tmp_path / "game"
    decisions_mod.ask(project, "the duel", round_index=1, blocks=[7, 12], provisional="two rounds")
    read = decisions_mod.read(project)[0]
    assert (read.blocks, read.provisional) == ([7, 12], "two rounds")


# ------------------------------------------------- what a person asks of a round


def test_what_a_person_asks_of_the_next_round_is_queued_and_carried_once(tmp_path: Path) -> None:
    """The Planner may be mid-turn in this checkout, so a request waits for the
    round boundary the same way an answer does; carrying it twice would spend two
    rounds on one instruction."""
    state_dir = tmp_path / "state"

    decisions_mod.enqueue_request(state_dir, "  spend the round on the camera  ")
    decisions_mod.enqueue_request(state_dir, "and leave the HUD alone")

    assert decisions_mod.take_requests(state_dir) == ["spend the round on the camera", "and leave the HUD alone"]
    assert decisions_mod.take_requests(state_dir) == []


def test_a_request_with_no_text_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        decisions_mod.enqueue_request(tmp_path / "state", "   ")


def test_a_request_is_kept_beside_the_run_where_nothing_can_revert_it(tmp_path: Path) -> None:
    """The project's decisions file is tracked in a tree the ownership pass
    reverts writes to, and a record that can be reverted is not a record."""
    project = _project(tmp_path, DOCUMENT)
    state_dir = tmp_path / "state"

    decisions_mod.record_requests(project, state_dir, ["make the duel best of three"], 4)

    ledger = decisions_mod.ledger_path(state_dir).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["text"] for line in ledger] == ["make the duel best of three"]
    assert json.loads(ledger[0])["round"] == 4
    text = (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8")
    assert decisions_mod.REQUESTS_HEADING in text
    assert "### Round 04" in text
    assert "- make the duel best of three" in text
    assert "- [ ] Q01" in text, "the questions the file already held are untouched"


def test_a_second_round_of_requests_adds_a_section_rather_than_a_second_heading(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)
    state_dir = tmp_path / "state"

    decisions_mod.record_requests(project, state_dir, ["one"], 1)
    decisions_mod.record_requests(project, state_dir, ["two", "   "], 2)

    text = (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8")
    assert text.count(decisions_mod.REQUESTS_HEADING) == 1
    assert "### Round 01" in text and "### Round 02" in text
    assert len(decisions_mod.ledger_path(state_dir).read_text(encoding="utf-8").splitlines()) == 2


def test_a_round_asked_for_nothing_writes_nothing_down(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)
    state_dir = tmp_path / "state"

    decisions_mod.record_requests(project, state_dir, ["   ", ""], 1)

    assert not decisions_mod.ledger_path(state_dir).exists()
    assert decisions_mod.REQUESTS_HEADING not in (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8")


def test_a_request_survives_a_project_with_no_decisions_file_to_put_it_in(tmp_path: Path) -> None:
    project = _project(tmp_path)
    state_dir = tmp_path / "state"

    decisions_mod.record_requests(project, state_dir, ["ship the camera fix"], 2)

    assert "ship the camera fix" in decisions_mod.ledger_path(state_dir).read_text(encoding="utf-8")


# ------------------------------------------------- answering with no run to wait for


def test_an_answer_queued_for_a_project_with_no_questions_file_stays_queued(tmp_path: Path) -> None:
    """Draining the queue against a file that is not there would lose a person's
    decision to a project that had not been set up yet."""
    project = _project(tmp_path)
    state_dir = tmp_path / "state"
    decisions_mod.enqueue(state_dir, "Q01", "two rounds, total score")

    assert decisions_mod.merge(project, state_dir, 1) == []
    assert [entry["id"] for entry in decisions_mod.queued(state_dir)] == ["Q01"]


def test_answering_a_project_with_no_run_writes_the_ruling_straight_in(tmp_path: Path) -> None:
    """There is no round boundary coming, so parking the answer would leave it
    invisible to the next run that starts."""
    project = _project(tmp_path, DOCUMENT)

    recorded = decisions_mod.answer_now(project, "Q01", "two rounds,  total score", round_index=3)

    assert recorded["id"] == "Q01"
    assert recorded["answer"] == "two rounds, total score"
    text = (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8")
    assert "- [x] Q01" in text
    assert "**Ruling** (round 03): two rounds, total score" in text
    assert decisions_mod.queued(tmp_path / "state") == []


def test_answering_now_lifts_the_block_the_question_was_holding(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)
    backlog = backlog_mod.Backlog(tasks=[backlog_mod.Task(id=7, title="waiting on a person")])
    backlog_mod.apply(backlog, "block", role=backlog_mod.PLANNER, task_id=7, blocker="human:Q01")
    backlog_mod.save(project, backlog)

    decisions_mod.answer_now(project, "Q01", "two rounds", round_index=2)

    assert backlog_mod.load(project).get(7).state == backlog_mod.OPEN


def test_a_task_the_question_named_but_that_moved_on_is_left_where_it_is(tmp_path: Path) -> None:
    """Q01 says it holds up task 7. If the Planner already unblocked it and put it
    in someone's hands, answering must not drag it through a transition nobody asked for."""
    project = _project(tmp_path, DOCUMENT)
    backlog = backlog_mod.Backlog(tasks=[backlog_mod.Task(id=7, title="already under way", state=backlog_mod.ASSIGNED)])
    backlog_mod.save(project, backlog)

    decisions_mod.answer_now(project, "Q01", "settled", round_index=2)

    after = backlog_mod.load(project).get(7)
    assert after.state == backlog_mod.ASSIGNED
    assert after.history == []


@pytest.mark.parametrize("question_id, answer", [("", "a ruling"), ("Q01", "   ")])
def test_answering_without_an_id_or_a_ruling_is_refused(tmp_path: Path, question_id: str, answer: str) -> None:
    with pytest.raises(ValueError):
        decisions_mod.answer_now(_project(tmp_path, DOCUMENT), question_id, answer)


def test_answering_a_project_that_has_no_questions_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="to answer in"):
        decisions_mod.answer_now(_project(tmp_path), "Q01", "a ruling")


def test_answering_a_question_nobody_asked_is_refused_rather_than_silently_lost(tmp_path: Path) -> None:
    project = _project(tmp_path, DOCUMENT)

    with pytest.raises(ValueError, match="Q99"):
        decisions_mod.answer_now(project, "Q99", "a ruling")

    assert (project / ".stint" / "HUMAN_DECISIONS.md").read_text(encoding="utf-8") == DOCUMENT


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 does not block root")
def test_a_decisions_file_that_cannot_be_read_is_no_questions_rather_than_a_crash(tmp_path: Path) -> None:
    """Every round opens by reading this file. One lost permission bit taking the
    whole run down would be a worse answer than an empty list."""
    project = _project(tmp_path, DOCUMENT)
    path = decisions_mod.decisions_path(project)
    path.chmod(0o000)
    try:
        assert decisions_mod.read(project) == []
        assert decisions_mod.pending(project) == []
    finally:
        path.chmod(0o600)
