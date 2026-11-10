"""Which spawn calls and DAG runs a turn dispatched, read from the turn's own tool rows."""

from __future__ import annotations

import json

import pytest

from raven.agent.task_board.board import Dispatch
from raven.agent.task_board.dispatches import dag_run_id_of, delegated_handle, spawn_task_id_of, turn_dispatches

RUN = "20261029T100213Z-3eb3b470"
PLAYBOOK_RUN = "20261029T100500Z-0badf00d"


def _call(call_id: str, name: str, args: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}],
    }


def _result(call_id: str, name: str, content) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def test_a_started_spawn_is_its_node_id():
    turn = [
        {"role": "user", "content": "find the bug"},
        _call("c1", "spawn", {"node_id": "locate_login_bug", "task_summary": "Locate it", "task": "..."}),
        _result("c1", "spawn", "Subagent [Locate it] started (id: 1a2b3c4d). I'll notify you when it completes."),
    ]

    assert turn_dispatches(turn) == [Dispatch("spawn:locate_login_bug", "Locate it")]


def test_a_refused_spawn_is_not_a_dispatch():
    turn = [
        _call("c1", "spawn", {"node_id": "locate_login_bug", "task_summary": "Locate it"}),
        _result("c1", "spawn", "Error: node_id 'locate_login_bug' is already taken by an earlier run."),
    ]

    assert turn_dispatches(turn) == []


def test_graph_and_playbook_receipts_name_their_run():
    turn = [
        _call("c1", "run_subagent_dag", {"task_summary": "Review fixes", "nodes": []}),
        _result("c1", "run_subagent_dag", f"DAG run {RUN} started in the background (3 nodes). Results follow."),
        _call("c2", "load_playbook", {"name": "review-fix"}),
        _result(
            "c2",
            "load_playbook",
            [
                {
                    "type": "text",
                    "text": f"DAG {PLAYBOOK_RUN}: started 'review-fix' (4 steps); results will be delivered.",
                }
            ],
        ),
    ]

    assert turn_dispatches(turn) == [
        Dispatch(f"dag:{RUN}", "Review fixes"),
        Dispatch(f"dag:{PLAYBOOK_RUN}", "review-fix"),
    ]


def test_a_graph_refused_before_dispatch_is_not_a_dispatch():
    turn = [
        _call("c1", "run_subagent_dag", {"task_summary": "Review fixes"}),
        _result("c1", "run_subagent_dag", "rejected: node 'a' depends on unknown node 'b'"),
    ]

    assert turn_dispatches(turn) == []


def test_dispatches_keep_their_first_order_without_duplicates():
    turn = [
        _call("c1", "run_subagent_dag", {"task_summary": "Review fixes"}),
        _result("c1", "run_subagent_dag", f"DAG run {RUN} started in the background (3 nodes)."),
        _call("c2", "spawn", {"node_id": "write_notes", "task_summary": "Write notes"}),
        _result("c2", "spawn", "Subagent [Write notes] started (id: 0000beef)."),
        _call("c3", "run_subagent_dag", {"task_summary": "Review fixes again"}),
        _result("c3", "run_subagent_dag", f"DAG run {RUN} started in the background (3 nodes)."),
    ]

    assert [d.handle for d in turn_dispatches(turn)] == [f"dag:{RUN}", "spawn:write_notes"]


def test_other_tools_and_unanswered_calls_are_ignored():
    turn = [
        _call("c1", "read_file", {"path": "x"}),
        _result("c1", "read_file", f"DAG run {RUN} started in the background (3 nodes)."),
        _call("c2", "spawn", {"node_id": "pending_one", "task_summary": "x"}),
    ]

    assert turn_dispatches(turn) == []


def test_the_receipt_readers():
    assert dag_run_id_of(f"DAG run {RUN} finished: 2 completed") == RUN
    assert dag_run_id_of(f"DAG {PLAYBOOK_RUN}: started 'x' (2 steps)") == PLAYBOOK_RUN
    assert dag_run_id_of("rejected: bad spec") is None
    assert spawn_task_id_of("Subagent [quotes started (id: 11111111)] started (id: 22222222).") == "22222222"
    assert spawn_task_id_of("Error: nope") is None


def test_a_dispatch_forwarded_through_tool_call_counts():
    # Under tool-search compaction run_subagent_dag is reached through the
    # tool_call meta-tool: the row names tool_call, the receipt is the graph's.
    turn = [
        _call("c1", "tool_call", {"name": "run_subagent_dag", "arguments": {"task_summary": "Review fixes"}}),
        _result("c1", "tool_call", f"DAG run {RUN} started in the background (3 nodes)."),
        _call("c2", "tool_call", {"name": "spawn", "arguments": {"node_id": "write_notes", "task_summary": "Notes"}}),
        _result("c2", "tool_call", "Subagent [Notes] started (id: 0000beef)."),
        _call("c3", "tool_call", {"name": "read_file", "arguments": {"path": "x"}}),
        _result("c3", "tool_call", f"DAG run {RUN} started in the background (3 nodes)."),
    ]

    assert turn_dispatches(turn) == [Dispatch(f"dag:{RUN}", "Review fixes"), Dispatch("spawn:write_notes", "Notes")]


def test_a_forwarded_dispatch_whose_inner_arguments_are_a_json_string_counts():
    """The forwarder parses a JSON string before dispatch, so the call runs and its receipt
    is real; the reader has to parse the same shape or the dispatch is silently dropped."""
    turn = [
        _call(
            "c1",
            "tool_call",
            {"name": "spawn", "arguments": json.dumps({"node_id": "write_notes", "task_summary": "Notes"})},
        ),
        _result("c1", "tool_call", "Subagent [Notes] started (id: 0000beef)."),
        _call(
            "c2",
            "tool_call",
            {"name": "run_subagent_dag", "arguments": json.dumps({"task_summary": "Review fixes"})},
        ),
        _result("c2", "tool_call", f"DAG run {RUN} started in the background (3 nodes)."),
    ]

    assert turn_dispatches(turn) == [
        Dispatch("spawn:write_notes", "Notes"),
        Dispatch(f"dag:{RUN}", "Review fixes"),
    ]


@pytest.mark.parametrize("name", ["message", "spawn"])
def test_a_rejected_deeply_nested_forward_does_not_hide_other_dispatches(name):
    turn = [
        _call("bad", "tool_call", {"name": name, "arguments": "[" * 10_000}),
        _result("bad", "tool_call", "Error: arguments are nested too deeply to read as JSON"),
        _call("good", "spawn", {"node_id": "write_notes", "task_summary": "Notes"}),
        _result("good", "spawn", "Subagent [Notes] started (id: 0000beef)."),
    ]

    assert turn_dispatches(turn) == [Dispatch("spawn:write_notes", "Notes")]


def test_a_delegated_result_names_the_run_it_brings_back():
    """The handle the turn that dispatched the run placed: a graph is placed whole, so a
    node's report names its run."""
    spawn = {"kind": "spawn", "label": "Locate it", "status": "ok", "node_id": "locate_login_bug"}
    node = {"kind": "dag", "label": RUN, "status": "error", "run_id": RUN, "node_id": "review"}

    assert delegated_handle(spawn) == "spawn:locate_login_bug"
    assert delegated_handle({"kind": "dag", "label": RUN, "status": "ok", "run_id": RUN}) == f"dag:{RUN}"
    assert delegated_handle(node) == f"dag:{RUN}"


def test_a_result_with_no_run_to_name_names_none():
    assert delegated_handle(None) is None
    assert delegated_handle({"kind": "spawn", "label": "Nightly check", "status": "ok"}) is None
    assert delegated_handle({"kind": "dag", "label": RUN, "status": "ok"}) is None
    assert delegated_handle({"kind": "unprompted", "label": "coder/x1", "status": "report", "node_id": "x"}) is None
