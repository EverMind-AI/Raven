"""Tests for ``raven.agent.subagent.dag_carry``: what a replan carries into its successor run."""

from __future__ import annotations

import pytest

from raven.agent.subagent.dag_carry import CarryPlan, classify_nodes, differing_fields, plan_carry
from raven.agent.subagent.dag_graph import DagNodeSpec


def _n(node_id: str, deps: tuple[str, ...] = (), prompt: str = "do it", **extra: object) -> DagNodeSpec:
    return DagNodeSpec(
        id=node_id,
        subagent=str(extra.pop("subagent", "alpha")),
        node_summary=str(extra.pop("node_summary", f"step {node_id}")),
        prompt_template=prompt,
        depends_on=list(deps),
        **extra,
    )


def _by_id(*nodes: DagNodeSpec) -> dict[str, DagNodeSpec]:
    return {node.id: node for node in nodes}


def test_classes_follow_the_attempt_not_the_status_alone():
    classes = classify_nodes(
        {
            "done": "completed",
            "judging": "completed",
            "run": "running",
            "wait": "exception",
            "again": "pending",
            "queued": "pending",
            "fresh": "pending",
            "x": "exception",
        },
        begun={"done", "judging", "run", "wait", "again", "x"},
        in_flight={"judging"},
        queued={"queued"},
        answered="x",
    )
    assert classes == {
        "done": "finished",
        "judging": "in_progress",
        "run": "in_progress",
        "wait": "in_progress",
        "again": "in_progress",
        "queued": "queued",
        "fresh": "not_started",
        "x": "answered",
    }


def test_every_terminal_status_is_finished():
    statuses = {s: s for s in ("completed", "failed", "skipped", "cancelled", "not_executed", "carried")}
    classes = classify_nodes(statuses, begun=set(), in_flight=set(), queued=set(), answered="none")
    assert set(classes.values()) == {"finished"}


def test_the_research_example_carries_r1_r3_and_redefines_write():
    authored = _by_id(_n("r1"), _n("r2"), _n("r3"), _n("write", deps=("r1", "r2", "r3")))
    classes = {"r1": "in_progress", "r2": "answered", "r3": "in_progress", "write": "not_started"}
    status = {"r1": "running", "r2": "exception", "r3": "running", "write": "pending"}
    plan = plan_carry(
        [_n("r2a"), _n("r2b"), _n("write", deps=("r1", "r2a", "r2b", "r3"))],
        authored=authored,
        current=authored,
        classes=classes,
        status=status,
    )
    assert isinstance(plan, CarryPlan), plan
    assert (plan.carried, plan.redefined, plan.new, plan.dropped) == (
        ("r1", "r3", "write"),
        ("write",),
        ("r2a", "r2b"),
        (),
    )
    assert [n.id for n in plan.nodes] == ["r1", "r3", "write", "r2a", "r2b"]
    assert plan.nodes[2].depends_on == ["r1", "r2a", "r2b", "r3"]


def test_an_unreferenced_leaf_is_dropped_and_a_listed_one_is_carried():
    authored = _by_id(_n("p"), _n("q"), _n("x"))
    classes = {"p": "in_progress", "q": "in_progress", "x": "answered"}
    status = {"p": "running", "q": "running", "x": "exception"}
    plan = plan_carry([authored["p"], _n("fresh")], authored=authored, current=authored, classes=classes, status=status)
    assert isinstance(plan, CarryPlan), plan
    assert plan.carried == ("p",)
    assert plan.dropped == ("q",)
    assert plan.new == ("fresh",)


def test_the_unfinished_upstream_of_a_carried_node_is_carried():
    authored = _by_id(_n("a"), _n("b", deps=("a",)), _n("c", deps=("b",)), _n("x"))
    classes = {"a": "finished", "b": "in_progress", "c": "not_started", "x": "answered"}
    status = {"a": "completed", "b": "running", "c": "pending", "x": "exception"}
    plan = plan_carry([_n("fresh", deps=("c",))], authored=authored, current=authored, classes=classes, status=status)
    assert isinstance(plan, CarryPlan), plan
    assert plan.carried == ("b", "c")
    assert plan.context == ("a",)
    assert plan.redefined == ()
    assert plan.dropped == ()


def test_listing_a_finished_node_is_refused():
    authored = _by_id(_n("a"), _n("x"))
    classes = {"a": "finished", "x": "answered"}
    status = {"a": "completed", "x": "exception"}
    errors = plan_carry(
        [authored["a"], _n("fresh")], authored=authored, current=authored, classes=classes, status=status
    )
    assert isinstance(errors, list)
    assert any("'a' has finished (completed); depend on it to read its output" in e for e in errors), errors


@pytest.mark.parametrize("status_word", ["completed", "not_executed"])
def test_a_node_carried_before_that_finished_since_is_read_not_refused(status_word: str):
    authored = _by_id(_n("a"), _n("x"))
    classes = {"a": "finished", "x": "answered"}
    status = {"a": status_word, "x": "exception"}
    plan = plan_carry(
        [authored["a"], _n("fresh", deps=("a",))],
        authored=authored,
        current=authored,
        classes=classes,
        status=status,
        carried_before={"a"},
    )
    assert isinstance(plan, CarryPlan), plan
    assert (plan.carried, plan.context, plan.new) == ((), ("a",), ("fresh",))


@pytest.mark.parametrize("status_word", ["failed", "skipped", "cancelled"])
def test_a_node_carried_before_that_died_since_is_still_refused(status_word: str):
    authored = _by_id(_n("a"), _n("x"))
    classes = {"a": "finished", "x": "answered"}
    status = {"a": status_word, "x": "exception"}
    errors = plan_carry(
        [authored["a"], _n("fresh")],
        authored=authored,
        current=authored,
        classes=classes,
        status=status,
        carried_before={"a"},
    )
    assert isinstance(errors, list)
    assert any(f"'a' has finished ({status_word})" in e for e in errors), errors


def test_a_running_node_listed_with_changes_is_refused_naming_the_fields():
    authored = _by_id(_n("r1"), _n("x"))
    classes = {"r1": "in_progress", "x": "answered"}
    status = {"r1": "running", "x": "exception"}
    errors = plan_carry(
        [_n("r1", prompt="do it differently"), _n("fresh")],
        authored=authored,
        current=authored,
        classes=classes,
        status=status,
    )
    assert isinstance(errors, list)
    assert any("'r1' is in progress" in e and "prompt_template" in e for e in errors), errors


def test_a_queued_node_listed_with_changes_is_refused():
    authored = _by_id(_n("q"), _n("x"))
    classes = {"q": "queued", "x": "answered"}
    status = {"q": "pending", "x": "exception"}
    errors = plan_carry(
        [_n("q", prompt="other"), _n("fresh")], authored=authored, current=authored, classes=classes, status=status
    )
    assert isinstance(errors, list)
    assert any("'q' is queued" in e for e in errors), errors


def test_node_summary_alone_does_not_change_a_running_node():
    authored = _by_id(_n("r1"), _n("x"))
    classes = {"r1": "in_progress", "x": "answered"}
    status = {"r1": "running", "x": "exception"}
    plan = plan_carry(
        [_n("r1", node_summary="renamed"), _n("fresh")],
        authored=authored,
        current=authored,
        classes=classes,
        status=status,
    )
    assert isinstance(plan, CarryPlan), plan
    assert plan.carried == ("r1",)
    assert plan.redefined == ()


def test_a_moved_node_may_be_listed_as_submitted_or_as_it_runs_now():
    authored = _by_id(_n("r1", subagent="alpha"), _n("x"))
    current = {**authored, "r1": authored["r1"].model_copy(update={"subagent": "beta"})}
    classes = {"r1": "in_progress", "x": "answered"}
    status = {"r1": "running", "x": "exception"}
    for agent in ("alpha", "beta"):
        plan = plan_carry(
            [_n("r1", subagent=agent), _n("fresh")], authored=authored, current=current, classes=classes, status=status
        )
        assert isinstance(plan, CarryPlan), (agent, plan)
        assert plan.carried == ("r1",)
    errors = plan_carry(
        [_n("r1", subagent="gamma"), _n("fresh")], authored=authored, current=current, classes=classes, status=status
    )
    assert isinstance(errors, list)
    assert any("'r1' is in progress" in e and "subagent" in e for e in errors), errors


def test_the_answered_node_cannot_be_listed_or_reached():
    authored = _by_id(_n("x"))
    classes = {"x": "answered"}
    status = {"x": "exception"}
    listed = plan_carry(
        [authored["x"], _n("fresh")], authored=authored, current=authored, classes=classes, status=status
    )
    reached = plan_carry(
        [_n("fresh", deps=("x",))], authored=authored, current=authored, classes=classes, status=status
    )
    for errors in (listed, reached):
        assert isinstance(errors, list)
        assert any("is the node this replan answers" in e for e in errors), errors


@pytest.mark.parametrize(
    ("status_word", "past"),
    [("failed", "failed"), ("skipped", "was skipped"), ("cancelled", "was cancelled")],
)
def test_a_dead_dependency_is_refused(status_word: str, past: str):
    authored = _by_id(_n("f"), _n("x"))
    classes = {"f": "finished", "x": "answered"}
    status = {"f": status_word, "x": "exception"}
    errors = plan_carry([_n("fresh", deps=("f",))], authored=authored, current=authored, classes=classes, status=status)
    assert isinstance(errors, list)
    assert any("depends on 'f', which" in e and past in e and "redefine" in e for e in errors), errors


def test_a_not_executed_dependency_is_context():
    authored = _by_id(_n("ne"), _n("x"))
    classes = {"ne": "finished", "x": "answered"}
    status = {"ne": "not_executed", "x": "exception"}
    plan = plan_carry([_n("fresh", deps=("ne",))], authored=authored, current=authored, classes=classes, status=status)
    assert isinstance(plan, CarryPlan), plan
    assert plan.context == ("ne",)


def test_context_is_the_whole_finished_upstream_and_none_of_it_is_carried():
    """The successor renders a node's upstream record lines from its own node table,
    so a finished node two hops up has to be there too, or the prompt the plan
    carried would lose a line it had."""
    authored = _by_id(_n("z"), _n("a", deps=("z",)), _n("x"))
    classes = {"z": "finished", "a": "finished", "x": "answered"}
    status = {"z": "completed", "a": "completed", "x": "exception"}
    plan = plan_carry([_n("fresh", deps=("a",))], authored=authored, current=authored, classes=classes, status=status)
    assert isinstance(plan, CarryPlan), plan
    assert plan.context == ("z", "a")
    assert plan.carried == ()


def test_a_plan_that_changes_nothing_is_refused():
    authored = _by_id(_n("r1"), _n("w", deps=("r1",)), _n("x"))
    classes = {"r1": "in_progress", "w": "not_started", "x": "answered"}
    status = {"r1": "running", "w": "pending", "x": "exception"}
    errors = plan_carry([authored["w"]], authored=authored, current=authored, classes=classes, status=status)
    assert isinstance(errors, list)
    assert any("has to change the plan" in e and "abandon" in e for e in errors), errors


def test_an_identical_not_started_listing_is_carried_unchanged():
    authored = _by_id(_n("w"), _n("x"))
    classes = {"w": "not_started", "x": "answered"}
    status = {"w": "pending", "x": "exception"}
    plan = plan_carry([authored["w"], _n("fresh")], authored=authored, current=authored, classes=classes, status=status)
    assert isinstance(plan, CarryPlan), plan
    assert plan.carried == ("w",)
    assert plan.redefined == ()


def test_a_node_listed_twice_is_refused():
    authored = _by_id(_n("x"))
    classes = {"x": "answered"}
    status = {"x": "exception"}
    errors = plan_carry([_n("fresh"), _n("fresh")], authored=authored, current=authored, classes=classes, status=status)
    assert isinstance(errors, list)
    assert any("listed twice" in e for e in errors), errors


def test_differing_fields_names_only_what_differs():
    base = _n("a", prompt="one")
    assert differing_fields(_n("a", prompt="two"), authored=base) == ["prompt_template"]
    assert differing_fields(_n("a", node_summary="other", prompt="one"), authored=base, ignore_summary=True) == []
    assert differing_fields(_n("a", node_summary="other", prompt="one"), authored=base) == ["node_summary"]
