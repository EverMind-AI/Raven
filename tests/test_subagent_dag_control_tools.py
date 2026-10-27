"""Model-facing controls for an in-flight DAG run (control_tools.py).

cancel_dag / dag_status / resolve_dag_node are schema-hidden tools: the provider
never sees them, so two texts are their whole advertisement -- the DAG tool's
acceptance text, and a suspended node's exception report for resolve_dag_node --
and each names tool_call, the only route by which a model can invoke a tool that
is not in its list. The tools are exercised directly, their conversation scoping
is asserted, their own cross-references are held to naming that route, and the
hiding surface (registry definitions + the tool_search catalog) is pinned to keep
them out of every discovery path except those texts.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.agent.loop.wiring import WiringMixin
from raven.agent.subagent import dag_tool as raven_agent_subagent
from raven.agent.subagent.dag_adjudication import ABANDON, CONTINUE, REASSIGN, REPLAN, ReassignPlan, ReplanPlan
from raven.agent.subagent.dag_control_tools import CancelDagTool, DagStatusTool, ResolveDagNodeTool
from raven.agent.subagent.dag_graph import parse_dag_spec
from raven.agent.subagent.dag_live import awaiting_decision, resolve_node
from raven.agent.subagent.dag_reader import DagReadError
from raven.agent.subagent.dag_runner import _exception_report
from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.agent.subagent.dag_verdict import Verdict
from raven.agent.subagent.delegate import DelegateTable, Worker, delegate_scope, outbound_charter
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.tool_search import TOOL_CALL_NAME, ToolCallTool, ToolSearchController
from raven.config.schema import ThirdPartyCliSubagentConfig, ToolSearchConfig
from raven.contracts.tool import Tool
from raven.providers.base import LLMProvider, LLMResponse


class _Registry:
    def __init__(self, tool: Any) -> None:
        self._tool = tool

    def get(self, name: str) -> Any:
        return self._tool if name == "run_subagent_dag" else None


class _Loop:
    """A duck-typed host with the two accessors the tools ask through."""

    def __init__(self, live: set[str] | None = None, tool: Any = None) -> None:
        self._live = live or set()
        self.tools = _Registry(tool)
        self.cancel_calls: list[str] = []

    def active_dag_run_ids(self) -> set[str]:
        return set(self._live)

    def cancel_dag_run(self, run_id: str) -> bool:
        self.cancel_calls.append(run_id)
        return run_id in self._live


class _DagTool:
    def __init__(
        self,
        run: dict[str, Any] | None = None,
        error: str | None = None,
        session_runs: set[str] | None = None,
        fail_read_after: int | None = None,
    ) -> None:
        self._run = run
        self._error = error
        self._session_runs = session_runs
        self._fail_read_after = fail_read_after
        self.reads = 0

    async def read_run(self, run_id: str, session_key: str | None = None) -> dict[str, Any]:
        self.reads += 1
        if self._error is not None:
            raise DagReadError(self._error)
        if self._fail_read_after is not None and self.reads > self._fail_read_after:
            raise DagReadError("gone mid-race")
        if self._run is not None and self._run.get("run_id") == run_id:
            return self._run
        raise DagReadError(f"no readable DAG run for {run_id}")

    async def session_run_ids(self, session_key: str | None = None) -> set[str]:
        return set(self._session_runs or set())


def _finished_run() -> dict[str, Any]:
    long_template = "\n".join(f"line{i}" for i in range(1, 13))
    return {
        "run_id": "r1",
        "dir": "/tmp/runs/r1",
        "nodes_root": "/tmp/nodes",
        "finalized": True,
        "task_summary": "Plan, then write, then review.",
        "files": [
            {
                "node": "a",
                "status": "completed",
                "node_summary": "Draft the plan.",
                "subagent": "Coder",
                "inputs": {"topic": "x"},
                "prompt_template": long_template,
                "prompt_file": "/tmp/nodes/a.prompt.md",
                "instance": "i-1",
                "output_file": "/tmp/nodes/a.out.md",
                "memory_file": "/tmp/nodes/a.memory.json",
                "started_at": 1000,
                "ended_at": 3000,
            },
            {
                "node": "b",
                "status": "failed",
                "subagent": "Writer",
                "inputs": None,
                "prompt_template": None,
                "error": "boom",
            },
        ],
        "terminal_outputs": [],
        "summary": {"total": 2, "completed": 1, "failed": 1, "cancelled": 0, "skipped": 0},
    }


async def test_cancel_dag_reports_the_stop_with_the_nodes() -> None:
    tool = CancelDagTool(loop=_Loop(live={"r1"}, tool=_DagTool(_finished_run())))
    tool.set_context("web", "chat-1", "sess-1")

    out = await tool.execute("r1")

    assert "Cancellation requested for DAG run r1" in out
    assert "nothing further is announced" in out
    assert "task_summary: Plan, then write, then review." in out
    assert "- a [completed]" in out
    assert "- b [failed]" in out


async def test_cancel_dag_reports_the_head_alone_when_the_run_cannot_be_read_back() -> None:
    # Ownership check reads once; the reconciled read after the cancel is the
    # one that fails mid-race. The head must stand on its own then.
    tool = CancelDagTool(loop=_Loop(live={"r1"}, tool=_DagTool(_finished_run(), fail_read_after=1)))

    out = await tool.execute("r1")

    assert "Cancellation requested for DAG run r1" in out
    assert "- a [" not in out


async def test_cancel_dag_reports_when_nothing_matches() -> None:
    # Owned (resolves under this conversation) but not running.
    out = await CancelDagTool(loop=_Loop(tool=_DagTool(_finished_run()))).execute("r1")

    assert "No in-flight DAG run r1 to cancel" in out
    assert 'tool_call name "dag_status" with no run_id' in out


async def test_cancel_dag_refuses_when_ownership_cannot_be_resolved() -> None:
    # No graph tool registered means no session-scoped reader, so the gate must
    # fail closed rather than signal a cancel it cannot attribute.
    host = _Loop(live={"r1"})
    out = await CancelDagTool(loop=host).execute("r1")

    assert "Cannot cancel DAG run r1" in out
    assert "nothing was signalled" in out
    assert host.cancel_calls == []


async def test_cancel_dag_refuses_a_run_from_another_conversation() -> None:
    host = _Loop(live={"theirs"}, tool=_DagTool(_finished_run()))
    out = await CancelDagTool(loop=host).execute("theirs")

    assert "No DAG run theirs in this conversation" in out
    assert host.cancel_calls == [], "a foreign run id must never reach the cancel"


async def test_dag_status_lists_the_runs_in_flight_and_points_at_one() -> None:
    host = _Loop(live={"b-run", "a-run"}, tool=_DagTool(session_runs={"a-run", "b-run"}))
    out = await DagStatusTool(loop=host).execute()

    assert out == (
        'In-flight DAG runs: a-run, b-run. Call tool_call name "dag_status" '
        'arguments {"run_id": "<run_id>"} for one run\'s per-node status.'
    )


async def test_dag_status_listing_is_scoped_to_the_conversation() -> None:
    host = _Loop(live={"mine", "theirs"}, tool=_DagTool(session_runs={"mine"}))
    out = await DagStatusTool(loop=host).execute()

    assert "In-flight DAG runs: mine." in out
    assert "theirs" not in out


async def test_dag_status_listing_degrades_to_empty_when_the_index_is_unknown() -> None:
    host = _Loop(live={"mine"}, tool=_DagTool(session_runs=None))
    out = await DagStatusTool(loop=host).execute()

    assert out == "No DAG runs are currently in flight."


async def test_dag_status_reports_nothing_when_idle() -> None:
    out = await DagStatusTool(loop=_Loop()).execute()

    assert out == "No DAG runs are currently in flight."


async def test_dag_status_reads_one_run_back_with_every_node_field() -> None:
    tool = DagStatusTool(loop=_Loop(tool=_DagTool(_finished_run())))
    tool.set_context("web", "chat-1", "sess-1")

    out = await tool.execute("r1")

    assert "DAG run r1: 1 completed, 1 failed, 0 cancelled, 0 skipped (of 2)." in out
    assert "task_summary: Plan, then write, then review." in out
    assert "- a [completed]" in out
    assert "    node_summary: Draft the plan." in out
    assert "    subagent: Coder" in out
    assert '    inputs: {"topic": "x"}' in out
    assert "    instance: i-1" in out
    assert "    output_file: /tmp/nodes/a.out.md" in out
    assert "    memory_file: /tmp/nodes/a.memory.json" in out
    assert "    started_at: 1000" in out
    assert "    ended_at: 3000" in out
    # The long template is cut to its head and points at the full prompt file.
    assert "    prompt_template: line1" in out
    assert "line10" in out
    assert "line11" not in out
    assert "truncated, 2 more lines" in out
    assert "full prompt in /tmp/nodes/a.prompt.md" in out
    # A node whose fields never got written renders (none) for each of them.
    assert "- b [failed]" in out
    assert "    node_summary: (none)" in out
    assert "    inputs: (none)" in out
    assert "    prompt_template: (none)" in out
    assert "    instance: (none)" in out
    assert "    error: boom" in out
    assert "Run dir: /tmp/runs/r1" in out


async def test_dag_status_of_a_graph_without_branches_prints_exactly_what_it_always_did() -> None:
    out = await DagStatusTool(loop=_Loop(tool=_DagTool(_finished_run()))).execute("r1")

    assert out == "\n".join(
        [
            "DAG run r1: 1 completed, 1 failed, 0 cancelled, 0 skipped (of 2).",
            "task_summary: Plan, then write, then review.",
            "- a [completed]",
            "    node_summary: Draft the plan.",
            "    subagent: Coder",
            '    inputs: {"topic": "x"}',
            "    prompt_template: line1",
            *(f"    line{i}" for i in range(2, 11)),
            "    ... (truncated, 2 more lines; full prompt in /tmp/nodes/a.prompt.md)",
            "    instance: i-1",
            "    output_file: /tmp/nodes/a.out.md",
            "    memory_file: /tmp/nodes/a.memory.json",
            "    started_at: 1000",
            "    ended_at: 3000",
            "- b [failed]",
            "    node_summary: (none)",
            "    subagent: Writer",
            "    inputs: (none)",
            "    prompt_template: (none)",
            "    instance: (none)",
            "    output_file: (none)",
            "    memory_file: (none)",
            "    started_at: (none)",
            "    ended_at: (none)",
            "    error: boom",
            "Run dir: /tmp/runs/r1",
        ]
    )


async def test_dag_status_counts_the_nodes_itself_when_the_record_has_no_total() -> None:
    run = _finished_run()
    run["summary"] = {"completed": 1, "failed": 1}

    out = await DagStatusTool(loop=_Loop(tool=_DagTool(run))).execute("r1")

    assert "DAG run r1: 1 completed, 1 failed, 0 cancelled, 0 skipped (of 2)." in out


def _branching_run() -> dict[str, Any]:
    """A finished run that took one branch: `fix` was not executed, one `select: many`
    decision picked two routes and another picked none."""
    return {
        "run_id": "r2",
        "dir": "/tmp/runs/r2",
        "nodes_root": "/tmp/nodes",
        "finalized": True,
        "task_summary": "Triage, then fix or write.",
        "files": [
            {
                "node": "triage",
                "status": "completed",
                "subagent": "Coder",
                "selected_routes": ["docs"],
                "route_evidence": "the judge's quote of the sub-agent",
            },
            {"node": "fix", "status": "not_executed", "subagent": "Coder", "reason": "when_false"},
            {"node": "write", "status": "completed", "subagent": "Writer"},
            {"node": "scan", "status": "completed", "subagent": "Coder", "selected_routes": ["a", "b"]},
            {"node": "sweep", "status": "completed", "subagent": "Coder", "selected_routes": []},
        ],
        "terminal_outputs": [],
        "summary": {"total": 5, "completed": 4, "failed": 0, "cancelled": 0, "skipped": 0, "not_executed": 1},
    }


def _node_block(out: str, node: str) -> list[str]:
    """The lines `dag_status` printed for one node: its `- id [status]` line up to the next node's."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"- {node} ["))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(("- ", "Run dir:"))), len(lines))
    return lines[start:end]


async def test_dag_status_counts_a_node_that_was_not_executed_and_says_why() -> None:
    out = await DagStatusTool(loop=_Loop(tool=_DagTool(_branching_run()))).execute("r2")

    assert "DAG run r2: 4 completed, 0 failed, 0 cancelled, 0 skipped, 1 not executed (of 5)." in out
    assert "    reason: when_false" in _node_block(out, "fix")
    assert [line for line in out.splitlines() if line.startswith("    reason:")] == ["    reason: when_false"]


async def test_dag_status_counts_the_not_executed_nodes_of_a_record_with_no_total() -> None:
    run = _branching_run()
    run["summary"] = {"completed": 4, "not_executed": 1}

    out = await DagStatusTool(loop=_Loop(tool=_DagTool(run))).execute("r2")

    assert "DAG run r2: 4 completed, 0 failed, 0 cancelled, 0 skipped, 1 not executed (of 5)." in out


async def test_dag_status_shows_what_each_decision_selected() -> None:
    out = await DagStatusTool(loop=_Loop(tool=_DagTool(_branching_run()))).execute("r2")

    assert "    selected_routes: docs" in _node_block(out, "triage")
    assert "    selected_routes: a, b" in _node_block(out, "scan")
    assert "    selected_routes: (none)" in _node_block(out, "sweep"), "selecting nothing is itself a decision"
    assert [line for line in out.splitlines() if line.startswith("    selected_routes:")] == [
        "    selected_routes: docs",
        "    selected_routes: a, b",
        "    selected_routes: (none)",
    ]


async def test_dag_status_treats_a_null_selected_routes_as_no_selection() -> None:
    run = _branching_run()
    run["files"] = [{"node": "x", "status": "completed", "subagent": "Coder", "selected_routes": None}]

    out = await DagStatusTool(loop=_Loop(tool=_DagTool(run))).execute("r2")

    assert "selected_routes" not in out


async def test_dag_status_never_prints_the_judges_route_evidence() -> None:
    out = await DagStatusTool(loop=_Loop(tool=_DagTool(_branching_run()))).execute("r2")

    assert "the judge's quote" not in out
    assert "route_evidence" not in out


def _unfinalized_branching_run() -> dict[str, Any]:
    """`_branching_run()`'s first two nodes as read before the manifest exists: every node pending."""
    files = [
        {"node": node, "status": "pending", "subagent": "Coder", "started_at": None, "ended_at": None}
        for node in ("triage", "fix")
    ]
    return {"run_id": "r3", "dir": "/tmp/runs/r3", "nodes_root": "/tmp/nodes", "finalized": False, "files": files}


async def test_a_reassigned_node_reloads_as_the_attempt_that_ran_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reloaded page has no events to replay. The registry row is the durable
    record of who is running the node, and the graph's submitted agent is not."""
    from raven.agent.subagent import instances as instances_mod
    from raven.agent.subagent.dag_resume import read_run_reconciled

    registry = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    monkeypatch.setattr(instances_mod, "_registry", registry)
    await registry.upsert_dag_node("tui:s1", "r3", "fix", "Coder", "exception")
    await registry.move_dag_node(
        "tui:s1",
        run_id="r3",
        node_id="fix",
        old_agent="Coder",
        new_agent="Writer",
        status="running",
        instance="fix-minted",
        instance_auto=True,
        attempt_agents=["Coder", "Writer"],
    )

    reconciled = await read_run_reconciled(
        _DagTool(_unfinalized_branching_run()), "r3", "tui:s1", live_runs=lambda: {"r3"}
    )

    fix = {entry["node"]: entry for entry in reconciled["files"]}["fix"]
    assert (fix["subagent"], fix["instance"]) == ("Writer", "fix-minted")
    assert fix["instance_auto"] is True
    assert fix["attempt_agents"] == ["Coder", "Writer"]
    assert fix["status"] == "running"

    # A gateway restart is a reader with nothing live left: the status settles,
    # the identity the row recorded does not.
    lost = await read_run_reconciled(_DagTool(_unfinalized_branching_run()), "r3", "tui:s1", live_runs=lambda: set())
    stale = {entry["node"]: entry for entry in lost["files"]}["fix"]
    assert stale["status"] == "interrupted"
    assert (stale["subagent"], stale["instance"], stale["attempt_agents"]) == (
        "Writer",
        "fix-minted",
        ["Coder", "Writer"],
    )


async def test_a_node_that_never_moved_reloads_with_the_keys_it_always_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import instances as instances_mod
    from raven.agent.subagent.dag_resume import read_run_reconciled

    registry = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    monkeypatch.setattr(instances_mod, "_registry", registry)
    await registry.upsert_dag_node("tui:s1", "r3", "fix", "Coder", "completed")

    reconciled = await read_run_reconciled(
        _DagTool(_unfinalized_branching_run()), "r3", "tui:s1", live_runs=lambda: {"r3"}
    )

    fix = {entry["node"]: entry for entry in reconciled["files"]}["fix"]
    assert not {"attempt_agents", "instance_auto"} & set(fix)


async def test_dag_status_of_a_run_still_going_says_what_a_branch_decided_but_never_the_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-run the outcome comes off the registry rows, quote and all, and the model
    still reads only the reason and the selection: the quote is sub-agent text."""
    from raven.agent.subagent import instances as instances_mod
    from raven.agent.subagent.dag_resume import read_run_reconciled

    registry = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    monkeypatch.setattr(instances_mod, "_registry", registry)
    quote = "the judge's quote of the sub-agent"
    await registry.upsert_dag_node(
        "tui:s1", "r3", "triage", "Coder", "completed", selected_routes=["docs"], route_evidence=quote
    )
    await registry.upsert_dag_node("tui:s1", "r3", "fix", "Coder", "not_executed", reason="when_false")

    reconciled = await read_run_reconciled(
        _DagTool(_unfinalized_branching_run()), "r3", "tui:s1", live_runs=lambda: {"r3"}
    )
    assert reconciled["files"][0]["route_evidence"] == quote, "the quote is on the run the renderer is handed"

    tool = DagStatusTool(loop=_Loop(live={"r3"}, tool=_DagTool(_unfinalized_branching_run())))
    tool.set_context("tui", "s1", session_key="tui:s1")
    out = await tool.execute("r3")

    assert "    selected_routes: docs" in _node_block(out, "triage")
    assert "    reason: when_false" in _node_block(out, "fix")
    assert quote not in out
    assert "route_evidence" not in out


async def test_dag_status_derives_the_prompt_path_when_truncated_without_one() -> None:
    run = _finished_run()
    run["files"][0]["prompt_file"] = None
    tool = DagStatusTool(loop=_Loop(tool=_DagTool(run)))

    out = await tool.execute("r1")

    assert "truncated, 2 more lines" in out
    assert "full prompt in /tmp/nodes/a.prompt.md" in out


async def test_dag_status_reports_an_unknown_run() -> None:
    tool = DagStatusTool(loop=_Loop(tool=_DagTool(error="no readable DAG run")))

    out = await tool.execute("r9")

    assert "No DAG run r9 found" in out
    assert 'tool_call name "dag_status" with no run_id' in out


class _ResolvableDagTool(_DagTool):
    """A run_subagent_dag double: read_run succeeds, resolve_node is recorded, and a
    foreground run hands scripted events to await_run. `calls` records the replan
    hops in the order they ran, for tests that check emit_replanned lands between
    resolve_node and await_finalized rather than just checking each one happened."""

    def __init__(
        self,
        resolves: bool = True,
        *,
        foreground: bool = False,
        events: list[Any] | None = None,
        plan: Any = None,
        raise_on_emit: BaseException | None = None,
        raise_on_finalize: BaseException | None = None,
        awaiting: bool = True,
    ) -> None:
        super().__init__(_finished_run())
        self._resolves = resolves
        self._foreground = foreground
        self.events: list[Any] = list(events or [])
        self.resolved: tuple[str, str, str, str | None] | None = None
        self.awaited: list[str] = []
        self.aborted: list[str] = []
        self.release_taker = asyncio.Event()
        self._plan = plan
        self._raise_on_emit = raise_on_emit
        self._raise_on_finalize = raise_on_finalize
        self.calls: list[str] = []
        self.is_foreground_calls: list[str] = []
        self.awaiting_calls: list[tuple[str, str]] = []
        self._awaiting = awaiting
        self.interrupted: tuple[str, Any, str] | None = None
        self.reassign_plan: Any = None
        self.reassign_refusal: str | None = None
        self.commit_ok: bool = True
        self.reassign_calls: list[tuple[str, str, str, str, str | None]] = []
        self.commit_calls: list[tuple[str, Any, str]] = []

    def is_awaiting_decision(self, run_id: str, node_id: str) -> bool:
        self.awaiting_calls.append((run_id, node_id))
        return self._awaiting

    def resolve_node(self, run_id: str, node_id: str, decision: str, message: str | None, plan: Any = None) -> bool:
        # A replan's plan is not folded into `resolved`: every existing caller that
        # drives continue/abandon through this double asserts the 4-tuple exactly,
        # and none of them cares that the parameter now exists.
        self.calls.append("resolve_node")
        self.resolved = (run_id, node_id, decision, message)
        return self._resolves

    def is_foreground(self, run_id: str) -> bool:
        self.is_foreground_calls.append(run_id)
        return self._foreground and run_id == "r1"

    def active_run_ids(self) -> list[str]:
        return ["r1"]

    async def await_run(self, run_id: str) -> Any:
        self.awaited.append(run_id)
        if not self.is_foreground(run_id):
            return None
        if self.events:
            return self.events.pop(0)
        await self.release_taker.wait()
        return None

    def abort_run(self, run_id: str) -> None:
        self.aborted.append(run_id)

    def render_event(self, run_id: str, event: Any) -> str:
        return f"RENDERED {run_id} {event}"

    async def prepare_replan(
        self, run_id: str, from_node: str, nodes: list, reason: str, session_key: str | None, live: dict
    ) -> Any:
        self.calls.append("prepare_replan")
        return self._plan

    async def emit_replanned(self, run_id: str, plan: Any) -> None:
        self.calls.append("emit_replanned")
        if self._raise_on_emit is not None:
            raise self._raise_on_emit

    async def await_finalized(self, run_id: str) -> None:
        self.calls.append("await_finalized")
        if self._raise_on_finalize is not None:
            raise self._raise_on_finalize

    async def record_interrupted_replan(self, run_id: str, plan: Any, reason: str) -> None:
        self.calls.append("record_interrupted_replan")
        self.interrupted = (run_id, plan, reason)

    async def start_replan(self, run_id: str, plan: Any, *, bound: bool = False) -> Any:
        self.calls.append("start_replan")
        return f"replan started as {plan.run_id}"

    async def prepare_reassign(
        self, run_id: str, node_id: str, agent: str, message: str, session_key: str | None
    ) -> Any:
        self.calls.append("prepare_reassign")
        self.reassign_calls.append((run_id, node_id, agent, message, session_key))
        if self.reassign_refusal is not None:
            return self.reassign_refusal
        return self.reassign_plan

    def commit_reassign(self, run_id: str, prepared: Any, message: str) -> bool:
        self.calls.append("commit_reassign")
        self.commit_calls.append((run_id, prepared, message))
        return self.commit_ok


class _LoopWithRun(_Loop):
    """A loop whose registered graph tool owns run r1 and answers resolve_node."""

    def __init__(self, resolves: bool = True, **kwargs: Any) -> None:
        self._dag_tool = _ResolvableDagTool(resolves=resolves, **kwargs)
        super().__init__(tool=self._dag_tool)

    @property
    def resolved(self) -> tuple[str, str, str, str | None] | None:
        return self._dag_tool.resolved


class _LoopWithoutRun(_Loop):
    """A loop whose registered graph tool cannot resolve run r1 for this session."""

    def __init__(self) -> None:
        super().__init__(tool=_DagTool(error="no readable DAG run for r1"))


class _NonOwningDagTool(_ResolvableDagTool):
    """Registered on the model's table, but never dispatched r1: read_run still
    answers from the shared session index (a real host's two instances agree on
    that), but active_run_ids admits nothing -- the shape owning_tool has to
    skip past rather than settle for."""

    def active_run_ids(self) -> list[str]:
        return []


class _LoopWithSecondOwner(_Loop):
    """Two graph tool instances, the shape dag_live's module docstring describes:
    one on the model's table, one privately dispatching r1 (a playbook engine's
    private instance, in reality). Both read r1's history the same way, but only
    the private instance's active_run_ids admits owning it, so owning_tool's
    fan-out -- not the registered-tool shortcut every _LoopWithRun test above
    takes -- is what every prepare_replan / is_foreground / emit_replanned /
    await_finalized / start_replan call below has to go through.
    """

    def __init__(self, resolves: bool = True, plan: Any = None) -> None:
        self._registered = _NonOwningDagTool(resolves=resolves, plan=plan)
        self._owner = _ResolvableDagTool(resolves=resolves, foreground=True, plan=plan)
        super().__init__(tool=self._registered)

    def dag_tools(self) -> list[Any]:
        return [self._registered, self._owner]


async def test_resolve_requires_a_message_when_continuing():
    # "message" alone also matches the CONTINUE success text ("...with your
    # message."), so pin the exact guard text and confirm resolve_node was
    # never reached -- either alone would already catch a deleted guard.
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a", decision="continue")
    assert out == (
        "Error: continue on node 'a' needs a message telling it what to do differently. "
        "Supply what the report said was missing."
    )
    assert loop.resolved is None, "an unmet guard must never reach resolve_node"


async def test_resolve_rejects_an_unknown_decision():
    # A loose "continue" and "abandon" substring check also matches the abandon
    # success text ("...continues... abandoned..."), so pin the exact guard
    # text and confirm resolve_node was never reached.
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a", decision="maybe", message="x")
    assert out == "Error: decision must be 'continue', 'abandon', 'replan' or 'reassign', not 'maybe'."
    assert loop.resolved is None, "an unmet guard must never reach resolve_node"


async def test_resolve_refuses_a_run_this_conversation_does_not_own():
    tool = ResolveDagNodeTool(loop=_LoopWithoutRun())
    out = await tool.execute(run_id="r1", node_id="a", decision="abandon")
    assert "No DAG run r1 in this conversation" in out


async def test_resolve_says_when_nobody_is_waiting():
    tool = ResolveDagNodeTool(loop=_LoopWithRun(resolves=False))
    out = await tool.execute(run_id="r1", node_id="a", decision="abandon")
    assert "no longer waiting" in out.lower()


async def test_resolve_confirms_a_continue():
    # "a" in out matches almost any sentence, including the abandon text, so it
    # would not notice the continue/abandon response branches being swapped.
    # Pin the exact continue-only wording instead.
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a", decision="continue", message="use staging")
    assert out == "Node 'a' of run r1 will run again with your message."
    assert loop.resolved == ("r1", "a", "continue", "use staging")


async def test_resolve_blocks_only_for_a_bound_foreground_run():
    background = ResolveDagNodeTool(loop=_LoopWithRun())
    foreground = ResolveDagNodeTool(loop=_LoopWithRun(foreground=True))

    assert background.blocking_for({"run_id": "r1"}) is False
    assert foreground.blocking_for({"run_id": "r1"}) is True
    assert foreground.blocking_for({"run_id": "other"}) is False
    assert ResolveDagNodeTool(loop=_Loop()).blocking_for({"run_id": "r1"}) is False, (
        "no graph tool, nothing to block on"
    )


async def test_a_foreground_resolve_returns_the_next_event_rendered():
    loop = _LoopWithRun(foreground=True, events=["next report"])
    tool = ResolveDagNodeTool(loop=loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="continue", message="use staging")

    assert loop.resolved == ("r1", "a", "continue", "use staging")
    assert loop._dag_tool.awaited == ["r1"]
    assert out == "RENDERED r1 next report"


async def test_a_background_resolve_does_not_wait():
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="continue", message="use staging")

    assert out == "Node 'a' of run r1 will run again with your message."
    assert loop._dag_tool.awaited == ["r1"], "asked, and told there is nothing to wait on"


async def test_a_refused_resolve_never_waits():
    loop = _LoopWithRun(resolves=False, foreground=True, events=["would be wrong"])
    tool = ResolveDagNodeTool(loop=loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="abandon")

    assert "no longer waiting" in out.lower()
    assert loop._dag_tool.awaited == []


async def test_cancelling_a_waiting_resolve_aborts_the_run():
    loop = _LoopWithRun(foreground=True)
    tool = ResolveDagNodeTool(loop=loop)
    call = asyncio.create_task(tool.execute(run_id="r1", node_id="a", decision="abandon"))
    await asyncio.sleep(0)
    assert loop._dag_tool.awaited == ["r1"]

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert loop._dag_tool.aborted == ["r1"]


class _Hidden(Tool):
    @property
    def name(self) -> str:
        return "hidden_tool"

    @property
    def description(self) -> str:
        return "Hidden from the schema."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "hidden ran"


class _HiddenTwo(_Hidden):
    @property
    def name(self) -> str:
        return "hidden_tool_two"


class _Dummy(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "dummy"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


async def test_hidden_tools_leave_the_schema_but_stay_callable_and_unsearchable() -> None:
    registry = ToolRegistry()
    registry.register(_Hidden())
    registry.hide_from_schema("hidden_tool")

    assert "hidden_tool" not in {t["function"]["name"] for t in registry.get_definitions()}

    ctrl = ToolSearchController(registry, always_visible=set())
    ctrl.refresh()
    assert ctrl.search("hidden tool") == []
    assert ctrl.resolve_target("hidden_tool").tool is not None

    assert await registry.execute("hidden_tool", {}) == "hidden ran"


def test_tool_call_availability_answers_whether_tool_call_ships() -> None:
    registry = ToolRegistry()
    registry.register(_Hidden())
    registry.register(_HiddenTwo())
    # A threshold far above this catalog: the fold would never engage here, and
    # the answer must not depend on that -- a schema-hidden tool needs the name
    # route at every catalog size, which is why the predicate stopped reading it.
    ctrl = ToolSearchController(registry, always_visible=set(), compaction_threshold=99)

    # Without the meta-tool registered there is no route.
    assert ctrl.tool_call_available() is False

    registry.register(ToolCallTool(ctrl))
    assert ctrl.tool_call_available() is True

    # An operator off switch on the meta-tool closes the route again.
    registry.set_withheld_source(lambda: frozenset({TOOL_CALL_NAME}))
    assert ctrl.tool_call_available() is False


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="stub", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace() -> Path:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


async def test_the_loop_registers_all_three_tools_outside_the_schema(workspace: Path) -> None:
    loop = AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True, tool_search_config=ToolSearchConfig(enabled=True)),
    )

    assert loop.tools.has("cancel_dag")
    assert loop.tools.has("dag_status")
    assert loop.tools.has("resolve_dag_node")
    names = {t["function"]["name"] for t in loop.tools.get_definitions()}
    assert "cancel_dag" not in names
    assert "dag_status" not in names
    assert "resolve_dag_node" not in names

    out = await loop.tools.execute("cancel_dag", {"run_id": "r1"})
    assert "No DAG run r1 in this conversation" in out

    out = await loop.tools.execute("resolve_dag_node", {"run_id": "r1", "node_id": "a", "decision": "abandon"})
    assert "No DAG run r1 in this conversation" in out

    assert loop.dag_control_reachable() is True, "tool_call carries the route to the hidden tools"

    for i in range(51):
        loop.tools.register(_Dummy(f"dummy_{i}"))
    assert loop.dag_control_reachable() is True, "and still does once the catalog is above the fold"


async def test_a_default_loop_can_still_reach_the_hidden_tools(workspace: Path) -> None:
    # tool_call used to ride the fold's switch, which left all three controls
    # advertised but unnameable wherever the fold was off -- a suspended node
    # then waited out its whole adjudication timeout for a decision the model
    # had no way to send. Both states are pinned because the switch moved: the
    # factory default now folds, and a deploy that turns it off must keep the
    # route the bug was about.
    def _loop(cfg) -> AgentLoop:
        return AgentLoop(
            provider=_StubProvider(),
            workspace=workspace,
            model="stub",
            policy=TurnPolicy(max_iterations=2),
            tools=ToolWiring(restrict_to_workspace=True, tool_search_config=cfg),
        )

    # No tool_search_config: the default deploy, which takes the schema's own.
    default = _loop(None)
    assert default.tools.has("tool_call")
    assert default.strategies.get("tool_search") is not None, "the factory default folds"
    assert default.dag_control_reachable() is True

    off = _loop(ToolSearchConfig(enabled=False))
    assert off.tools.has("tool_call")
    assert off.strategies.get("tool_search") is None
    assert off.dag_control_reachable() is True


_HIDDEN = ("cancel_dag", "dag_status", "resolve_dag_node")


async def _cross_referencing_texts(tmp_path) -> list[tuple[str, str]]:
    """Every model-facing string that points a model at one of the hidden controls.

    The graph tool's acceptance text belongs here as much as the control tools' own
    replies: this file's docstring calls those two texts the whole advertisement the
    controls get, and a guard that reaches only one of them lets the defect this
    branch removes come back in the other with the suite green.
    """
    graph = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        control_reachable=lambda: True,
    )
    graph.set_context("web", "default", "web:xref")
    accepted = await graph.execute(
        task_summary="run the graph under test",
        nodes=[{"id": "a", "subagent": "echo", "node_summary": "say hello", "prompt_template": "hi"}],
    )
    guide = (
        Path(raven_agent_subagent.__file__).parents[2]
        / "memory_engine"
        / "skills"
        / "subagent-dag-orchestration"
        / "SKILL.md"
    )
    texts = [
        ("graph tool: background acceptance text", accepted.model_text),
        # run_subagent_dag makes reading this a required first step, so it is the
        # first place a model learns how to answer a suspended node -- earlier than
        # the report, and wrong for longer if it disagrees with it.
        ("orchestration guide: the required first read", guide.read_text(encoding="utf-8")),
        # The text a suspended node hands the model, and the one this branch is
        # named after. It renders without a tool instance, so it joins the roster
        # the same way the guide does.
        (
            "exception report: what a suspended node hands the model",
            _exception_report(
                run_id="r1",
                node=parse_dag_spec(
                    {
                        "task_summary": "one node",
                        "nodes": [{"id": "a", "subagent": "echo", "node_summary": "first", "prompt_template": "do a"}],
                    }
                ).nodes[0],
                verdict=Verdict(accomplished=False, category="tool_failure", what_is_missing="a token"),
                attempt=1,
                remaining=2,
                blocked=["b"],
                timeout_s=600.0,
            ),
        ),
        ("cancel: unresolvable ownership", await CancelDagTool(loop=_Loop(live={"r1"})).execute("r1")),
        ("cancel: nothing matches", await CancelDagTool(loop=_Loop(tool=_DagTool(_finished_run()))).execute("r9")),
        (
            "status: listing",
            await DagStatusTool(loop=_Loop(live={"a-run"}, tool=_DagTool(session_runs={"a-run"}))).execute(),
        ),
        ("status: unknown run", await DagStatusTool(loop=_Loop(tool=_DagTool(error="gone"))).execute("r1")),
        (
            "resolve: node no longer waiting",
            await ResolveDagNodeTool(loop=_LoopWithRun(resolves=False)).execute(
                run_id="r1", node_id="a", decision="abandon"
            ),
        ),
        (
            "resolve: abandoned",
            await ResolveDagNodeTool(loop=_LoopWithRun()).execute(run_id="r1", node_id="a", decision="abandon"),
        ),
        (
            "resolve: decision parameter",
            ResolveDagNodeTool(loop=_LoopWithRun()).parameters["properties"]["decision"]["description"],
        ),
    ]
    return [(label, text) for label, text in texts if any(name in text for name in _HIDDEN)]


async def test_every_cross_reference_between_the_controls_names_the_route(tmp_path) -> None:
    """These tools point the model at each other, and none of them is in its schema.

    Naming a sibling as a bare call spells an invocation the model cannot issue --
    the same defect that made a suspended node wait out its adjudication timeout,
    one layer in. A reader here has necessarily reached this tool through
    ``tool_call`` already, so the fix is consistency of spelling rather than a
    repeated explanation.
    """
    referencing = await _cross_referencing_texts(tmp_path)
    assert len(referencing) >= 9, "the sites under test must actually still cross-reference"

    offenders = [label for label, text in referencing if "tool_call" not in text]
    assert offenders == [], f"these name a schema-hidden tool without naming the route: {offenders}"


async def test_no_cross_reference_spells_a_bare_call_syntax(tmp_path) -> None:
    """`dag_status("r1")` reads as a callable form. There is no such call to make."""
    for label, text in await _cross_referencing_texts(tmp_path):
        for name in _HIDDEN:
            # The offending line, not the whole text: one of these is a file.
            offending = [ln.strip() for ln in text.splitlines() if f'{name}("' in ln]
            assert not offending, f"{label} spells a call syntax the model cannot use: {offending[:3]}"


def test_the_resolve_tool_parks_its_taker_before_it_yields() -> None:
    """A structural claim, read structurally, because behaviour cannot pin it.

    `_retire` drops a finished run's outbox from the index `release_turn` reaches
    runs through, so a run that completes before a taker exists has nowhere left to
    deliver its result. Nothing opens that window because this tool signals the desk
    and enters `await_run` with no yield between the two.

    `test_a_resolve_through_the_control_tool_is_handed_the_run_it_completes` drives
    that call site and catches a yield that lasts long enough to lose the race. It
    cannot catch every yield, and the claim is about every yield: `await
    asyncio.sleep(0)` inserted here passes a single tick, the run has not finished,
    the outbox is still indexed, and the delivery test stays green while the
    invariant it stands for is gone. So this one reads the source instead -- the
    first await after the resolve must be the take itself.
    """
    import ast
    from pathlib import Path

    import raven

    module = Path(raven.__file__).parent / "agent" / "subagent" / "dag_control_tools.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    execute = next(
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "ResolveDagNodeTool"
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute"
    )

    def _calls(node: ast.AST, name: str) -> list[ast.Call]:
        return [
            n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
        ]

    resolve_calls = _calls(execute, "resolve_node")
    # The reassign branch parks its own taker, and its hand-off is a commit
    # rather than a resolve; the claim here is about the continue/abandon call,
    # which sits above the tail's take.
    take = min(
        (call for call in _calls(execute, "await_run") if call.lineno > max(c.lineno for c in resolve_calls)),
        key=lambda call: call.lineno,
    )
    # The replan branch calls resolve_node too, to fold its plan into the same
    # decision record, but that call returns before the tail's await_run is ever
    # reached, so both calls precede the take -- this test's claim is about the
    # continue/abandon call, the textually-last one, immediately before it.
    resolve = max(resolve_calls, key=lambda c: c.lineno)
    assert resolve.lineno < take.lineno, "the continue/abandon resolve_node call must precede its own await_run take"
    awaits = sorted((n for n in ast.walk(execute) if isinstance(n, ast.Await)), key=lambda n: (n.lineno, n.col_offset))
    after_resolve = [n for n in awaits if n.lineno > resolve.lineno]

    assert after_resolve, "the taker's own await is missing, so this test is reading the wrong function"
    first = after_resolve[0]
    assert first.value is take, (
        f"a yield was added between the resolve and the take, at line {first.lineno}: "
        "the run can finish there with no taker registered, and `_retire` then drops the outbox "
        "it would have delivered into. Either keep them adjacent or stop claiming they are."
    )


async def test_preflight_refuses_a_disabled_agent_before_any_dispatch(tmp_path) -> None:
    """The refusal `_execute` produced inline must survive the extraction verbatim."""
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="coder", command="true", enabled=False)],
    )
    spec = parse_dag_spec(
        {
            "task_summary": "one step",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "coder",
                    "node_summary": "do it",
                    "prompt_template": "go",
                }
            ],
        }
    )

    with pytest.raises(DagValidationError) as exc:
        await tool._preflight(spec)

    assert "turned off on this machine" in str(exc.value)


def _resolve_tool(loop: Any = None) -> ResolveDagNodeTool:
    if loop is None:
        loop = _LoopWithRun()
    elif hasattr(loop, "node_schema"):
        # _registered_tool looks the graph tool up as loop.tools.get("run_subagent_dag"),
        # so a bare tool must be wrapped in a loop or the schema degrades to {"type": "object"}.
        loop = _Loop(tool=loop)
    tool = ResolveDagNodeTool(loop)
    tool.set_context("cli", "direct", None)
    return tool


def _node(node_id: str, *, depends_on: list[str] | None = None, prompt: str = "do it") -> dict[str, Any]:
    return {
        "id": node_id,
        "subagent": "x",
        "node_summary": "a step",
        "prompt_template": prompt,
        **({"depends_on": depends_on} if depends_on else {}),
    }


def _graph_tool() -> SubAgentDagTool:
    return SubAgentDagTool(
        workspace=Path(tempfile.gettempdir()),
        agents=[ThirdPartyCliSubagentConfig(name="x", command="true")],
    )


async def test_replan_without_nodes_is_refused() -> None:
    tool = _resolve_tool()

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", message="the plan was wrong")

    assert "needs a `nodes` list" in out
    assert "no decision was recorded" in out


async def test_replan_without_a_message_is_refused() -> None:
    tool = _resolve_tool()

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", nodes=[_node("fresh")])

    assert "needs a message" in out


async def test_an_unknown_decision_names_all_four() -> None:
    tool = _resolve_tool()

    out = await tool.execute(run_id="r1", node_id="a", decision="wat")

    assert "'continue'" in out
    assert "'abandon'" in out
    assert "'replan'" in out
    assert "'reassign'" in out


def test_the_decision_enum_advertises_the_four_decisions() -> None:
    params = _resolve_tool().parameters

    assert params["properties"]["decision"]["enum"] == ["continue", "abandon", "replan", "reassign"]


def test_the_description_names_the_reassign_decision() -> None:
    describe = _resolve_tool().description.lower()

    assert "reassign" in describe, "the hidden tool's only prose advertisement has to name the fourth decision"


def test_agent_and_message_stay_conditional_runtime_requirements() -> None:
    """`agent` is required for reassign and `message` for continue, reassign or
    replan, but the schema cannot say so with `required`: a model answering an
    ordinary question would then be told to send fields the decision does not
    read. The descriptions carry the conditions instead, and `agent` says it is
    read only by reassign -- through `tool_call` the declared enum rejects a
    stray value before the branch ever sees it."""
    params = _resolve_tool().parameters

    assert params["required"] == ["run_id", "node_id"]
    assert params["properties"]["agent"]["type"] == "string"
    agent_description = params["properties"]["agent"]["description"]
    assert "read only when reassigning" in agent_description
    assert "ignored" not in agent_description
    message_description = params["properties"]["message"]["description"]
    assert "required when continuing, reassigning or replanning" in message_description


def test_the_agent_enum_lists_only_enabled_roster_names(no_discovered_products) -> None:
    graph = SubAgentDagTool(
        workspace=Path(tempfile.gettempdir()),
        agents=[
            ThirdPartyCliSubagentConfig(name="alpha", command="true"),
            ThirdPartyCliSubagentConfig(name="switched_off", command="true", enabled=False),
        ],
    )
    params = _resolve_tool(graph).parameters

    enum = params["properties"]["agent"]["enum"]
    assert enum == graph.registry.names(), "the check the tool runs at admission is the one the enum shows"
    assert "alpha" in enum
    assert "switched_off" not in enum


def test_the_agent_enum_follows_a_hot_applied_roster(no_discovered_products) -> None:
    graph = _graph_tool()
    tool = _resolve_tool(graph)
    before = tool.parameters["properties"]["agent"]["enum"]
    assert before == graph.registry.names()

    graph.set_agents([ThirdPartyCliSubagentConfig(name="beta", command="true")])

    after = tool.parameters["properties"]["agent"]["enum"]
    assert after == graph.registry.names()
    assert after != before, "a snapshot taken at admission would still name the old roster"


def test_the_agent_enum_never_substitutes_worker_labels(no_discovered_products) -> None:
    """A turn's worker labels are what `subagent` takes; `agent` is a roster name."""
    table = DelegateTable(workers={"helper": Worker("helper", "alpha", "helps")})
    graph = SubAgentDagTool(
        workspace=Path(tempfile.gettempdir()),
        agents=[ThirdPartyCliSubagentConfig(name="alpha", command="true")],
        worker_table_for=lambda: table,
    )
    tool = _resolve_tool(graph)

    labels = graph.node_schema()["properties"]["subagent"]["enum"]
    enum = tool.parameters["properties"]["agent"]["enum"]
    assert labels == ["helper"], "the node schema really is serving the worker table here"
    assert "helper" not in enum
    assert enum == graph.registry.names()


async def test_execute_reads_agent_only_for_reassign() -> None:
    """The direct `execute` contract: the reassign branch is the only one that reads
    `agent`. This is not a promise about the model-facing schema -- through
    `tool_call` the declared enum rejects a stray value before `execute` sees it --
    so what is pinned here is the method a host or a test calls directly."""
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="abandon", agent="not_a_roster_name")

    assert "is abandoned" in out
    assert loop.resolved is not None and loop.resolved[2] == "abandon"


def test_the_nodes_parameter_reuses_the_graph_tools_node_schema() -> None:
    graph = _graph_tool()
    params = _resolve_tool(graph).parameters

    assert params["properties"]["nodes"]["items"] == graph.node_schema(), (
        "a copied node schema drifts from run_subagent_dag's"
    )


def _replan_plan() -> ReplanPlan:
    """A minimal, equality-stable plan: these tests check pass-through, not content."""
    return ReplanPlan(
        run_id="r2",
        from_node="a",
        reason="the plan was wrong",
        nodes=(),
        backends={},
        auto_instances=frozenset(),
        notices=(),
    )


async def test_the_plan_survives_both_hops_to_the_tool() -> None:
    seen: list[Any] = []

    class _Owner:
        def resolve_node(self, run_id, node_id, decision, message, plan=None):
            seen.append(plan)
            return True

    class _LoopWithMethod(_Loop):
        def resolve_dag_node(self, run_id, node_id, decision, message, plan=None):
            return any(t.resolve_node(run_id, node_id, decision, message, plan) for t in [_Owner()])

    assert resolve_node(_LoopWithMethod(), "r1", "a", REPLAN, "wrong", _replan_plan()) is True
    assert seen == [_replan_plan()], "the loop hop must not swallow the plan"


async def test_the_real_loop_hop_forwards_the_plan_too() -> None:
    """`_LoopWithMethod` above proves dag_live.resolve_node finds and calls a loop's
    resolve_dag_node when one exists; it never runs the real method's body, because
    that class hand-rolls its own copy rather than inheriting one. This subclasses
    the real `WiringMixin` and stubs only `dag_tools()` -- the one collaborator its
    actual resolve_dag_node touches -- so the forwarding call in that method's own
    body is what executes. Dropping `plan` from that call turns this test red; the
    other test would not notice.
    """
    seen: list[Any] = []

    class _Owner:
        def resolve_node(self, run_id, node_id, decision, message, plan=None):
            seen.append(plan)
            return True

    class _RealMixinHost(WiringMixin):
        def __init__(self, tool: Any) -> None:
            self._tool = tool

        def dag_tools(self):
            return [self._tool]

    assert resolve_node(_RealMixinHost(_Owner()), "r1", "a", REPLAN, "wrong", _replan_plan()) is True
    assert seen == [_replan_plan()], "the real resolve_dag_node must not swallow the plan"


async def test_a_successful_replan_emits_between_resolve_and_await_finalized() -> None:
    """The wire event fires once resolve_node succeeds, before await_finalized --
    the old run has not wound down yet at that point, which is what the web UI's
    live tracking (keyed by run_id, dropped on that run's own completion) needs."""
    plan = _replan_plan()
    loop = _LoopWithRun(plan=plan)
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._dag_tool.calls == [
        "prepare_replan",
        "resolve_node",
        "emit_replanned",
        "await_finalized",
        "start_replan",
    ]
    assert "replan started" in out


async def test_a_replan_nobody_waits_for_never_emits() -> None:
    """resolve_node returning False is a real, tested outcome elsewhere (an answer
    for a node that already got one) -- emitting for it would announce a replan
    that never happened, and nothing durable corrects that claim on this path."""
    plan = _replan_plan()
    loop = _LoopWithRun(resolves=False, plan=plan)
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._dag_tool.calls == ["prepare_replan", "resolve_node"]
    assert "no longer waiting for a decision" in out


async def test_a_replan_for_a_node_nobody_awaits_is_refused_before_it_costs_anything() -> None:
    """The cost of a replan is paid inside `prepare_replan` -- the confirm question,
    the dispatch quota, the minted instances -- and `resolve_node` is only reached
    afterwards. So a node that is plainly not suspended has to be turned away
    before that call, or a replan aimed at the wrong id spends a dispatch from the
    session's hourly budget and dispatches nothing: enough of those and a
    legitimate replan is refused for a budget the model never got to use.

    Distinct from `test_a_replan_nobody_waits_for_never_emits` above, which is the
    race -- the node stopped waiting *while* the replacement graph was being
    validated. That one still has to pay, and still has to refuse.
    """
    loop = _LoopWithRun(awaiting=False, plan=_replan_plan())
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._dag_tool.calls == [], "nothing that costs budget may run for a node nobody is waiting on"
    assert loop.resolved is None
    assert "no longer waiting for a decision" in out
    assert "still running as submitted" in out


async def test_the_two_not_waiting_refusals_read_identically() -> None:
    """One situation, one sentence: the model cannot act differently on "refused
    before validation" than on "refused after it", and a second wording would only
    invite it to try to."""
    early = await _resolve_tool(_LoopWithRun(awaiting=False, plan=_replan_plan())).execute(
        run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")]
    )
    raced = await _resolve_tool(_LoopWithRun(resolves=False, plan=_replan_plan())).execute(
        run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")]
    )

    assert early == raced


async def test_a_graph_tool_that_cannot_say_whether_a_node_waits_still_replans() -> None:
    """The predicate is advisory, like every other duck-typed hop in this module:
    a host built before it existed must keep replanning rather than have every
    replan refused by a missing attribute. `awaiting_decision` answers None there,
    which is why it is three-valued and why the caller tests `is False`."""

    class _NoPredicate(_ResolvableDagTool):
        is_awaiting_decision = None

    loop = _Loop(tool=_NoPredicate(plan=_replan_plan()))
    assert awaiting_decision(loop, "r1", "a") is None

    out = await _resolve_tool(loop).execute(
        run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")]
    )

    assert "replan started" in out


async def test_the_pre_check_asks_every_instance_the_hand_off_would() -> None:
    """The shape dag_live's module docstring describes: the run is held by the
    playbook engine's private instance, not by the one on the model's table.
    `WiringMixin.resolve_dag_node` answers from whichever instance holds it, so a
    pre-check that asked only the registered tool -- or only `owning_tool`, which
    falls back to it when no instance claims the run -- would refuse a replan the
    hand-off was going to accept.
    """

    class _Loop2(_Loop):
        def __init__(self) -> None:
            self._registered = _NonOwningDagTool(awaiting=False, plan=_replan_plan())
            self._owner = _ResolvableDagTool(awaiting=True, plan=_replan_plan())
            super().__init__(tool=self._registered)

        def dag_tools(self) -> list[Any]:
            return [self._registered, self._owner]

    loop = _Loop2()
    assert awaiting_decision(loop, "r1", "a") is True, "one instance saying yes settles it"

    out = await _resolve_tool(loop).execute(
        run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")]
    )

    assert "replan started" in out


async def test_an_instance_that_raises_does_not_refuse_the_replan() -> None:
    """Same contract as every other helper in dag_live: advisory, never fatal. A
    raising instance must not be read as "not waiting" -- that would turn a broken
    predicate into a refusal the model cannot act on."""

    class _Raises(_ResolvableDagTool):
        def is_awaiting_decision(self, run_id: str, node_id: str) -> bool:
            raise RuntimeError("boom")

    loop = _Loop(tool=_Raises(plan=_replan_plan()))
    assert awaiting_decision(loop, "r1", "a") is None, "an instance that cannot answer has not answered no"

    out = await _resolve_tool(loop).execute(
        run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")]
    )

    assert "replan started" in out


async def test_continue_and_abandon_do_not_consult_the_predicate() -> None:
    """Neither charges anything, so neither needs the pre-check -- and adding one
    would turn `resolve_node`'s own False into a second, earlier refusal for a
    decision that was always free to attempt."""
    for decision, kwargs in (("continue", {"message": "try again"}), ("abandon", {})):
        loop = _LoopWithRun(awaiting=False)
        out = await _resolve_tool(loop).execute(run_id="r1", node_id="a", decision=decision, **kwargs)

        assert loop._dag_tool.awaiting_calls == [], decision
        assert "no longer waiting" not in out, decision


async def test_a_replan_resolves_through_the_owning_tool_not_just_the_registered_one() -> None:
    """dag_live's module docstring: two tool instances can be dispatching runs at
    once, and the registered one only shares the on-disk session index with the
    other -- its in-memory bookkeeping (_outboxes, _runs, _cancels) knows nothing
    about a run the other instance actually dispatched. Routing prepare_replan,
    is_foreground, emit_replanned, await_finalized and start_replan through the
    merely-registered instance instead of owning_tool's fan-out would silently
    read that instance's own empty bookkeeping (is_foreground always False,
    await_finalized returning at once with nothing to wait for) rather than
    raising, so this pins *which object* receives each call, not just that the
    calls happened -- the registered-tool shortcut every _LoopWithRun test above
    takes would pass this assertion trivially, since there the two are the same
    object.
    """
    plan = _replan_plan()
    loop = _LoopWithSecondOwner(plan=plan)
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._owner.calls == ["prepare_replan", "emit_replanned", "await_finalized", "start_replan"]
    assert loop._owner.is_foreground_calls == ["r1"]
    assert loop._registered.calls == ["resolve_node"]
    assert loop._registered.is_foreground_calls == []
    assert "replan started" in out


async def test_an_interrupted_emit_still_records_the_link_before_reraising() -> None:
    """The stretch between resolve_node succeeding and start_replan itself is not
    covered by start_replan's own _record_link -- emit_replanned and
    await_finalized run before it, and an interruption there (a /stop cancelling
    this call is realistic) must not leave the old run's graph.json silent about
    a replan the desk already answered. record_interrupted_replan has to fire
    with the exact reason, and the exception still has to propagate rather than
    being swallowed by that bookkeeping.
    """
    plan = _replan_plan()
    loop = _LoopWithRun(plan=plan, raise_on_emit=RuntimeError("boom"))
    tool = _resolve_tool(loop)

    with pytest.raises(RuntimeError, match="boom"):
        await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._dag_tool.calls == ["prepare_replan", "resolve_node", "emit_replanned", "record_interrupted_replan"]
    assert loop._dag_tool.interrupted == (
        "r1",
        plan,
        "Interrupted between the node hand-off and starting the replan.",
    )


async def test_a_cancelled_await_finalized_still_records_the_link_and_reraises() -> None:
    """CancelledError is the realistic shape of the interruption record_interrupted_replan
    exists for (a /stop mid-wait): it still has to propagate, not be swallowed by
    the wind-down bookkeeping, or the caller waiting on this call would never
    learn the turn was cancelled.
    """
    plan = _replan_plan()
    loop = _LoopWithRun(plan=plan, raise_on_finalize=asyncio.CancelledError())
    tool = _resolve_tool(loop)

    with pytest.raises(asyncio.CancelledError):
        await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._dag_tool.calls == [
        "prepare_replan",
        "resolve_node",
        "emit_replanned",
        "await_finalized",
        "record_interrupted_replan",
    ]
    assert loop._dag_tool.interrupted == (
        "r1",
        plan,
        "Interrupted between the node hand-off and starting the replan.",
    )


async def test_resolve_accepts_action_as_the_name_the_model_reaches_for():
    # Three runs in a row (2026-09-03/04) the main agent wrote `action` and lost
    # a call to "missing required decision". The schema keeps `decision`; the
    # alias is accepted and lands the same resolve.
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a", action="abandon")
    assert loop.resolved is not None, "the alias must reach resolve_node"
    assert "abandon" in out
    assert "decision" not in tool.parameters["required"]


async def test_resolve_with_neither_name_says_which_field_is_missing():
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a")
    assert out == "Error: decision is required: 'continue', 'abandon', 'replan' or 'reassign'."
    assert loop.resolved is None


class TestTheHiddenControlsAdvertiseTheirRealSchema:
    """These three are withheld from the provider schema, so the only thing the
    model ever reads about them is another tool's result text. That text used to
    retell the arguments by hand in three places, and a retelling is not a
    schema: the model reached for `action` where the tool declares `decision`.
    """

    def test_resolve_declares_its_schema_dynamic(self) -> None:
        """Its node shape carries a roster the agent table can change at runtime,
        so the snapshot the registry takes at admission would go stale."""
        from raven.agent.tools.registry import admit_tool

        assert admit_tool(ResolveDagNodeTool(loop=None)).schema_dynamic
        # The two static ones make no such claim, and should not.
        assert not admit_tool(CancelDagTool(loop=None)).schema_dynamic
        assert not admit_tool(DagStatusTool(loop=None)).schema_dynamic

    def test_the_advert_carries_every_field_the_tool_declares(self) -> None:
        """Asserted against the schema, not a literal: rename a field and this
        fails, instead of the advertisement quietly describing the old one."""
        from raven.agent.subagent.dag_control_advert import render

        tool = ResolveDagNodeTool(loop=_LoopWithNodeSchema())
        text = render(tool.to_schema())

        assert text is not None
        declared = tool.parameters["properties"]
        assert set(declared) >= {"run_id", "node_id", "decision", "message", "nodes"}, (
            "an empty or shrunken property set would make the loop below vacuous"
        )
        for field in declared:
            assert f'"{field}"' in text, f"{field} is declared but not advertised"
        for decision in tool.parameters["properties"]["decision"]["enum"]:
            assert decision in text

    def test_the_advert_points_at_the_graph_tools_node_shape_instead_of_copying_it(self) -> None:
        """`run_subagent_dag` is not hidden, so its node shape is already in the
        model's tool list; inlining it again cost ~845 tokens to repeat that."""
        from raven.agent.subagent.dag_control_advert import render

        tool = ResolveDagNodeTool(loop=_LoopWithNodeSchema())
        inlined = tool.to_schema()["function"]["parameters"]["properties"]["nodes"]["items"]
        text = render(tool.to_schema())

        assert text is not None
        assert "run_subagent_dag" in text, "the reference has to name where the shape is"
        # Above the compaction threshold the graph tool is cataloged rather than
        # offered (it is deliberately absent from DEFAULT_ALWAYS_VISIBLE), so a
        # pointer that only asserts presence is false in that mode.
        assert "tool_search" in text, "the pointer must name a route true in both disclosure modes"
        assert "prompt_template" not in text, "the node shape must not be inlined here"
        assert "prompt_template" in str(inlined), "...but it is what was inlined before"
        assert len(text) < len(render_without_dedupe(tool)), "the reference must be the smaller one"

    def test_rendering_leaves_the_registrys_own_copy_alone(self) -> None:
        from raven.agent.subagent.dag_control_advert import render

        tool = ResolveDagNodeTool(loop=_LoopWithNodeSchema())
        definition = tool.to_schema()
        render(definition)

        assert "items" in definition["function"]["parameters"]["properties"]["nodes"]
        assert "prompt_template" in str(definition["function"]["parameters"]["properties"]["nodes"]["items"])


class _GraphOnTheTable:
    """The graph tool as ``_registered_tool`` finds it, serving the real node schema."""

    def node_schema(self) -> dict[str, Any]:
        from raven.agent.subagent.dag_tool import _NODE_SCHEMA

        return _NODE_SCHEMA


class _NodeSchemaToolTable:
    def get(self, name: str) -> Any:
        return _GraphOnTheTable() if name == "run_subagent_dag" else None


class _LoopWithNodeSchema:
    """A loop whose graph tool serves the real node schema.

    The resolve tool's ``nodes`` field is ``run_subagent_dag``'s own node shape,
    so a double that omits it leaves ``items`` a bare object and hides the very
    duplication these tests are about.
    """

    tools = _NodeSchemaToolTable()


def render_without_dedupe(tool: Any) -> str:
    import json

    return json.dumps(tool.to_schema())


class TestTheGraphToolsResultTextCarriesTheControlDefinitions:
    """The other two advertisement sites. The background-start text named the
    three tools, and the foreground report named ``resolve_dag_node`` and no
    arguments at all -- so the bound lane told the model strictly less than the
    background lane did.
    """

    def _tool(self, advert: Any) -> Any:
        return raven_agent_subagent.SubAgentDagTool(
            workspace=Path(tempfile.mkdtemp()),
            registry=None,
            control_advert=advert,
        )

    def test_the_named_definitions_are_appended(self) -> None:
        tool = self._tool(lambda name: f"<{name}-schema>")

        text = tool._advert_for("dag_status", "cancel_dag")

        assert "dag_status: <dag_status-schema>" in text
        assert "cancel_dag: <cancel_dag-schema>" in text
        assert "resolve_dag_node" not in text, "only what the caller asked for"

    def test_no_renderer_wired_appends_nothing(self) -> None:
        # A tool built without a loop, and every test that does the same: the
        # caller concatenates unconditionally, so this has to be a bare string.
        assert self._tool(None)._advert_for("dag_status") == ""

    def test_a_renderer_that_raises_costs_the_hint_and_nothing_else(self) -> None:
        def _boom(_name: str) -> str:
            raise RuntimeError("registry is gone")

        assert self._tool(_boom)._advert_for("dag_status", "cancel_dag") == ""

    def test_a_name_with_no_definition_is_skipped_not_rendered_empty(self) -> None:
        tool = self._tool(lambda name: None if name == "cancel_dag" else "<s>")

        text = tool._advert_for("dag_status", "cancel_dag")

        assert "dag_status: <s>" in text
        assert "cancel_dag" not in text


def test_the_foreground_report_advertises_resolve_once_not_twice() -> None:
    """The report `render_event` fences already carries the schema.

    `_exception_report` appends it, and a Report exists only past the
    awaiting-decision gate -- which implies `deliverable` and `remaining > 0`,
    so neither of that function's early returns was taken. Appending it here
    too emitted the definition twice, once as fenced node evidence and once
    trusted.
    """
    from raven.agent.subagent.dag_adjudication import Report
    from raven.agent.subagent.dag_control_advert import render
    from raven.agent.subagent.dag_runner import _exception_report
    from raven.agent.subagent.dag_verdict import Verdict

    tool = ResolveDagNodeTool(loop=_LoopWithNodeSchema())
    advert = raven_agent_subagent.SubAgentDagTool(
        workspace=Path(tempfile.mkdtemp()),
        registry=None,
        control_advert=lambda name: render(tool.to_schema()) if name == "resolve_dag_node" else None,
    )
    spec = parse_dag_spec(
        {
            "task_summary": "t",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "s", "prompt_template": "p"}],
        }
    )
    report = _exception_report(
        run_id="r1",
        node=spec.nodes[0],
        verdict=Verdict(accomplished=False, category="other", what_is_missing="a token"),
        attempt=1,
        remaining=2,
        blocked=[],
        timeout_s=600.0,
        control_advert=lambda name: render(tool.to_schema()),
    )
    rendered = advert.render_event("r1", Report(node_id="a", text=report)).model_text

    assert rendered.count('"name": "resolve_dag_node"') == 1, "one advertisement, not two"
    assert "resolve_dag_node" in rendered


def test_the_node_shape_pointer_travels_in_a_key_the_model_is_shown() -> None:
    """`$comment` would read as the right key for a note and is the wrong one.

    JSON Schema defines it as a note to developers that implementations MUST
    NOT present, so a provider that normalizes the schema is entitled to drop
    it -- leaving `items` an empty object and the node shape nowhere, which is
    the failure the reference exists to avoid. The module's docstring gives
    that reason; this holds it, because flipping the key left the suite green.
    """
    from raven.agent.subagent.dag_control_advert import _NODE_SHAPE_REF, render

    assert "description" in _NODE_SHAPE_REF, "the pointer has to be presented to the model"
    assert "$comment" not in _NODE_SHAPE_REF, "$comment is a note implementations must not present"

    tool = ResolveDagNodeTool(loop=_LoopWithNodeSchema())
    text = render(tool.to_schema())
    assert text is not None
    assert "$comment" not in text, "and it must not reach the rendered advertisement either"


# --- reassigning a suspended node: admission, confirmation and transport ------


_REASSIGN_SESSION = "web:reassign"


class _FakeAgent:
    """One roster agent's backend. Records dispatches and never touches a transport."""

    kind = "cli"

    def __init__(self, reply: str = "done") -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []
        self.hold: asyncio.Event | None = None
        self.hold_ids: set[str] = set()

    async def run(self, task: str, **kwargs: Any) -> str:
        self.calls.append({"task": task, "kwargs": kwargs, "charter": outbound_charter()})
        if self.hold is not None and (not self.hold_ids or kwargs.get("task_id") in self.hold_ids):
            await self.hold.wait()
        return self.reply


class _Charge:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, conversation: Any) -> str | None:
        self.calls.append(conversation)
        return None


class _Ask:
    """An ask channel that answers at once, and remembers every question."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, conversation: str, question: str) -> bool:
        self.calls.append((conversation, question))
        return self.answer


def _short_once(node_ids: set[str]):
    """A judge that rejects each named node's first attempt and passes later ones."""
    remaining = set(node_ids)

    async def _judge(*, node: Any, **_kwargs: Any) -> Verdict:
        if node.id in remaining:
            remaining.discard(node.id)
            return Verdict(accomplished=False, category="tool_failure", what_is_missing="a parser")
        return Verdict(accomplished=True)

    return _judge


def _local(name: str, *, stateful: bool = False) -> ThirdPartyCliSubagentConfig:
    if stateful:
        return ThirdPartyCliSubagentConfig(
            name=name, command="true", resume_command="true {agent_id}", id_source="derived"
        )
    return ThirdPartyCliSubagentConfig(name=name, command="true")


def _node_spec(node_id: str = "a", *, subagent: str = "alpha", **extra: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "subagent": subagent,
        "node_summary": "a step",
        "prompt_template": "do it",
        **extra,
    }


async def _noop_announce(*_args: Any, **_kwargs: Any) -> None:
    """A delivery route for a background run, so a short node can suspend and be asked about."""
    return None


def _real_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agents: list[Any] | None = None,
    backends: dict[str, _FakeAgent] | None = None,
    judge: Any = None,
    confirm: bool = True,
    **kwargs: Any,
) -> tuple[SubAgentDagTool, dict[str, _FakeAgent], _Ask, _Charge]:
    """A real graph tool whose agents are recording fakes, never transports."""
    ask = kwargs.pop("ask", _Ask())
    charge = kwargs.pop("charge", _Charge())
    kwargs.setdefault("announce_exception", _noop_announce)
    if agents is None:
        agents = [_local("alpha"), _local("beta")]
    tool = SubAgentDagTool(workspace=tmp_path, agents=agents, ask=ask, charge=charge, **kwargs)
    tool.set_context("web", "default", _REASSIGN_SESSION)
    fakes = backends if backends is not None else {"alpha": _FakeAgent("partial"), "beta": _FakeAgent("fixed")}
    monkeypatch.setattr(tool, "_resolve_node", lambda node: fakes[node.subagent])
    script = judge if judge is not None else _short_once({"a"})
    tool._judge_node = lambda: script
    return tool, fakes, ask, charge


async def _suspended(
    tool: SubAgentDagTool,
    *,
    nodes: list[dict[str, Any]] | None = None,
    confirm: bool = True,
    background: bool = False,
    node_id: str = "a",
) -> str:
    """Run the graph until ``node_id`` waits for a decision; return its run id."""
    from tests.test_subagent_dag_runner import _wait_until

    await tool.execute(
        task_summary="graph under test",
        nodes=nodes or [_node_spec()],
        background=background,
        confirm=confirm,
    )
    await _wait_until(lambda: bool(tool._desks))
    run_id = next(iter(tool._desks))
    await _wait_until(lambda: run_id in tool._desks and tool._desks[run_id].is_open(node_id))
    return run_id


@dataclass
class _Scene:
    tool: SubAgentDagTool
    run_id: str
    agent: str | None
    message: str | None
    expected: str
    backends: dict[str, _FakeAgent]
    ask: _Ask
    charge: _Charge
    session_key: str = _REASSIGN_SESSION
    scope: Any = None
    waits: bool = True

    @property
    def desk(self) -> Any:
        return self.tool._desks[self.run_id]

    @property
    def state(self) -> Any:
        return self.tool._run_contexts[self.run_id].state


async def _refusal_scene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> _Scene:
    """One refused-reassign arrangement. Raises AssertionError for an unknown case."""
    session = _REASSIGN_SESSION

    if case in {"missing_agent", "blank_agent", "missing_message", "blank_message", "unknown_target"}:
        tool, fakes, ask, charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool)
        agent = {"missing_agent": None, "blank_agent": "   ", "unknown_target": "ghost"}.get(case, "beta")
        message = {"missing_message": None, "blank_message": "  "}.get(case, "use the parser")
        expected = {
            "missing_agent": "needs an `agent`",
            "blank_agent": "needs an `agent`",
            "missing_message": "needs a message",
            "blank_message": "needs a message",
            "unknown_target": "no sub-agent by that name",
        }[case]
        return _Scene(tool, run_id, agent, message, expected, fakes, ask, charge)

    if case == "worker_label":
        tool, fakes, ask, charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool)
        table = DelegateTable(workers={"helper": Worker("helper", "alpha", "helps", "")})
        return _Scene(
            tool,
            run_id,
            "helper",
            "use the parser",
            "worker label",
            fakes,
            ask,
            charge,
            scope=delegate_scope(table),
        )

    if case == "disabled_target":
        tool, fakes, ask, charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha"), ThirdPartyCliSubagentConfig(name="beta", command="true", enabled=False)],
        )
        run_id = await _suspended(tool)
        return _Scene(tool, run_id, "beta", "use the parser", "turned off", fakes, ask, charge)

    if case == "other_conversation":
        tool, fakes, ask, charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool)
        return _Scene(
            tool, run_id, "beta", "use the parser", "No DAG run", fakes, ask, charge, session_key="web:elsewhere"
        )

    if case == "non_waiting":
        tool, fakes, ask, charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool)
        tool._desks[run_id].close("a")
        return _Scene(tool, run_id, "beta", "use the parser", "no longer waiting", fakes, ask, charge, waits=False)

    if case == "stint":
        tool, fakes, ask, charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool)
        tool._stints[run_id] = SimpleNamespace(stint_id="s1", round_index=1)
        return _Scene(tool, run_id, "beta", "use the parser", "stint", fakes, ask, charge)

    if case == "delegation_paused":
        paused = {"on": False}
        tool, fakes, ask, charge = _real_tool(tmp_path, monkeypatch, is_paused=lambda: paused["on"])
        run_id = await _suspended(tool)
        paused["on"] = True
        return _Scene(tool, run_id, "beta", "use the parser", "delegation is paused", fakes, ask, charge)

    if case == "same_agent_alias":
        tool, fakes, ask, charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[],
            backends={"Raven-General": _FakeAgent("partial")},
            judge=_short_once({"a"}),
        )
        run_id = await _suspended(tool, nodes=[_node_spec(subagent="Raven-General")])
        return _Scene(tool, run_id, "Raven", "use the parser", "choose continue", fakes, ask, charge)

    if case == "path_gate":
        (tmp_path / "notes.md").write_text("the file the node reads", encoding="utf-8")
        tool, fakes, ask, charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[
                _local("alpha"),
                ThirdPartyCliSubagentConfig(name="remote", command="true", machine="worker-a"),
            ],
            backends={"alpha": _FakeAgent("partial"), "remote": _FakeAgent("remote")},
        )
        run_id = await _suspended(tool, nodes=[_node_spec(prompt_template="inspect {{ ref_path:notes.md }}")])
        return _Scene(tool, run_id, "remote", "use the parser", "[no-local-files]", fakes, ask, charge)

    if case == "shared_group_stateless_target":
        tool, fakes, ask, charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), _local("gamma")],
            backends={"alpha": _FakeAgent("partial"), "gamma": _FakeAgent("gamma")},
            judge=_short_once({"a"}),
        )
        nodes = [
            _node_spec("a", instance="shared"),
            _node_spec("b", instance="shared"),
        ]
        run_id = await _suspended(tool, nodes=nodes)
        from tests.test_subagent_dag_runner import _wait_until

        state = tool._run_contexts[run_id].state
        await _wait_until(lambda: len(state.attempt_agents.get("b", [])) == 1)
        return _Scene(tool, run_id, "gamma", "use the parser", "stateful", fakes, ask, charge)

    if case == "occupied_instance":
        from raven.agent.subagent import instances as instances_mod

        registry = instances_mod.InstanceRegistry(path=tmp_path / "instances.json")
        monkeypatch.setattr(instances_mod, "_registry", registry)
        tool, fakes, ask, charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), _local("beta", stateful=True)],
            backends={"alpha": _FakeAgent("partial"), "beta": _FakeAgent("fixed")},
        )
        run_id = await _suspended(tool, nodes=[_node_spec(instance="shared")])
        await registry.commit(session, "beta", "shared", "beta-session-1")
        return _Scene(tool, run_id, "beta", "use the parser", "conversation", fakes, ask, charge)

    raise AssertionError(f"unknown refusal case {case!r}")


_REFUSAL_CASES = [
    "missing_agent",
    "blank_agent",
    "missing_message",
    "blank_message",
    "unknown_target",
    "worker_label",
    "disabled_target",
    "other_conversation",
    "non_waiting",
    "stint",
    "delegation_paused",
    "same_agent_alias",
    "path_gate",
    "shared_group_stateless_target",
    "occupied_instance",
]


@pytest.mark.parametrize("case", _REFUSAL_CASES)
async def test_a_refused_reassign_names_its_condition_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from contextlib import nullcontext

    from tests.test_subagent_dag_runner import draining_dag_runs

    async with draining_dag_runs():
        scene = await _refusal_scene(tmp_path, monkeypatch, case)
        desk = scene.desk
        state = scene.state
        source_backend = scene.backends[state.effective_nodes["a"].subagent]
        follow_up = Path(scene.tool._nodes_root(scene.session_key)) / "a.follow_ups.json"
        before = {
            "source": len(source_backend.calls),
            "ask": len(scene.ask.calls),
            "charge": len(scene.charge.calls),
            "attempts": {k: list(v) for k, v in state.attempt_agents.items()},
            "revision": state.revision,
        }

        with scene.scope if scene.scope is not None else nullcontext():
            out = await scene.tool.prepare_reassign(scene.run_id, "a", scene.agent, scene.message, scene.session_key)

        assert isinstance(out, str), f"{case}: an unmet check must refuse, got {out!r}"
        assert scene.expected in out, f"{case}: {out!r}"
        assert "no decision was recorded" in out.lower() or "no decision was recorded" in out, case
        assert desk.is_open("a") is scene.waits, f"{case}: the node's waiting state must not change"
        assert desk.peek("a") is None, f"{case}: no answer may be recorded"
        assert not follow_up.exists(), f"{case}: no follow-up may be written"
        assert len(source_backend.calls) == before["source"], f"{case}: no backend may run"
        assert len(scene.ask.calls) == before["ask"], f"{case}: a failed check must not ask the user"
        assert len(scene.charge.calls) == before["charge"], f"{case}: nothing may be charged"
        assert state.pending_moves == {} and state.pending_handovers == {}, f"{case}: nothing seeded"
        assert state.reassigned == set(), case
        assert state.revision == before["revision"], case
        assert {k: list(v) for k, v in state.attempt_agents.items()} == before["attempts"], case
        scene.tool.request_cancel(scene.run_id)


async def test_an_accepted_reassign_prepares_the_target_and_waits_for_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prepared move records nothing: the desk stays open until the hand-off."""
    from tests.test_subagent_dag_runner import draining_dag_runs

    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool)

        plan = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(plan, ReassignPlan), plan
        assert plan.node_id == "a"
        assert plan.source_agent == "alpha"
        assert plan.source_instance is None
        assert plan.target_agent == "beta"
        assert plan.target_stateful is False
        assert plan.target_instance is None
        assert plan.waiter is tool._desks[run_id].waiter("a")
        assert [node.id for node in plan.nodes] == ["a"]
        assert set(plan.backends) == {"a"}
        assert tool._desks[run_id].peek("a") is None, "preparing is not deciding"
        assert fakes["beta"].calls == [], "preparing a backend must not run one"
        tool.request_cancel(run_id)


async def test_a_stateful_target_keeps_a_graph_named_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_subagent_dag_runner import draining_dag_runs

    async with draining_dag_runs():
        tool, _fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), _local("beta", stateful=True)],
            backends={"alpha": _FakeAgent("partial"), "beta": _FakeAgent("fixed")},
        )
        run_id = await _suspended(tool, nodes=[_node_spec(instance="shared")])

        plan = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(plan, ReassignPlan), plan
        assert plan.target_stateful is True
        assert plan.target_instance == "shared"
        assert plan.instance_auto is False
        tool.request_cancel(run_id)


async def test_a_stateful_target_mints_a_handle_without_publishing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import instances as instances_mod
    from tests.test_subagent_dag_runner import draining_dag_runs

    registry = instances_mod.InstanceRegistry(path=tmp_path / "instances.json")
    monkeypatch.setattr(instances_mod, "_registry", registry)
    async with draining_dag_runs():
        tool, _fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha"), _local("beta", stateful=True)],
            backends={"alpha": _FakeAgent("partial"), "beta": _FakeAgent("fixed")},
        )
        run_id = await _suspended(tool)

        plan = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(plan, ReassignPlan), plan
        assert plan.instance_auto is True
        assert plan.target_instance, "a stateful target gets a fresh handle"
        assert await registry.lookup(_REASSIGN_SESSION, "beta", plan.target_instance) is None, (
            "a proposed handle must not be bound before the move is accepted"
        )
        tool.request_cancel(run_id)


async def test_a_singleton_named_instance_may_move_to_a_stateless_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_subagent_dag_runner import draining_dag_runs

    async with draining_dag_runs():
        tool, _fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), _local("beta")],
            backends={"alpha": _FakeAgent("partial"), "beta": _FakeAgent("fixed")},
        )
        run_id = await _suspended(tool, nodes=[_node_spec(instance="shared")])

        plan = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(plan, ReassignPlan), plan
        assert plan.target_stateful is False
        assert plan.target_instance is None, "a stateless target takes no handle"
        assert plan.instance_auto is False
        tool.request_cancel(run_id)


async def test_a_path_using_node_may_move_to_an_agent_that_reads_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_subagent_dag_runner import draining_dag_runs

    (tmp_path / "notes.md").write_text("the file the node reads", encoding="utf-8")
    async with draining_dag_runs():
        tool, _fakes, _ask, _charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool, nodes=[_node_spec(prompt_template="inspect {{ ref_path:notes.md }}")])

        plan = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(plan, ReassignPlan), plan
        tool.request_cancel(run_id)


async def test_a_shared_group_moves_together_to_a_stateful_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent.dag_store import DagRunStore
    from tests.test_subagent_dag_runner import _wait_until, draining_dag_runs

    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), _local("beta", stateful=True)],
            backends={"alpha": _FakeAgent("partial"), "beta": _FakeAgent("fixed")},
            judge=_short_once({"a", "b"}),
        )
        run_id = await _suspended(tool, nodes=[_node_spec("a", instance="shared"), _node_spec("b", instance="shared")])
        await _wait_until(lambda: tool._desks[run_id].is_open("b"))

        plan = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(plan, ReassignPlan), plan
        assert [node.id for node in plan.nodes] == ["a", "b"], "every node that may move is approved"
        assert {node.instance for node in plan.nodes} == {"shared"}
        assert plan.target_instance == "shared"
        assert set(plan.backends) == {"a", "b"}
        store = DagRunStore(
            tool._backend,
            tool._run_root(_REASSIGN_SESSION),
            run_id,
            nodes_root=tool._nodes_root(_REASSIGN_SESSION),
            registry_root=tool._history_root(_REASSIGN_SESSION) or tool._run_root(_REASSIGN_SESSION),
        )
        assert await store.read_follow_ups("a") == []
        tool.request_cancel(run_id)


async def test_a_machine_target_is_noticed_without_touching_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_subagent_dag_runner import draining_dag_runs

    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha"), ThirdPartyCliSubagentConfig(name="worker", command="true", machine="worker-a")],
            backends={"alpha": _FakeAgent("partial"), "worker": _FakeAgent("remote")},
        )
        run_id = await _suspended(tool)

        plan = await tool.prepare_reassign(run_id, "a", "worker", "use the parser", _REASSIGN_SESSION)

        assert isinstance(plan, ReassignPlan), plan
        joined = " ".join(plan.notices)
        assert "worker-a" in joined and "files" in joined, joined
        assert fakes["worker"].calls == [], "a notice must not require contacting the machine"
        tool.request_cancel(run_id)


class _PausedAsk:
    """An ask channel the test releases by hand, so mutations can land mid-question."""

    def __init__(self) -> None:
        self.asked = asyncio.Event()
        self.release = asyncio.Event()
        self.answer = True
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, conversation: str, question: str) -> bool:
        self.calls.append((conversation, question))
        self.asked.set()
        await self.release.wait()
        return self.answer


async def _prepare_then_commit(tool: SubAgentDagTool, run_id: str, agent: str, message: str) -> Any:
    """The pair the control tool performs, so a race can be observed at either half."""
    prepared = await tool.prepare_reassign(run_id, "a", agent, message, _REASSIGN_SESSION)
    if isinstance(prepared, str):
        return prepared
    return prepared if tool.commit_reassign(run_id, prepared, message) else None


async def test_the_confirmation_lists_every_possible_mover_and_waits_for_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_subagent_dag_runner import _wait_until, draining_dag_runs

    ask = _PausedAsk()
    alpha = _FakeAgent("partial")
    alpha.hold = asyncio.Event()
    alpha.hold_ids = {"b"}
    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), _local("beta", stateful=True)],
            backends={"alpha": alpha, "beta": _FakeAgent("fixed")},
            judge=_short_once({"a", "b"}),
        )
        run_id = await _suspended(tool, nodes=[_node_spec("a", instance="shared"), _node_spec("b", instance="shared")])
        await _wait_until(lambda: any(call["kwargs"].get("task_id") == "b" for call in alpha.calls))
        tool._ask = ask

        pending = asyncio.create_task(tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION))
        await asyncio.wait_for(ask.asked.wait(), timeout=5)
        question = ask.calls[-1][1]

        assert "- a: alpha -> beta" in question
        assert "- b: alpha -> beta" in question, "a sibling that is running now may still move"
        assert "may move" in question, "a listed sibling is a possible mover, not a promised one"
        assert "whose attempt has not settled yet finishes that attempt first" in question
        assert tool._desks[run_id].peek("a") is None, "the node waits until the user answers"
        ask.release.set()
        plan = await asyncio.wait_for(pending, timeout=5)

        assert isinstance(plan, ReassignPlan), plan
        assert [node.id for node in plan.nodes] == ["a", "b"]
        assert plan.target_instance == "shared"
        alpha.hold.set()
        tool.request_cancel(run_id)


async def test_a_rejected_confirmation_uses_the_approved_text_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_subagent_dag_runner import draining_dag_runs

    ask = _PausedAsk()
    ask.answer = False
    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool)
        tool._ask = ask
        state = tool._run_contexts[run_id].state
        follow_up = Path(tool._nodes_root(_REASSIGN_SESSION)) / "a.follow_ups.json"

        pending = asyncio.create_task(tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION))
        await asyncio.wait_for(ask.asked.wait(), timeout=5)
        ask.release.set()
        out = await asyncio.wait_for(pending, timeout=5)

        assert out == (
            "The user did not approve this reassign, so no decision was recorded and node 'a' is still waiting."
        )
        assert tool._desks[run_id].is_open("a")
        assert tool._desks[run_id].peek("a") is None
        assert state.pending_moves == {} and state.pending_handovers == {}
        assert state.reassigned == set()
        assert not follow_up.exists()

        # A later attempt asks again: the earlier "no" is not an approval to reuse.
        ask.asked.clear()
        ask.release = asyncio.Event()
        second = asyncio.create_task(tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION))
        await asyncio.wait_for(ask.asked.wait(), timeout=5)
        assert len(ask.calls) == 2
        ask.answer = True
        ask.release.set()
        plan = await asyncio.wait_for(second, timeout=5)
        assert isinstance(plan, ReassignPlan), plan
        assert fakes["beta"].calls == [], "planning still runs no backend"
        tool.request_cancel(run_id)


@pytest.mark.parametrize(
    "mutation",
    ["cancel_the_run", "answer_the_node", "hot_disable_the_target", "change_the_revision", "occupy_the_instance"],
)
async def test_a_change_while_the_user_is_asked_refuses_without_seeding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from raven.agent.subagent import instances as instances_mod
    from raven.agent.subagent.dag_adjudication import CONTINUE
    from tests.test_subagent_dag_runner import draining_dag_runs

    registry = instances_mod.InstanceRegistry(path=tmp_path / "instances.json")
    monkeypatch.setattr(instances_mod, "_registry", registry)
    ask = _PausedAsk()
    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), _local("beta", stateful=True)],
            backends={"alpha": _FakeAgent("partial"), "beta": _FakeAgent("fixed")},
        )
        run_id = await _suspended(tool, nodes=[_node_spec(instance="shared")])
        tool._ask = ask
        state = tool._run_contexts[run_id].state
        desk = tool._desks[run_id]

        pending = asyncio.create_task(_prepare_then_commit(tool, run_id, "beta", "use the parser"))
        await asyncio.wait_for(ask.asked.wait(), timeout=5)
        if mutation == "cancel_the_run":
            tool.request_cancel(run_id)
        elif mutation == "answer_the_node":
            assert desk.resolve("a", CONTINUE, "try again here")
        elif mutation == "hot_disable_the_target":
            tool._registry.apply(
                [
                    _local("alpha", stateful=True),
                    ThirdPartyCliSubagentConfig(name="beta", command="true", enabled=False),
                ]
            )
        elif mutation == "change_the_revision":
            state.revision += 1
        elif mutation == "occupy_the_instance":
            await registry.commit(_REASSIGN_SESSION, "beta", "shared", "beta-session-1")
        ask.release.set()
        out = await asyncio.wait_for(pending, timeout=5)

        assert not isinstance(out, ReassignPlan), f"{mutation}: must refuse, got {out!r}"
        if isinstance(out, str):
            assert "no decision was recorded" in out.lower(), f"{mutation}: {out!r}"
        peeked = desk.peek("a")
        if mutation != "answer_the_node":
            assert peeked is None, f"{mutation}: the move must not land an answer"
        else:
            assert peeked is not None and peeked.decision != REASSIGN, f"{mutation}: the continue stands"
        assert state.pending_moves == {}, f"{mutation}: nothing may be staged"
        assert state.pending_handovers == {}, f"{mutation}: nothing may be seeded"
        assert state.reassigned == set(), f"{mutation}: nothing may install"
        assert not (Path(tool._nodes_root(_REASSIGN_SESSION)) / "a.follow_ups.json").exists(), (
            f"{mutation}: a proposal that never landed records no follow-up"
        )
        tool.request_cancel(run_id)


async def test_a_confirm_run_with_no_way_to_ask_refuses_the_reassign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The permissive no-ask default authorizes a graph; it must not authorize a move."""
    from tests.test_subagent_dag_runner import draining_dag_runs

    async with draining_dag_runs():
        tool, _fakes, _ask, _charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool)
        tool._ask = None

        out = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(out, str), out
        assert "no way to ask the user" in out
        assert "no decision was recorded" in out.lower()
        assert tool._desks[run_id].peek("a") is None
        assert tool._run_contexts[run_id].state.pending_moves == {}
        tool.request_cancel(run_id)


def _transport_plan(node_ids: tuple[str, ...] = ("a",), *, instance: str | None = None) -> ReassignPlan:
    nodes = tuple(
        parse_dag_spec(
            {
                "task_summary": "move",
                "nodes": [
                    {"id": nid, "subagent": "alpha", "node_summary": "a step", "prompt_template": "do it"}
                    for nid in node_ids
                ],
            }
        ).nodes
    )
    return ReassignPlan(
        node_id=node_ids[0],
        source_agent="alpha",
        source_instance=instance,
        target_agent="beta",
        target_stateful=instance is not None,
        target_instance=instance,
        instance_auto=False,
        revision=1,
        waiter=asyncio.Event(),
        nodes=nodes,
        backends={},
        notices=(),
    )


async def test_a_reassign_goes_through_the_owning_tools_prepare_and_commit() -> None:
    """The same fan-out a replan takes: the instance holding the run does the work."""
    loop = _LoopWithSecondOwner()
    loop._owner.reassign_plan = _transport_plan()
    loop._owner._foreground = False
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="reassign", agent="beta", message="use it")

    assert loop._owner.calls == ["prepare_reassign", "commit_reassign"]
    assert loop._owner.reassign_calls == [("r1", "a", "beta", "use it", None)]
    assert loop._registered.calls == [], "the registered instance holds no run to move"
    assert loop._owner.commit_calls and loop._owner.commit_calls[0][0] == "r1"
    assert out == "Node 'a' of run r1 will run again on beta with your message."


async def test_a_prepare_refusal_is_returned_verbatim_and_never_commits() -> None:
    loop = _LoopWithSecondOwner()
    loop._owner.reassign_refusal = "Error: cannot reassign node 'a' to 'ghost'."
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="reassign", agent="ghost", message="use it")

    assert out == "Error: cannot reassign node 'a' to 'ghost'."
    assert "commit_reassign" not in loop._owner.calls
    assert loop._registered.calls == []


async def test_a_commit_refusal_reports_a_node_no_longer_waiting() -> None:
    loop = _LoopWithSecondOwner()
    loop._owner.reassign_plan = _transport_plan()
    loop._owner.commit_ok = False
    loop._owner._awaiting = False
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="reassign", agent="beta", message="use it")

    assert "no longer waiting for a decision" in out
    assert "reassign was not applied" in out
    assert loop._owner.calls == ["prepare_reassign", "commit_reassign"]


async def test_a_stale_commit_refusal_says_to_request_it_again() -> None:
    """A state change refuses the proposal; the node itself is still waiting."""
    loop = _LoopWithSecondOwner()
    loop._owner.reassign_plan = _transport_plan()
    loop._owner.commit_ok = False
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="reassign", agent="beta", message="use it")

    assert "state changed" in out
    assert "request" in out.lower()
    assert "no longer waiting" not in out, "the node's question is still open"


async def test_a_target_that_already_tried_the_node_is_still_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure may have been chance, so a previous agent is not excluded."""
    from tests.test_subagent_dag_runner import _wait_until, draining_dag_runs

    shortfalls = {"a": 2}

    async def _judge(*, node: Any, **_kwargs: Any) -> Verdict:
        if shortfalls.get(node.id, 0) > 0:
            shortfalls[node.id] -= 1
            return Verdict(accomplished=False, category="tool_failure", what_is_missing="a parser")
        return Verdict(accomplished=True)

    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha"), _local("beta")],
            backends={"alpha": _FakeAgent("partial"), "beta": _FakeAgent("closer")},
            judge=_judge,
        )
        run_id = await _suspended(tool, confirm=False)
        state = tool._run_contexts[run_id].state
        desk = tool._desks[run_id]

        first = await _prepare_then_commit(tool, run_id, "beta", "use the parser")
        assert isinstance(first, ReassignPlan), first
        await _wait_until(lambda: len(state.attempt_agents.get("a", [])) == 2)
        await _wait_until(lambda: desk.is_open("a"))
        assert state.effective_nodes["a"].subagent == "beta"
        follow_up = Path(tool._nodes_root(_REASSIGN_SESSION)) / "a.follow_ups.json"
        assert json.loads(follow_up.read_text(encoding="utf-8")) == ["use the parser"], (
            "the raw reassign message lands once, as the attempt's follow-up"
        )

        back = await tool.prepare_reassign(run_id, "a", "alpha", "try the parser instead", _REASSIGN_SESSION)

        assert isinstance(back, ReassignPlan), back
        assert back.target_agent == "alpha"
        assert len(fakes["alpha"].calls) == 1, "planning still runs no backend"
        tool.request_cancel(run_id)


def _judge_provider(verdict_args: dict[str, Any]) -> Any:
    """A provider that answers the judge's tool call and records every request."""

    class _Provider:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat_with_retry(self, **kwargs: Any) -> SimpleNamespace:
            self.calls.append(kwargs)
            call = SimpleNamespace(arguments=json.dumps(verdict_args))
            return SimpleNamespace(tool_calls=[call], finish_reason="stop", content="")

    return _Provider()


async def test_the_production_judge_reads_the_original_prompt_and_the_raw_reassign_message(
    tmp_path: Path,
) -> None:
    """The judge weighs the task as first given plus the messages sent since.

    A reassigned attempt is dispatched with a handover wrapper. The judge must read
    the recorded original task and follow-up, never the wrapper and never its
    earlier turns replayed as a new task.
    """
    from raven.agent.subagent.dag_store import DagRunStore
    from raven.agent.subagent.prompt_backend import LocalFileBackend

    provider = _judge_provider({"outcome": "accomplished"})
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[_local("alpha"), _local("beta")],
        provider_for=lambda: provider,
        ask=_Ask(),
    )
    store = DagRunStore(
        LocalFileBackend(),
        str(tmp_path / "runs"),
        "r1",
        nodes_root=str(tmp_path / "nodes"),
        registry_root=str(tmp_path / "subagents"),
    )
    node = parse_dag_spec({"task_summary": "judge", "nodes": [_node_spec()]}).nodes[0]
    wrapper = (
        '<handover from="alpha" to="beta" turns="1">\n'
        "User: original task\n"
        "Assistant: partial\n"
        "</handover>\n\nuse the parser"
    )
    await store.write_text(store.prompt_path(node.id), "original task")
    await store.write_text(store.attempt_prompt_path(node.id, 2), wrapper)
    await store.record_follow_up(node.id, "use the parser")

    judge = tool._judge_node()
    verdict = await judge(node=node, store=store, output="the answer", error="", crashed=False)

    assert verdict.accomplished is True
    system, user = (message["content"] for message in provider.calls[0]["messages"])
    fenced = re.findall(r"\[BEGIN UNTRUSTED \S+ #\w+ [^\n]*\n(.*?)\n\[END UNTRUSTED", user, flags=re.DOTALL)
    assert fenced == ["original task", "use the parser", "the answer"]
    assert "<handover" not in user and "handover" not in user, "the wrapper is transport, not task material"
    assert "the later one stands" in system, "the reassign message is weighed as part of the task"


async def test_a_background_reassign_names_the_siblings_that_may_move_with_it() -> None:
    loop = _LoopWithRun()
    loop._dag_tool.reassign_plan = _transport_plan(("a", "b"), instance="shared")
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="reassign", agent="beta", message="use it")

    assert out.startswith("Node 'a' of run r1 will run again on beta with your message.")
    assert "may move with it" in out, "the tool cannot know which siblings the runner will relocate"
    assert "'b'" in out and "shared" in out, "a group move names the sibling and their instance"
    assert loop.resolved is None, "a reassign must not reach the replan/continue hand-off"


async def test_a_foreground_reassign_waits_on_the_owning_tools_outbox() -> None:
    loop = _LoopWithRun(foreground=True, events=["next report"])
    loop._dag_tool.reassign_plan = _transport_plan()
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="reassign", agent="beta", message="use it")

    assert loop._dag_tool.awaited == ["r1"]
    assert out == "RENDERED r1 next report"


async def test_cancelling_a_waiting_reassign_aborts_the_run() -> None:
    loop = _LoopWithRun(foreground=True)
    loop._dag_tool.reassign_plan = _transport_plan()
    tool = _resolve_tool(loop)

    call = asyncio.create_task(
        tool.execute(run_id="r1", node_id="a", decision="reassign", agent="beta", message="use it")
    )
    await asyncio.sleep(0)
    assert loop._dag_tool.awaited == ["r1"]

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert loop._dag_tool.aborted == ["r1"]


def test_the_reassign_decision_and_agent_are_on_the_model_facing_schema() -> None:
    """The exception report copies this schema, so the decision and its target are
    named here rather than in a hand-written retelling beside it."""
    params = _resolve_tool().parameters

    assert params["properties"]["decision"]["enum"] == ["continue", "abandon", "replan", "reassign"]
    assert params["properties"]["agent"]["type"] == "string"


class _GrantingAgent(_FakeAgent):
    """A backend that can take an injected MCP grant, and records what it got."""

    def __init__(self, reply: str = "fixed") -> None:
        super().__init__(reply)
        self.grants: list[tuple[list[str], Any]] = []

    def resolve_mcp_grant(self, mcps: list[str] | None = None) -> Any:
        grant = SimpleNamespace(note_text=lambda: "")
        self.grants.append((list(mcps or []), grant))
        return grant


async def _settled_result(tool: SubAgentDagTool, run_id: str) -> Any:
    from tests.test_subagent_dag_runner import _wait_until

    await _wait_until(lambda: "a" not in tool._desks[run_id].open_nodes(), timeout=5)
    return None


async def _reassign_and_settle(tool: SubAgentDagTool, run_id: str, agent: str = "beta", node_id: str = "a") -> Any:
    from tests.test_subagent_dag_runner import _wait_until

    prepared = await tool.prepare_reassign(run_id, node_id, agent, "use the parser", _REASSIGN_SESSION)
    assert isinstance(prepared, ReassignPlan), prepared
    assert tool.commit_reassign(run_id, prepared, "use the parser")
    await _wait_until(lambda: not tool._desks or run_id not in tool._run_contexts, timeout=10)
    return prepared


async def test_a_moved_node_dispatches_through_the_correct_grant_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The target's row decides the grant; the node's own list is what it resolves."""
    from tests.test_subagent_dag_runner import draining_dag_runs

    beta = _GrantingAgent("fixed")
    async with draining_dag_runs():
        tool, _fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha"), ThirdPartyCliSubagentConfig(name="beta", command="cat {mcp_file}")],
            backends={"alpha": _FakeAgent("partial"), "beta": beta},
        )
        run_id = await _suspended(tool, nodes=[_node_spec(mcps=["db"])])

        await _reassign_and_settle(tool, run_id)

        assert beta.calls, "the destination attempt ran"
        assert beta.grants and beta.grants[-1][0] == ["db"], "the grant resolved from the node's list"
        assert beta.calls[0]["kwargs"]["mcp_grant"] is beta.grants[-1][1], (
            "the dispatched attempt uses the grant its own prepared backend resolved"
        )
        tool.request_cancel(run_id)


async def test_a_target_that_cannot_inject_mcp_servers_gets_the_list_dropped_and_a_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_subagent_dag_runner import draining_dag_runs

    gamma = _FakeAgent("fixed")
    async with draining_dag_runs():
        tool, _fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha"), _local("gamma")],
            backends={"alpha": _FakeAgent("partial"), "gamma": gamma},
        )
        run_id = await _suspended(tool, nodes=[_node_spec(mcps=["db"])])

        prepared = await _reassign_and_settle(tool, run_id, "gamma")

        assert gamma.calls, "the destination attempt ran"
        assert "mcps" not in gamma.calls[0]["kwargs"], "a target that cannot inject must not receive the list"
        assert any("cannot take injected mcp servers" in note for note in prepared.notices), prepared.notices
        tool.request_cancel(run_id)


async def test_skills_are_folded_once_from_the_authored_prompt_for_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source agent's folded copy is never the target's starting point."""
    from raven.agent.subagent.dag_skills import HEADING
    from tests.test_subagent_dag_runner import draining_dag_runs

    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool, nodes=[_node_spec(skills=[])])
        state = tool._run_contexts[run_id].state

        await _reassign_and_settle(tool, run_id)

        source_prompt = fakes["alpha"].calls[0]["task"]
        execution_prompt = state.effective_nodes["a"].prompt_template
        assert source_prompt.count(HEADING) == 1, "baseline: submission folded the menu-less node once"
        assert execution_prompt.count(HEADING) == 1, "the target gets one fold, not the source's copy plus another"
        assert "This step uses no skills" in execution_prompt
        target_prompt = fakes["beta"].calls[0]["task"]
        assert target_prompt.count(HEADING) == 1, "the dispatched handover carries the task once"
        tool.request_cancel(run_id)


async def test_a_groups_skills_reach_the_member_that_opens_the_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the group's head may declare skills, and the head need not be the mover."""
    from raven.agent.subagent.dag_skills import HEADING
    from tests.test_subagent_dag_runner import _wait_until, draining_dag_runs

    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), _local("beta", stateful=True)],
            backends={"alpha": _FakeAgent("partial"), "beta": _FakeAgent("fixed")},
            judge=_short_once({"b"}),
        )
        nodes = [
            _node_spec("a", instance="shared", skills=[]),
            _node_spec("b", instance="shared", depends_on=["a"]),
        ]
        run_id = await _suspended(tool, nodes=nodes, node_id="b")
        await _wait_until(lambda: tool._desks[run_id].is_open("b"))
        state = tool._run_contexts[run_id].state

        await _reassign_and_settle(tool, run_id, node_id="b")

        execution_prompt = state.effective_nodes["b"].prompt_template
        assert execution_prompt.count(HEADING) == 1, "the group's declared skills follow the mover"
        assert "This step uses no skills" in execution_prompt
        tool.request_cancel(run_id)


async def test_the_moved_attempt_reads_the_targets_resolvers_and_row_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent.registry import AgentRegistry
    from raven.config.schema import BuiltinAgentConfig
    from tests.test_subagent_dag_runner import draining_dag_runs

    built: list[tuple[str, Any]] = []
    fakes = {"alpha": _FakeAgent("partial"), "beta": _FakeAgent("fixed")}

    def _factory(row: Any, build: Any) -> Any:
        built.append((row.name, build))
        return fakes[row.name]

    registry = AgentRegistry(build_builtin=_factory)
    registry.apply([BuiltinAgentConfig(name="alpha"), BuiltinAgentConfig(name="beta")])
    state_calls: list[tuple] = []
    mode_calls: list[tuple] = []
    model_calls: list[tuple] = []
    memory_calls: list[str] = []

    async with draining_dag_runs():
        tool = SubAgentDagTool(
            workspace=tmp_path,
            registry=registry,
            session_dir=None,
            ask=_Ask(),
            state_for=lambda *args: state_calls.append(args),
            mode_for=lambda *args: mode_calls.append(args),
            model_for=lambda *args: model_calls.append(args),
            memory_for=lambda name: memory_calls.append(name),
        )
        tool.set_context("web", "default", _REASSIGN_SESSION)
        tool._judge_node = lambda: _short_once({"a"})
        run_id = await _suspended(tool, nodes=[_node_spec(skills=["demo"])])

        prepared = await _reassign_and_settle(tool, run_id)

        assert built[0][0] == "alpha", "the submitted node resolved its own row"
        assert built[1:] and all(name == "beta" for name, _build in built[1:]), (
            "the target row built the moved node's backend"
        )
        target_build = built[-1][1]
        assert target_build is not None and target_build.skills_allow == ["demo"], (
            "the node's skills narrowing reaches the target backend"
        )
        assert state_calls[-1][1] == "beta", "the destination attempt reads the target's instance state"
        assert mode_calls[-1][1] == "beta", "and its mode"
        assert model_calls[-1][1] == "beta", "and its model"
        assert memory_calls and memory_calls[-1] == "beta", "and records memory under the target's identity"
        assert prepared.target_agent == "beta"
        tool.request_cancel(run_id)


class _TwoToolLoop:
    """A host whose registered graph tool is not the one running the graph."""

    def __init__(self, public: Any, private: Any) -> None:
        self.tools = _Registry(public)
        self._tools = [public, private]

    def dag_tools(self) -> list[Any]:
        return list(self._tools)


async def test_a_reassign_in_a_private_tool_uses_that_tools_desk_and_admission_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registered instance shares only the on-disk index; the run lives in the private one."""
    from tests.test_subagent_dag_runner import draining_dag_runs

    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()
    async with draining_dag_runs():
        public, _pfakes, _pask, _pcharge = _real_tool(tmp_path / "public", monkeypatch)
        private, _vfakes, _vask, _vcharge = _real_tool(tmp_path / "private", monkeypatch)
        run_id = await _suspended(private)
        assert run_id in private._outboxes, "the bound run's reports wait on the private tool's outbox"
        assert run_id not in public._outboxes
        control = ResolveDagNodeTool(loop=_TwoToolLoop(public, private))
        control.set_context("web", "default", _REASSIGN_SESSION)

        out = await asyncio.wait_for(
            control.execute(run_id=run_id, node_id="a", decision="reassign", agent="beta", message="use it"),
            timeout=10,
        )

        text = getattr(out, "model_text", out)
        assert f"DAG run {run_id} finished" in text, "the bound call is handed the run's next event"
        assert public._desks == {}, "the registered instance never held this run"
        assert public._run_contexts == {}
        assert run_id not in private._run_contexts, "the move ran against the instance that owned the run"
        private.request_cancel(run_id)


async def test_a_second_move_of_the_same_group_is_refused_while_the_first_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One group has one destination at a time; the claim is released on install."""
    from tests.test_subagent_dag_runner import _wait_until, draining_dag_runs

    alpha = _FakeAgent("partial")
    alpha.hold = asyncio.Event()
    alpha.hold_ids = {"c"}
    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), _local("beta", stateful=True), _local("gamma", stateful=True)],
            backends={"alpha": alpha, "beta": _FakeAgent("fixed"), "gamma": _FakeAgent("gamma")},
            judge=_short_once({"a", "b"}),
        )
        run_id = await _suspended(
            tool,
            nodes=[
                _node_spec("a", instance="shared"),
                _node_spec("b", instance="shared"),
                _node_spec("c", instance="shared"),
            ],
        )
        state = tool._run_contexts[run_id].state
        from tests.test_subagent_dag_runner import _wait_until

        await _wait_until(lambda: tool._desks[run_id].is_open("b"))
        await _wait_until(lambda: any(call["kwargs"].get("task_id") == "c" for call in alpha.calls))

        first = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)
        assert isinstance(first, ReassignPlan), first
        assert [node.id for node in first.nodes] == ["a", "b", "c"], "the running sibling may still join"
        assert tool.commit_reassign(run_id, first, "use the parser")
        await _wait_until(lambda: bool(state.pending_moves), timeout=5)

        second = await tool.prepare_reassign(run_id, "b", "gamma", "use the parser", _REASSIGN_SESSION)

        assert isinstance(second, str), second
        assert "already has an accepted move in progress" in second
        assert "no decision was recorded" in second.lower()
        assert tool._desks[run_id].peek("b") is None

        alpha.hold.set()
        # The run is still waiting on b's open question; answering it lets the
        # scheduler pass that installs the staged move run.
        assert tool._desks[run_id].resolve("b", CONTINUE, "try again on the destination")
        await _wait_until(
            lambda: state.effective_nodes["a"].subagent == "beta" and state.effective_nodes["b"].subagent == "beta",
            timeout=5,
        )
        await _wait_until(lambda: not tool._runs or run_id not in tool._runs, timeout=10)
        admission = tool._run_contexts.get(run_id)
        if admission is not None:
            (reservation,) = admission.reservations.values()
            assert tool._reservation_draining(state, reservation) is False, "an installed move no longer drains"
        tool.request_cancel(run_id)


class _MutatingGrantAgent(_GrantingAgent):
    """A grant resolver that changes the roster while the move is being built."""

    def __init__(self, reply: str, mutate: Any) -> None:
        super().__init__(reply)
        self._mutate = mutate

    def resolve_mcp_grant(self, mcps: list[str] | None = None) -> Any:
        self._mutate()
        return super().resolve_mcp_grant(mcps)


async def test_a_terminal_siblings_paths_do_not_gate_the_move(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed member stays where it ran; its prompt must not veto a safe move.

    With a and b on one instance, a abandoned and b continued, the move's
    possible set still names a -- the tool cannot tell it from a parked node --
    but a is terminal, so its path requirement must not refuse a move the runner
    will never apply to it.
    """
    from tests.test_subagent_dag_runner import _wait_until, draining_dag_runs

    (tmp_path / "notes.md").write_text("the file a reads", encoding="utf-8")
    remote = ThirdPartyCliSubagentConfig(
        name="remote",
        command="true",
        resume_command="true {agent_id}",
        id_source="derived",
        machine="worker-a",
    )
    shortfalls = {"a": 1, "b": 2}

    async def _judge(*, node: Any, **_kwargs: Any) -> Verdict:
        if shortfalls.get(node.id, 0) > 0:
            shortfalls[node.id] -= 1
            return Verdict(accomplished=False, category="tool_failure", what_is_missing="a parser")
        return Verdict(accomplished=True)

    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), remote],
            backends={"alpha": _FakeAgent("partial"), "remote": _FakeAgent("remote")},
            judge=_judge,
        )
        run_id = await _suspended(
            tool,
            nodes=[
                _node_spec("a", instance="shared", prompt_template="inspect {{ ref_path:notes.md }}"),
                _node_spec("b", instance="shared"),
            ],
            node_id="a",
        )
        await _wait_until(lambda: tool._desks[run_id].is_open("b"))
        state = tool._run_contexts[run_id].state
        # Both questions are answered in one batch: a is abandoned, b continues
        # and its next attempt falls short again, so a is terminal while b waits.
        assert tool.resolve_node(run_id, "a", ABANDON, None)
        assert tool.resolve_node(run_id, "b", CONTINUE, "try once more")
        await _wait_until(lambda: tool._desks[run_id].peek("a") is None)
        await _wait_until(lambda: len(state.attempt_agents.get("b", [])) == 2)
        await _wait_until(lambda: tool._desks[run_id].is_open("b"))

        prepared = await tool.prepare_reassign(run_id, "b", "remote", "use the parser", _REASSIGN_SESSION)

        assert isinstance(prepared, ReassignPlan), prepared
        assert [node.id for node in prepared.nodes] == ["b"], "a failed member is not a mover"
        assert fakes["remote"].calls == [], "preparing a remote target touches no host"
        tool.request_cancel(run_id)


async def test_a_failed_install_releases_the_group_for_another_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move that cannot be built fails its node and frees the group it reserved."""
    from tests.test_subagent_dag_runner import _wait_until, draining_dag_runs

    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), _local("beta", stateful=True)],
            backends={"alpha": _FakeAgent("partial"), "beta": _FakeAgent("fixed")},
            judge=_short_once({"a", "b"}),
        )
        run_id = await _suspended(tool, nodes=[_node_spec("a", instance="shared"), _node_spec("b", instance="shared")])
        await _wait_until(lambda: tool._desks[run_id].is_open("b"))
        state = tool._run_contexts[run_id].state

        first = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)
        assert isinstance(first, ReassignPlan), first
        assert tool.commit_reassign(run_id, first, "use the parser")
        # The install builds its targets on the next scheduler pass; removing the
        # destination backend now makes that build fail.
        broken = dict(fakes)
        broken.pop("beta")
        monkeypatch.setattr(tool, "_resolve_node", lambda node: broken.get(node.subagent))
        await _wait_until(lambda: bool(state.dropped_moves), timeout=5)

        fakes["beta"] = _FakeAgent("fixed")
        monkeypatch.setattr(tool, "_resolve_node", lambda node: fakes.get(node.subagent))
        second = await tool.prepare_reassign(run_id, "b", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(second, ReassignPlan), second
        tool.request_cancel(run_id)


async def test_a_cancelled_run_is_refused_before_the_user_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_subagent_dag_runner import draining_dag_runs

    async with draining_dag_runs():
        tool, _fakes, ask, _charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool)
        tool.request_cancel(run_id)
        asked = len(ask.calls)

        out = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(out, str), out
        assert "cancelled" in out
        assert "no decision was recorded" in out.lower()
        assert len(ask.calls) == asked, "a run being stopped must not reach the user"
        tool.request_cancel(run_id)


async def test_a_replanned_run_is_refused_before_the_user_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_subagent_dag_runner import draining_dag_runs

    async with draining_dag_runs():
        tool, _fakes, ask, _charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool)
        tool._desks[run_id].replanned.set()
        asked = len(ask.calls)

        out = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(out, str), out
        assert "replan" in out.lower()
        assert "no decision was recorded" in out.lower()
        assert len(ask.calls) == asked, "a graph being replaced must not reach the user"
        tool.request_cancel(run_id)


async def test_the_target_is_revalidated_after_the_build_without_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-build reread is not tied to the confirmation question."""
    from tests.test_subagent_dag_runner import draining_dag_runs

    tool_ref: dict[str, Any] = {}

    def _disable_beta() -> None:
        tool_ref["tool"]._registry.apply(
            [
                _local("alpha"),
                ThirdPartyCliSubagentConfig(name="beta", command="cat {mcp_file}", enabled=False),
            ]
        )

    beta = _MutatingGrantAgent("fixed", _disable_beta)
    async with draining_dag_runs():
        tool, _fakes, ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha"), ThirdPartyCliSubagentConfig(name="beta", command="cat {mcp_file}")],
            backends={"alpha": _FakeAgent("partial"), "beta": beta},
        )
        tool_ref["tool"] = tool
        run_id = await _suspended(tool, confirm=False)
        asked = len(ask.calls)

        out = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(out, str), out
        assert "changed while the reassign was being prepared" in out
        assert "no decision was recorded" in out.lower()
        assert len(ask.calls) == asked
        tool.request_cancel(run_id)


async def test_a_replaced_waiter_refuses_the_commit_and_consumes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_subagent_dag_runner import draining_dag_runs

    async with draining_dag_runs():
        tool, _fakes, _ask, _charge = _real_tool(tmp_path, monkeypatch)
        run_id = await _suspended(tool)
        prepared = await tool.prepare_reassign(run_id, "a", "beta", "use the parser", _REASSIGN_SESSION)
        assert isinstance(prepared, ReassignPlan), prepared
        desk = tool._desks[run_id]
        replacement = desk.open("a")
        handed_off: list[Any] = []
        resolve = desk.resolve

        def _spy(*args: Any, **kwargs: Any) -> bool:
            handed_off.append(args)
            return resolve(*args, **kwargs)

        desk.resolve = _spy  # type: ignore[method-assign]

        assert desk.waiter("a") is replacement
        assert tool.commit_reassign(run_id, prepared, "use the parser") is False

        assert handed_off == [], "the commit must refuse before handing anything to the desk"
        assert desk.peek("a") is None, "no answer may be recorded against the replacement"
        assert desk.is_open("a")
        tool.request_cancel(run_id)


async def test_a_pending_siblings_unreadable_path_is_refused_up_front(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending sibling is a certain mover, so its paths must gate the move."""
    from tests.test_subagent_dag_runner import draining_dag_runs

    (tmp_path / "notes.md").write_text("the file a reads", encoding="utf-8")
    remote = ThirdPartyCliSubagentConfig(
        name="remote",
        command="true",
        resume_command="true {agent_id}",
        id_source="derived",
        machine="worker-a",
    )
    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), remote],
            backends={"alpha": _FakeAgent("partial"), "remote": _FakeAgent("remote")},
            judge=_short_once({"b"}),
        )
        run_id = await _suspended(
            tool,
            nodes=[
                _node_spec("a", instance="shared", depends_on=["b"], prompt_template="inspect {{ ref_path:notes.md }}"),
                _node_spec("b", instance="shared"),
            ],
            node_id="b",
        )
        state = tool._run_contexts[run_id].state
        assert state.status["a"] == "pending", "a waits for its dependency, so it is a certain mover"

        out = await tool.prepare_reassign(run_id, "b", "remote", "use the parser", _REASSIGN_SESSION)

        assert isinstance(out, str), out
        assert "node 'a'" in out
        assert "no-local-files" in out or "local file paths" in out
        assert "no decision was recorded" in out.lower()
        assert fakes["remote"].calls == []
        tool.request_cancel(run_id)


async def test_a_running_siblings_unreadable_path_is_refused_up_front(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling still attempting may land unfinished and join the move."""
    from tests.test_subagent_dag_runner import _wait_until, draining_dag_runs

    (tmp_path / "notes.md").write_text("the file b reads", encoding="utf-8")
    remote = ThirdPartyCliSubagentConfig(
        name="remote",
        command="true",
        resume_command="true {agent_id}",
        id_source="derived",
        machine="worker-a",
    )
    alpha = _FakeAgent("partial")
    alpha.hold = asyncio.Event()
    alpha.hold_ids = {"b"}
    async with draining_dag_runs():
        tool, fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), remote],
            backends={"alpha": alpha, "remote": _FakeAgent("remote")},
            judge=_short_once({"a"}),
        )
        run_id = await _suspended(
            tool,
            nodes=[
                _node_spec("a", instance="shared"),
                _node_spec("b", instance="shared", prompt_template="inspect {{ ref_path:notes.md }}"),
            ],
            node_id="a",
        )
        state = tool._run_contexts[run_id].state
        await _wait_until(lambda: state.status.get("b") == "running")

        out = await tool.prepare_reassign(run_id, "a", "remote", "use the parser", _REASSIGN_SESSION)

        assert isinstance(out, str), out
        assert "node 'b'" in out
        assert "no-local-files" in out or "local file paths" in out
        assert "no decision was recorded" in out.lower()
        alpha.hold.set()
        tool.request_cancel(run_id)


async def test_a_judge_held_siblings_unreadable_path_is_refused_up_front(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backend's return reads completed, but the judge has not ruled and may
    suspend the attempt: that sibling may still move, so its paths are gated."""
    from tests.test_subagent_dag_runner import draining_dag_runs

    (tmp_path / "notes.md").write_text("the file b reads", encoding="utf-8")
    remote = ThirdPartyCliSubagentConfig(
        name="remote",
        command="true",
        resume_command="true {agent_id}",
        id_source="derived",
        machine="worker-a",
    )
    judging = asyncio.Event()
    release = asyncio.Event()

    async def _judge(*, node: Any, **_kwargs: Any) -> Verdict:
        if node.id == "b":
            judging.set()
            await release.wait()
            return Verdict(accomplished=False, what_is_missing="still short")
        return Verdict(accomplished=False, category="tool_failure", what_is_missing="a parser")

    async with draining_dag_runs():
        tool, _fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), remote],
            backends={"alpha": _FakeAgent("partial"), "remote": _FakeAgent("remote")},
            judge=_judge,
        )
        run_id = await _suspended(
            tool,
            nodes=[
                _node_spec("a", instance="shared"),
                _node_spec("b", instance="shared", prompt_template="inspect {{ ref_path:notes.md }}"),
            ],
            node_id="a",
        )
        state = tool._run_contexts[run_id].state
        await asyncio.wait_for(judging.wait(), timeout=10)
        assert state.status["b"] == "completed", "the backend returned while the judge is still out"
        assert state.active_attempts["b"] == ("alpha", "shared")

        out = await tool.prepare_reassign(run_id, "a", "remote", "use the parser", _REASSIGN_SESSION)

        assert isinstance(out, str), out
        assert "node 'b'" in out
        assert "no-local-files" in out or "local file paths" in out
        assert "no decision was recorded" in out.lower()
        release.set()
        tool.request_cancel(run_id)


async def test_the_move_installer_gates_every_node_it_is_about_to_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer reads node status a second time; the build boundary applies
    the same file-reference gate, so a target that cannot read the node's paths
    refuses the move instead of dispatching it."""
    from tests.test_subagent_dag_runner import draining_dag_runs

    (tmp_path / "notes.md").write_text("the file a reads", encoding="utf-8")
    remote = ThirdPartyCliSubagentConfig(
        name="remote",
        command="true",
        resume_command="true {agent_id}",
        id_source="derived",
        machine="worker-a",
    )
    async with draining_dag_runs():
        tool, _fakes, _ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), remote],
            backends={"alpha": _FakeAgent("partial"), "remote": _FakeAgent("remote")},
        )
        run_id = await _suspended(
            tool,
            nodes=[
                _node_spec("a", instance="shared", prompt_template="inspect {{ ref_path:notes.md }}"),
                _node_spec("b"),
            ],
        )
        admission = tool._run_contexts[run_id]
        moved = admission.authored()["a"].model_copy(update={"subagent": "remote", "instance": "shared"})

        with pytest.raises(DagValidationError) as refused:
            await admission.build(moved)

        assert "node 'a'" in str(refused.value)
        assert "no-local-files" in str(refused.value) or "local file paths" in str(refused.value)

        execution, _backend = await admission.build(admission.authored()["b"].model_copy(update={"subagent": "remote"}))
        assert (execution.subagent, execution.instance) == ("remote", None), "a path-free node still builds"
        tool.request_cancel(run_id)


async def test_the_confirmation_does_not_list_a_terminal_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_subagent_dag_runner import _wait_until, draining_dag_runs

    shortfalls = {"a": 1, "b": 2}

    async def _judge(*, node: Any, **_kwargs: Any) -> Verdict:
        if shortfalls.get(node.id, 0) > 0:
            shortfalls[node.id] -= 1
            return Verdict(accomplished=False, category="tool_failure", what_is_missing="a parser")
        return Verdict(accomplished=True)

    async with draining_dag_runs():
        tool, _fakes, ask, _charge = _real_tool(
            tmp_path,
            monkeypatch,
            agents=[_local("alpha", stateful=True), _local("beta", stateful=True)],
            backends={"alpha": _FakeAgent("partial"), "beta": _FakeAgent("fixed")},
            judge=_judge,
        )
        run_id = await _suspended(
            tool, nodes=[_node_spec("a", instance="shared"), _node_spec("b", instance="shared")], node_id="a"
        )
        await _wait_until(lambda: tool._desks[run_id].is_open("b"))
        state = tool._run_contexts[run_id].state
        assert tool.resolve_node(run_id, "a", ABANDON, None)
        assert tool.resolve_node(run_id, "b", CONTINUE, "try once more")
        await _wait_until(lambda: tool._desks[run_id].peek("a") is None)
        await _wait_until(lambda: len(state.attempt_agents.get("b", [])) == 2)
        await _wait_until(lambda: tool._desks[run_id].is_open("b"))

        prepared = await tool.prepare_reassign(run_id, "b", "beta", "use the parser", _REASSIGN_SESSION)

        assert isinstance(prepared, ReassignPlan), prepared
        assert [node.id for node in prepared.nodes] == ["b"], "a failed member is not a mover"
        question = ask.calls[-1][1]
        assert "- b: alpha -> beta" in question
        assert "- a:" not in question, "a terminal member must not be named as a possible mover"
        assert "may move" in question or "Reassign 1 node(s)" in question
        tool.request_cancel(run_id)
