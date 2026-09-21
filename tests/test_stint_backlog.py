"""The backlog: who may move a task where, and what "ready" means."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.stint import backlog as backlog_mod
from raven.stint.backlog import (
    ASSIGNED,
    BLOCKED,
    BUILDER,
    DONE,
    HUMAN,
    IN_REVIEW,
    OPEN,
    PLANNER,
    VERIFIER,
    Backlog,
    BacklogError,
    Task,
    apply,
    backlog_path,
    load,
    save,
    start,
)


def _backlog(*tasks: Task) -> Backlog:
    return Backlog(tasks=list(tasks))


def _seed(project: Path, tasks: list[dict]) -> Path:
    path = project / ".stint" / "backlog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"meta": {}, "tasks": tasks}), encoding="utf-8")
    return path


def test_a_task_walks_from_open_to_done_through_the_three_roles() -> None:
    backlog = _backlog(Task(id=1, title="the work"))
    backlog_mod.apply(backlog, "assign", role=PLANNER, task_id=1, round_index=1)
    assert backlog.get(1).state == ASSIGNED
    backlog_mod.apply(backlog, "implement", role=BUILDER, task_id=1, round_index=1, commit="abc1234")
    assert backlog.get(1).state == IN_REVIEW
    backlog_mod.apply(backlog, "proven", role=VERIFIER, task_id=1, round_index=1, evidence="demo/round_01/")
    assert backlog.get(1).state == DONE
    assert backlog.get(1).implements[0]["verdict"] == "proven"
    assert backlog.get(1).implements[0]["evidence"] == "demo/round_01/"


@pytest.mark.parametrize(
    "verb, role, extra",
    [
        ("assign", VERIFIER, {}),
        ("assign", BUILDER, {}),
        ("proven", BUILDER, {"evidence": "x"}),
        ("proven", PLANNER, {"evidence": "x"}),
        ("reopen", PLANNER, {"reason": "x"}),
        ("implement", VERIFIER, {"commit": "abc"}),
        ("drop", PLANNER, {"reason": "x"}),
    ],
)
def test_a_role_cannot_make_a_transition_it_does_not_own(verb, role, extra) -> None:
    """The whole point of the structure: `done` is not the Planner's to write."""
    backlog = _backlog(Task(id=1, title="the work", state=IN_REVIEW))
    with pytest.raises(BacklogError) as caught:
        backlog_mod.apply(backlog, verb, role=role, task_id=1, **extra)
    assert "may not" in str(caught.value)


def test_only_qa_can_reopen_a_finished_task() -> None:
    backlog = _backlog(Task(id=1, title="the work", state=DONE))
    backlog_mod.apply(backlog, "reopen", role=VERIFIER, task_id=1, round_index=8, reason="round 08 regression")
    task = backlog.get(1)
    assert task.state == OPEN
    assert task.history[-1] == {"round": 8, "to": OPEN, "by": VERIFIER, "reason": "round 08 regression"}


def test_a_verb_applied_from_the_wrong_state_is_refused() -> None:
    backlog = _backlog(Task(id=1, title="the work", state=OPEN))
    with pytest.raises(BacklogError) as caught:
        backlog_mod.apply(backlog, "implement", role=BUILDER, task_id=1, commit="abc")
    assert "is open" in str(caught.value)


def test_a_rejection_and_a_deferral_both_need_a_reason() -> None:
    """A rejection with no reason is the one Verifier cannot argue with, or learn from."""
    backlog = _backlog(Task(id=1, title="the work"))
    with pytest.raises(BacklogError):
        backlog_mod.apply(backlog, "reject", role=PLANNER, task_id=1, reason="  ")
    backlog_mod.apply(backlog, "defer", role=PLANNER, task_id=1, reason="phase two is not built")
    assert backlog.get(1).deferred == 1


def test_deferring_counts_and_leaves_the_task_open() -> None:
    backlog = _backlog(Task(id=1, title="the work"))
    for _ in range(3):
        backlog_mod.apply(backlog, "defer", role=PLANNER, task_id=1, reason="not yet")
    assert backlog.get(1).deferred == 3
    assert backlog.get(1).state == OPEN


def test_a_proven_verdict_needs_evidence_and_a_failing_one_needs_a_reason() -> None:
    backlog = _backlog(Task(id=1, title="the work", state=IN_REVIEW, implements=[{"round": 1, "verdict": None}]))
    with pytest.raises(BacklogError):
        backlog_mod.apply(backlog, "proven", role=VERIFIER, task_id=1, evidence="")
    with pytest.raises(BacklogError):
        backlog_mod.apply(backlog, "not_proven", role=VERIFIER, task_id=1, reason="")
    backlog_mod.apply(backlog, "not_proven", role=VERIFIER, task_id=1, reason="one hit location only")
    assert backlog.get(1).state == OPEN


def test_blocking_remembers_what_it_interrupted() -> None:
    backlog = _backlog(Task(id=1, title="the work", state=ASSIGNED))
    backlog_mod.apply(backlog, "block", role=PLANNER, task_id=1, blocker="human:Q3")
    assert backlog.get(1).state == BLOCKED
    backlog_mod.apply(backlog, "unblock", role=HUMAN, task_id=1, blocker="human:Q3")
    assert backlog.get(1).state == ASSIGNED, "unblocking returns a task to the work it was in"


def test_a_task_waiting_on_two_things_stays_blocked_until_both_lift() -> None:
    backlog = _backlog(Task(id=1, title="the work"), Task(id=9, title="the other"))
    backlog_mod.apply(backlog, "block", role=PLANNER, task_id=1, blocker="human:Q3")
    backlog_mod.apply(backlog, "block", role=PLANNER, task_id=1, blocker="task:9")
    backlog_mod.apply(backlog, "unblock", role=HUMAN, task_id=1, blocker="human:Q3")
    assert backlog.get(1).state == BLOCKED
    assert backlog.get(1).blocked_by == ["task:9"]
    backlog_mod.apply(backlog, "unblock", role=PLANNER, task_id=1, blocker="task:9")
    assert backlog.get(1).state == OPEN, "the second block must not have overwritten what to come back to"


def test_ready_is_open_unblocked_and_every_dependency_settled() -> None:
    backlog = _backlog(
        Task(id=1, title="infra", state=DONE),
        Task(id=2, title="depends on done", depends_on=[1]),
        Task(id=3, title="depends on open", depends_on=[2]),
        Task(id=4, title="blocked", blocked_by=["human:Q1"], state=BLOCKED),
        Task(id=5, title="already assigned", state=ASSIGNED),
    )
    assert [task.id for task in backlog.ready()] == [2]


def test_a_rejected_dependency_does_not_strand_what_came_after_it() -> None:
    """Deciding not to do something is an answer, not a permanent block."""
    backlog = _backlog(
        Task(id=1, title="cut", state="rejected"),
        Task(id=2, title="after it", depends_on=[1]),
    )
    assert [task.id for task in backlog.ready()] == [2]


def test_an_unjudged_task_goes_back_to_open_at_the_end_of_a_round() -> None:
    """Not verified is not done, and the sweep is what makes that true."""
    backlog = _backlog(
        Task(id=1, title="judged", state=DONE),
        Task(id=2, title="never judged", state=IN_REVIEW),
    )
    swept = backlog_mod.sweep_unverified(backlog, 4)
    assert [task.id for task in swept] == [2]
    assert backlog.get(2).state == OPEN
    assert "unverified" in backlog.get(2).history[-1]["reason"]


def test_a_task_assigned_and_never_taken_goes_back_to_open_when_the_round_ends() -> None:
    """Assigned is a promise for the round. Kept past it, the next Planner read
    the task as somebody's and the next Builder was handed only what that
    round's Planner assigned, so a Builder cut off by its turn budget parked
    tasks for the rest of the stint (measured 2026-09-20: three of them)."""
    backlog = _backlog(
        Task(id=1, title="taken", state=IN_REVIEW),
        Task(id=2, title="promised", state=ASSIGNED, owner="builder-2"),
        Task(id=3, title="not this round", state=OPEN),
        Task(id=4, title="finished", state=DONE),
    )
    released = backlog_mod.release_unimplemented(backlog, 3)
    assert [task.id for task in released] == [2]
    assert backlog.get(2).state == OPEN and backlog.get(2).owner == ""
    assert "unimplemented" in backlog.get(2).history[-1]["reason"]
    assert backlog.get(1).state == IN_REVIEW, "a task the Builder took is Verifier's to judge, not this verb's"
    assert [backlog.get(3).state, backlog.get(4).state] == [OPEN, DONE]


def test_adding_mints_the_next_id_and_records_where_it_came_from() -> None:
    backlog = _backlog(Task(id=1, title="one"), Task(id=7, title="seven"))
    task = backlog_mod.apply(backlog, "add", role=PLANNER, round_index=4, title="found in round 3", source="round_03")
    assert task.id == 8
    assert task.source == "round_03"


def test_a_round_trip_through_disk_keeps_every_field(tmp_path: Path) -> None:
    project = tmp_path / "game"
    _seed(
        project,
        [
            {
                "id": 3,
                "title": "the work",
                "state": "blocked",
                "gates": ["7.4"],
                "depends_on": [1],
                "with": [4],
                "blocked_by": ["human:Q3"],
                "deferred": 2,
                "implements": [{"round": 1, "commit": "abc", "verdict": "not_proven"}],
            }
        ],
    )
    backlog = backlog_mod.load(project)
    backlog_mod.save(project, backlog)
    again = backlog_mod.load(project)
    assert again.get(3).to_dict() == backlog.get(3).to_dict()
    assert again.get(3).with_ == [4]


def test_a_backlog_with_a_repeated_id_is_refused(tmp_path: Path) -> None:
    """Ids are how a task stays the same task across thirty rounds."""
    project = tmp_path / "game"
    _seed(project, [{"id": 1, "title": "one"}, {"id": 1, "title": "one again"}])
    with pytest.raises(BacklogError) as caught:
        backlog_mod.load(project)
    assert "twice" in str(caught.value)


def test_a_project_with_no_backlog_says_how_to_get_one(tmp_path: Path) -> None:
    with pytest.raises(BacklogError) as caught:
        backlog_mod.load(tmp_path)
    assert "raven playbook stint task add" in str(caught.value)


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("Boot shell: 1920x1080 SubViewport render path", "Boot shell"),
        ("Pin the cross-session clock -- never read the tick", "Pin the cross-session clock"),
        (
            "Structured event log with a monotonic high-precision clock, covering every domain",
            "Structured event log with a monotonic...",
        ),
        ("assert_feel.py", "assert_feel.py"),
    ],
)
def test_a_task_without_a_name_is_called_by_its_title_head(title: str, name: str) -> None:
    assert Task(id=1, title=title).label == name
    assert Task(id=1, title=title, name="Given").label == "Given"


def test_adding_keeps_the_name_and_round_trips_it() -> None:
    backlog = _backlog()
    task = backlog_mod.apply(backlog, "add", role=PLANNER, title="Boot shell: the render path", name="  Boot  shell ")
    assert task.name == "Boot shell"
    assert Task.from_dict(task.to_dict()).name == "Boot shell"
    assert "name" not in Task(id=2, title="unnamed").to_dict(), "a derived name is not written into the ledger"


def test_blocking_on_a_question_nobody_asked_is_refused() -> None:
    """The answer that would lift such a block can never arrive."""
    backlog = _backlog(Task(id=1, title="the work"))
    with pytest.raises(BacklogError, match="raven playbook stint ask"):
        backlog_mod.apply(
            backlog, "block", role=PLANNER, task_id=1, blocker="human:q_7_2_definition", known_questions=["Q01"]
        )
    assert backlog.get(1).state == OPEN
    backlog_mod.apply(backlog, "block", role=PLANNER, task_id=1, blocker="human:Q01", known_questions=["Q01"])
    assert backlog.get(1).blocked_by == ["human:Q01"]


@pytest.mark.parametrize("blocker", ["task:9", "task:one", "external:", "weather:rain"])
def test_a_blocker_that_names_nothing_is_refused(blocker: str) -> None:
    backlog = _backlog(Task(id=1, title="the work"))
    with pytest.raises(BacklogError):
        backlog_mod.apply(backlog, "block", role=PLANNER, task_id=1, blocker=blocker)


class TestFilingTheFirstTask:
    """`task add` into a project that has no backlog yet."""

    def test_the_verb_the_missing_backlog_message_names_is_not_refused_by_it(self, tmp_path: Path) -> None:
        """`load` refuses a project with no backlog and points at `task add`;
        `task add` went through `load`, so the cure was refused by the message
        recommending it and no first task could ever be filed."""
        backlog = start(tmp_path)
        apply(backlog, "add", role=HUMAN, title="the first thing", round_index=0)
        save(tmp_path, backlog)

        assert [task.title for task in load(tmp_path).tasks] == ["the first thing"]

    def test_an_existing_backlog_is_read_rather_than_replaced(self, tmp_path: Path) -> None:
        first = start(tmp_path)
        apply(first, "add", role=HUMAN, title="one", round_index=0)
        save(tmp_path, first)

        second = start(tmp_path)
        apply(second, "add", role=HUMAN, title="two", round_index=0)
        save(tmp_path, second)

        assert [task.title for task in load(tmp_path).tasks] == ["one", "two"]

    def test_a_backlog_that_is_there_and_unreadable_still_refuses(self, tmp_path: Path) -> None:
        """Starting empty here would file the new task into a document that
        silently dropped every task before it."""
        path = backlog_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(BacklogError):
            start(tmp_path)
