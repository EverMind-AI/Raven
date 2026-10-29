"""Which spawn calls and DAG runs a turn dispatched, read from the turn's own tool rows."""

from __future__ import annotations

import json

from raven.agent.task_board.board import Dispatch
from raven.agent.task_board.dispatches import dag_run_id_of, spawn_task_id_of, turn_dispatches

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
