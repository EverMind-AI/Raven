"""DAG runner + native run_subagent_dag tool."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import posixpath
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.agent import workdir
from raven.agent.subagent import instances as instances_mod
from raven.agent.subagent.backends import format_agent_listing, third_party_agent_meta
from raven.agent.subagent.backends.base import clamp_output
from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.agent.subagent.dag_graph import DagNodeSpec, parse_dag_spec
from raven.agent.subagent.dag_runner import DagRunResult, run_dag
from raven.agent.subagent.dag_store import DagRunStore, read_session_nodes
from raven.agent.subagent.dag_tool import _NODE_SCHEMA, GUIDE_SKILL_ID, SubAgentDagTool
from raven.agent.subagent.mcp_grant import McpGrant, MissingServer
from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.config.schema import ThirdPartyAcpSubagentConfig, ThirdPartyCliSubagentConfig
from raven.contracts.tool import ToolResult

#: How long a drain waits for a cancelled background run to finish. Generous
#: against the work (a node's subprocess teardown is milliseconds) and short
#: against the alternative, which is an unbounded wait inside loop close.
_DRAIN_TIMEOUT_S = 10.0

#: How long the failure path gives a run to land after its sub-agent tree has
#: been killed, before declaring the task unreachable. Only spent on failures.
_KILL_GRACE_S = 2.0


def _discover_child_pids() -> list[int]:
    """The pids of this process's direct children, without external binaries.

    ``/proc`` is the Linux answer and needs nothing installed -- the GitLab
    test image has no procps, so a `pgrep`-only discovery silently reaps
    nothing there. ``pgrep -P`` covers the other POSIX boxes; where neither
    exists this returns nothing and the detach below still bounds loop close.
    """
    children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    if children.exists():
        try:
            return [int(x) for x in children.read_text(encoding="utf-8").split()]
        except (OSError, ValueError):
            return []
    import shutil
    import subprocess

    pgrep = shutil.which("pgrep")
    if not pgrep:
        return []
    try:
        out = subprocess.run([pgrep, "-P", str(os.getpid())], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in out.stdout.split():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def _kill_direct_children() -> None:
    """SIGKILL every process group this worker's children lead, else the child.

    Raven starts CLI launchers with `start_new_session=True`, so a launcher is
    its group's leader and its pid doubles as the group's pgid; killing only
    the launcher leaks Codex-style workers that stay in (or reparent from) that
    group -- the topology `CliAgentBackend._kill_process_group` documents. A
    child that is not a leader has no group of its own, so `killpg` raises and
    the plain kill covers it; our own process group is never touched because
    only child pids are sent.

    A wedged DAG run can hold a live sub-agent process even though its own task
    is unreachable, so the failure path has to reap the tree itself; where
    discovery finds nothing this degrades to a no-op, and the detach still
    bounds loop close.
    """
    import signal

    for pid in _discover_child_pids():
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _detach_from_loop_close(tasks: "list[asyncio.Task]") -> None:
    """Drop the wedged tasks from the loop's registry so teardown cannot wait.

    ``Runner.close()`` gathers ``asyncio.tasks.all_tasks(loop)`` with no
    timeout, and a task suspended on a future whose cancellation was swallowed
    never answers that gather. Nothing in-process can terminate such a task --
    not cancel, not killing its child -- so taking it out of the registry is
    the one way to bound loop close. ``_scheduled_tasks`` is the CPython 3.12
    name for that registry; the assertion in
    ``test_a_run_that_ignores_cancellation_fails_named_instead_of_hanging``
    fails loudly if a Python bump renames it, instead of silently hanging.
    """
    import asyncio.tasks as _tasks

    registry = _tasks._scheduled_tasks
    for task in tasks:
        registry.discard(task)
        # Without this, every detached task prints "Task was destroyed but it
        # is pending!" at interpreter shutdown -- noise on top of a failure
        # that already names the run. The same flag asyncio itself sets for
        # the internal tasks it expects to be abandoned.
        task._log_destroy_pending = False


async def drain_dag_runs(
    tools: "list[SubAgentDagTool]",
    *,
    timeout: float = _DRAIN_TIMEOUT_S,
    kill_grace: float = _KILL_GRACE_S,
) -> None:
    """Stop every background run these tools started, or say which would not.

    Sets each run's cancel event before cancelling the task: the event is the
    cooperative path the runner checks between nodes, and reaching a node
    boundary is far cheaper than unwinding a subprocess mid-setup.

    A run still pending after the timeout is a failure, and the failure path is
    a ladder in the one order that can help. The sub-agent tree is killed
    first -- a run wedged mid-spawn is the shape that leaks its child, and a
    dead child is the only thing that resolves the awaits that can be
    resolved. A short grace then lets those land. Anything still standing is
    suspended on a future that cannot resolve, so it is detached from loop
    close before the failure is raised: otherwise ``Runner.close()`` gathers
    it again, unbounded, and the suite hangs before the message is ever shown.
    """
    pending: list[asyncio.Task] = []
    for tool in tools:
        for event in list(tool._cancels.values()):
            event.set()
        pending.extend(tool._runs.values())
    if not pending:
        return

    for task in pending:
        task.cancel()
    _, stalled = await asyncio.wait(pending, timeout=timeout)
    if not stalled:
        return

    _kill_direct_children()
    # The first timed-out set is the diagnostic: it names what actually
    # ignored cancellation. The post-grace set is only for the detach, because
    # a task that landed during grace -- which is what the kill is for -- is
    # gone from it, and building the message from it would report "...: "
    # with no run named.
    _, still_running = await asyncio.wait(list(stalled), timeout=kill_grace)
    if still_running:
        _detach_from_loop_close(list(still_running))
    names = ", ".join(sorted(t.get_name() for t in stalled))
    pytest.fail(f"background DAG run(s) ignored cancellation for {timeout}s: {names}")


@contextlib.asynccontextmanager
async def draining_dag_runs():
    """Drain every ``SubAgentDagTool`` built inside the block.

    Tracks constructions rather than asking each test to hand its tools over:
    the tools are built by helpers and inside the calls under test, and a drain
    that depends on remembering to register one is a drain that silently stops
    covering the next test somebody writes.
    """
    created: list[SubAgentDagTool] = []
    real_init = SubAgentDagTool.__init__

    def _tracking_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        created.append(self)

    SubAgentDagTool.__init__ = _tracking_init  # type: ignore[method-assign]
    try:
        yield
    finally:
        SubAgentDagTool.__init__ = real_init  # type: ignore[method-assign]
        await drain_dag_runs(created)


def _by_name(mapping):
    """A ``resolve`` for ``run_dag`` from a plain name->backend map.

    ``run_dag`` resolves per node rather than per name, because a node narrows its
    own session (its ``skills``) and two nodes may name one agent with different
    lists. Tests still describe their fleet by name, so this adapts one to the
    other.
    """
    return lambda node: mapping.get(node.subagent)


@pytest.fixture(autouse=True)
def _isolated_instance_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module must never touch the real user registry file.

    Without this, any test whose DAG run carries a `session_key` (directly, or
    via `SubAgentDagTool.set_context`) resolves `get_registry()` to the
    process-wide singleton, i.e. the real `~/.raven/subagent_instances.json` --
    accumulating one junk row per node per run across every test run, forever
    (each run mints a fresh run_id, so nothing ever overwrites an old row).
    """
    monkeypatch.setattr(
        instances_mod, "_registry", instances_mod.InstanceRegistry(path=tmp_path / "_autouse_inst.json")
    )


@pytest.fixture(autouse=True)
def _isolated_record_tasks() -> None:
    """`_RECORD_TASKS` is a process-global GC anchor, not scoped to a run.

    Left alone, a test whose assertion runs before its own poller finishes
    draining (or that fails before reaching that assertion) leaves an entry
    behind that would poison every later test asserting on the same set.
    Clearing it around each test makes those assertions test-local instead.
    """
    from raven.agent.subagent import dag_runner as runner_mod

    runner_mod._RECORD_TASKS.clear()
    yield
    runner_mod._RECORD_TASKS.clear()


class _InMemBackend:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def join_path(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def abspath(self, path: str, cwd: str | None = None) -> str:
        return path if path.startswith("/") else posixpath.join(cwd or "/", path)

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def read_file(self, path: str) -> bytes:
        return self.files[path]

    async def file_exists(self, path: str) -> bool:
        return path in self.files


class _FakeExec:
    """A SubagentBackend that echoes its task, tagged by node id."""

    def __init__(self, fail_ids: set[str] | None = None, *, reply: str | None = None) -> None:
        self.fail_ids = fail_ids or set()
        self.calls: list[dict] = []
        self.reply = reply

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace,
        executor,
        session_key: str | None = None,
        instance: str | None = None,
        mode: str | None = None,
    ) -> str:
        self.calls.append(
            {"task_id": task_id, "session_key": session_key, "instance": instance, "prompt": task, "mode": mode}
        )
        if task_id in self.fail_ids:
            raise RuntimeError(f"boom {task_id}")
        if self.reply is not None:
            # A narrating backend's closing text is what `_add_node_to_instance_log`
            # reads as the node's answer, not this return value -- so a test that
            # cares what lands in the instance log has to set it the same way
            # acp_agent.py does, not just return the text.
            from raven.agent.subagent import activity

            activity.note_closing(self.reply)
            return self.reply
        return f"OUT[{task_id}]:{task}"


# --- runner --------------------------------------------------------------


async def test_run_dag_runs_a_sub_agent_whose_name_has_a_space() -> None:
    # The roster key travels from the node spec through the capability check,
    # the backend lookup and the status records. A name `spawn` accepts must
    # work here too, so the whole path is exercised rather than only parsing.
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {"id": "a", "subagent": "General Audit", "node_summary": "say hello", "prompt_template": "hello"},
                {
                    "id": "b",
                    "subagent": "General Audit",
                    "node_summary": "echo node a's greeting",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ],
        }
    )
    result = await run_dag(
        spec,
        resolve=_by_name({"General Audit": _FakeExec()}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
    )
    assert result.summary == {"total": 2, "completed": 2, "failed": 0, "skipped": 0, "cancelled": 0}
    # The name survives into the per-node records the UI and the resume path read.
    assert {f["subagent"] for f in result.files} == {"General Audit"}


async def test_run_dag_hands_each_node_the_invoking_turn_binding() -> None:
    """A pooled ACP worker keeps the binding it was launched with, so the graph
    has to carry the turn's provider and model to every node the way `spawn`
    does. Without it a node ran on the worker's default whatever the session had
    selected. Only when the tool resolved one: the fakes above take no such
    keywords, and they must keep working.
    """

    class _Recording:
        def __init__(self) -> None:
            self.kwargs: list[dict] = []

        async def run(self, task: str, *, task_id: str, workspace, executor, **kwargs) -> str:
            self.kwargs.append(kwargs)
            return f"OUT[{task_id}]"

    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "say hello", "prompt_template": "hello"}],
        }
    )
    node = _Recording()
    provider = object()
    result = await run_dag(
        spec,
        resolve=_by_name({"x": node}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        provider=provider,
        model="claude-opus-5",
    )
    assert result.summary["completed"] == 1
    assert node.kwargs[0]["provider"] is provider and node.kwargs[0]["model"] == "claude-opus-5"


async def test_the_graph_tool_resolves_the_turn_binding_per_dispatch(tmp_path: Path, monkeypatch) -> None:
    """Read at dispatch, not when the tool was built, for the reason the judge's
    provider is: a session that switched model must reach its nodes."""

    class _Recording:
        def __init__(self) -> None:
            self.kwargs: list[dict] = []

        async def run(self, task: str, *, task_id: str, workspace, executor, **kwargs) -> str:
            self.kwargs.append(kwargs)
            return f"OUT[{task_id}]"

    bindings = iter([("p-one", "m-one"), ("p-two", "m-two")])
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        binding_for=lambda: next(bindings),
    )
    node = _Recording()
    monkeypatch.setattr(tool, "_resolve_node", lambda _node: node)
    # Two node ids: they are unique per conversation, so a second graph
    # reusing `a` would be refused before anything was dispatched.
    for node_id, expected in (("a", ("p-one", "m-one")), ("b", ("p-two", "m-two"))):
        await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": node_id, "subagent": "echo", "node_summary": "say hello", "prompt_template": "go"}],
            background=False,
        )
        assert (node.kwargs[-1]["provider"], node.kwargs[-1]["model"]) == expected


async def test_run_dag_passes_output_downstream_and_emits_progress() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {"id": "a", "subagent": "x", "node_summary": "say hello", "prompt_template": "hello"},
                {
                    "id": "b",
                    "subagent": "x",
                    "node_summary": "pass node a's output downstream",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ],
        }
    )
    events: list[tuple[str, dict]] = []

    async def pub(name, value):
        events.append((name, value))

    result = await run_dag(
        spec,
        resolve=_by_name({"x": _FakeExec()}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        progress_publisher=pub,
    )

    assert result.summary == {"total": 2, "completed": 2, "failed": 0, "skipped": 0, "cancelled": 0}
    # b is the only sink -> terminal output; it received a's output through a file.
    assert len(result.terminal_outputs) == 1
    term = result.terminal_outputs[0]
    assert term["node"] == "b"
    assert "OUT[a]:hello" in term["text"]
    # progress: one run_started + node updates incl. b completed
    names = [n for n, _ in events]
    assert names.count("dag_run_started") == 1
    assert any(v["node"] == "b" and v["status"] == "completed" for n, v in events if n == "dag_node_updated")


async def test_the_started_event_names_each_node() -> None:
    # The event is the authoritative live source for a row's subject: unlike the
    # prompt template, it needs no correlation with the tool call.
    spec = parse_dag_spec(
        {
            "task_summary": "compare the two vendors",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "read the pricing pages", "prompt_template": "hi"}],
        }
    )
    events: list[tuple[str, dict]] = []

    async def pub(name, value):
        events.append((name, value))

    await run_dag(
        spec,
        resolve=_by_name({"x": _FakeExec()}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        progress_publisher=pub,
    )

    started = next(e for e in events if e[0] == "dag_run_started")
    assert started[1]["nodes"][0]["node_summary"] == "read the pricing pages"


async def test_run_dag_threads_session_key_and_node_instance_to_backend() -> None:
    # Nodes sharing an `instance` handle must resume the same stateful CLI
    # session; that only works if run_dag forwards both the DAG's session_key
    # and each node's own instance down to backend.run.
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "node a",
                    "prompt_template": "hi",
                    "instance": "refactor-auth",
                },
                {"id": "b", "subagent": "x", "node_summary": "node b", "prompt_template": "hi again"},
            ],
        }
    )
    backend = _FakeExec()
    await run_dag(
        spec,
        resolve=_by_name({"x": backend}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="web:sess1",
    )
    calls_by_id = {c["task_id"]: c for c in backend.calls}
    assert calls_by_id["a"]["session_key"] == "web:sess1"
    assert calls_by_id["a"]["instance"] == "refactor-auth"
    assert calls_by_id["b"]["session_key"] == "web:sess1"
    assert calls_by_id["b"]["instance"] is None


async def test_run_dag_preserves_omitted_override_and_explicit_empty_mcps() -> None:
    omitted = object()

    class McpExec(_FakeExec):
        async def run(
            self,
            task: str,
            *,
            task_id: str,
            workspace: Any,
            executor: Any,
            session_key: str | None = None,
            instance: str | None = None,
            mode: str | None = None,
            mcps: Any = omitted,
        ) -> str:
            self.calls.append({"task_id": task_id, "mcps": mcps})
            return task

    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {
                    "id": "default",
                    "subagent": "x",
                    "node_summary": "no mcps override",
                    "prompt_template": "default",
                },
                {
                    "id": "none",
                    "subagent": "x",
                    "node_summary": "explicitly no mcps",
                    "prompt_template": "none",
                    "mcps": [],
                },
                {
                    "id": "one",
                    "subagent": "x",
                    "node_summary": "one mcp server",
                    "prompt_template": "one",
                    "mcps": ["github"],
                },
            ],
        }
    )
    executor = McpExec()

    await run_dag(
        spec,
        resolve=_by_name({"x": executor}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
    )

    calls = {call["task_id"]: call["mcps"] for call in executor.calls}
    assert calls["default"] is omitted
    assert calls["none"] == []
    assert calls["one"] == ["github"]


async def test_run_dag_failure_cascades_to_skip() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {"id": "a", "subagent": "x", "node_summary": "the node that fails", "prompt_template": "hi"},
                {
                    "id": "b",
                    "subagent": "x",
                    "node_summary": "the node that gets skipped when a fails",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ],
        }
    )
    result = await run_dag(
        spec,
        resolve=_by_name({"x": _FakeExec(fail_ids={"a"})}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
    )
    assert result.summary["failed"] == 1
    assert result.summary["skipped"] == 1  # b skipped because a failed
    assert result.terminal_outputs == []


async def test_run_dag_writes_node_status_transitions_to_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    seen: list[tuple[str, str]] = []
    original_upsert = reg.upsert_dag_node

    async def recording_upsert(session_key: str, run_id: str, node_id: str, agent: str, status: str) -> None:
        seen.append((node_id, status))
        await original_upsert(session_key, run_id, node_id, agent, status)

    monkeypatch.setattr(reg, "upsert_dag_node", recording_upsert)
    monkeypatch.setattr(instances_mod, "_registry", reg)

    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "hello"},
                {
                    "id": "b",
                    "subagent": "x",
                    "node_summary": "node b, downstream of a",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ],
        }
    )
    result = await run_dag(
        spec,
        resolve=_by_name({"x": _FakeExec()}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="web:sess1",
    )

    assert result.summary == {"total": 2, "completed": 2, "failed": 0, "skipped": 0, "cancelled": 0}
    assert any(status == "running" for _, status in seen)
    final_status = {r["nodeId"]: r["status"] for r in reg.list_instances("web:sess1")}
    assert final_status == {"a": "completed", "b": "completed"}


async def test_run_dag_records_the_node_an_instance_handle_belongs_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node on a handle owns two rows, and nothing in either says so: the
    handle carries no mark of its node, and ``mint_handle`` promises that nothing
    keys on the slug it derives. Written here, at the one point that holds both
    halves, so a reader can report the node once instead of twice."""
    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {
                    "id": "shape",
                    "subagent": "x",
                    "node_summary": "the stateful node with a named instance",
                    "prompt_template": "hello",
                    "instance": "shape-726da8",
                },
                {
                    "id": "bare",
                    "subagent": "x",
                    "node_summary": "the stateless node with no instance",
                    "prompt_template": "hi",
                },
            ],
        }
    )
    await run_dag(
        spec,
        resolve=_by_name({"x": _FakeExec()}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="web:sess1",
        run_id="r1",
    )

    rows = {r["handle"]: r for r in reg.list_instances("web:sess1")}
    assert (rows["shape-726da8"]["runId"], rows["shape-726da8"]["nodeId"]) == ("r1", "shape")
    # A node that names no instance runs on no handle, so it has only its own row.
    assert set(rows) == {"r1/shape", "r1/bare", "shape-726da8"}


async def test_run_dag_cancel_skips_unfinished_and_reaps_in_flight_task(tmp_path: Path) -> None:
    # a and b are both independent (root) nodes so they run concurrently in the
    # same round; c depends on b, so cascade-skip is also covered. b blocks
    # until cancelled so the test can set `cancel` while it is genuinely in
    # flight, not merely pending. (An earlier revision of this test also set
    # max_concurrency=1, intending to prove a is only able to run because b's
    # semaphore slot was freed on cancellation -- but _run_ready_groups cancels
    # every not-done task in the round synchronously, in one batch, before
    # awaiting any of them, so a's cancellation is always delivered before b's
    # own unwind ever runs; a never gets the freed slot, deterministically
    # (verified 30/30 trials), not just occasionally. Demonstrating that claim
    # would require changing the cancellation scheduling itself, which is out
    # of scope here, so it was dropped -- the reaped/no-leaked-task assertions
    # below already cover the Critical-1 class of defect this test exists for.)
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "hi-a"},
                {"id": "b", "subagent": "x", "node_summary": "node b", "prompt_template": "hi-b"},
                {
                    "id": "c",
                    "subagent": "x",
                    "node_summary": "node c, downstream of b",
                    "prompt_template": "{{ b.output }}",
                    "depends_on": ["b"],
                },
            ],
        }
    )
    b_running = asyncio.Event()
    reaped: list[str] = []

    class _BlockingExec:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, mode=None) -> str:
            if task_id == "b":
                b_running.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    # Proves cancellation actually reaches the node's own work
                    # (the "child process"), not just its wrapper task.
                    reaped.append(task_id)
                    raise
                return "should never complete"
            return f"OUT[{task_id}]"

    cancel = asyncio.Event()

    async def _cancel_once_b_is_running() -> None:
        await b_running.wait()
        cancel.set()

    tasks_before = set(asyncio.all_tasks())
    canceller = asyncio.create_task(_cancel_once_b_is_running())
    try:
        result = await asyncio.wait_for(
            run_dag(
                spec,
                resolve=_by_name({"x": _BlockingExec()}),
                backend=_InMemBackend(),
                workdir="/w",
                run_root="/hist/mas_dag",
                cancel=cancel,
            ),
            timeout=5,
        )
    finally:
        # If run_dag raised (e.g. RED: no `cancel` kwarg yet) before ever
        # invoking the "b" node, b_running is never set and this waiter would
        # hang forever; cancel it unconditionally rather than awaiting it.
        canceller.cancel()
        try:
            await canceller
        except asyncio.CancelledError:
            pass

    by_node = {f["node"]: f["status"] for f in result.files}
    assert by_node["a"] == "completed"
    assert by_node["b"] == "cancelled"
    assert by_node["c"] == "skipped"  # dependent of the cancelled node
    assert result.summary == {"total": 3, "completed": 1, "failed": 0, "skipped": 1, "cancelled": 1}

    assert reaped == ["b"]  # CancelledError was actually raised into the node

    # No _run_group task -- the wrapper around each node/instance-group's
    # coroutine -- is left running past run_dag's return. A leaky
    # _run_ready_groups that breaks out of the cancel race without cancelling
    # and awaiting every task leaves exactly this behind (see
    # probe_test_sensitivity.py).
    leftover_new_tasks = set(asyncio.all_tasks()) - tasks_before - {asyncio.current_task()}
    leaked_run_groups = [t for t in leftover_new_tasks if t.get_coro().__qualname__ == "_run_group"]
    assert leaked_run_groups == []


async def test_an_injected_semaphore_bounds_concurrent_runs_together() -> None:
    """The cap has to mean total dispatches in flight, not per run.

    Every run used to build its own semaphore, which was equivalent while only
    one graph could be running; backgrounded runs overlap, so N runs would
    otherwise each get the full allowance.
    """
    live = 0
    peak = 0

    class _Tracking:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, mode=None) -> str:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1
            return f"OUT[{task_id}]"

    def _independent_nodes(prefix: str) -> Any:
        return parse_dag_spec(
            {
                "task_summary": "run the graph under test",
                "nodes": [
                    {
                        "id": f"{prefix}{i}",
                        "subagent": "x",
                        "node_summary": "an independent node",
                        "prompt_template": "hi",
                    }
                    for i in range(3)
                ],
            }
        )

    gate = asyncio.Semaphore(2)
    backend = _Tracking()
    results = await asyncio.gather(
        *[
            run_dag(
                _independent_nodes(prefix),
                resolve=_by_name({"x": backend}),
                backend=_InMemBackend(),
                workdir="/w",
                run_root="/hist/mas_dag",
                semaphore=gate,
            )
            for prefix in ("a", "b")
        ]
    )

    # == not <=: the gate has to be saturated for the bound to prove anything.
    # A private semaphore per run lets all 6 nodes reach 4 in flight instead.
    assert peak == 2, f"{peak} nodes ran at once under a shared Semaphore(2)"
    assert all(r.summary == {"total": 3, "completed": 3, "failed": 0, "skipped": 0, "cancelled": 0} for r in results)


async def test_run_dag_writes_skipped_status_to_registry_with_session_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No test combined a session_key with a node that ends up "skipped" (as
    # opposed to "completed"): the cascade-skip path writes to the registry
    # from a different call site (the scheduling loop's skip-publish block,
    # not _run_node), and that site was uncovered.
    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {"id": "a", "subagent": "x", "node_summary": "the node that fails", "prompt_template": "hi"},
                {
                    "id": "b",
                    "subagent": "x",
                    "node_summary": "the node that gets skipped",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ],
        }
    )
    result = await run_dag(
        spec,
        resolve=_by_name({"x": _FakeExec(fail_ids={"a"})}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="web:sess1",
    )

    assert result.summary == {"total": 2, "completed": 0, "failed": 1, "skipped": 1, "cancelled": 0}
    rows = {r["nodeId"]: r["status"] for r in reg.list_instances("web:sess1")}
    assert rows == {"a": "failed", "b": "skipped"}


async def test_run_dag_without_session_key_writes_nothing_to_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    calls: list[tuple] = []
    original_upsert = reg.upsert_dag_node

    async def recording_upsert(*args, **kwargs):
        calls.append((args, kwargs))
        await original_upsert(*args, **kwargs)

    monkeypatch.setattr(reg, "upsert_dag_node", recording_upsert)
    monkeypatch.setattr(instances_mod, "_registry", reg)

    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "hi"}],
        }
    )
    result = await run_dag(
        spec, resolve=_by_name({"x": _FakeExec()}), backend=_InMemBackend(), workdir="/w", run_root="/hist/mas_dag"
    )

    assert result.summary["completed"] == 1
    assert calls == []


# --- native tool end-to-end (real `cat` CLI backend) ---------------------


class _Announces:
    """A stand-in for the manager's announcer that a test can wait on."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._arrived = asyncio.Event()

    async def __call__(self, run_id: str, summary: str, origin: dict) -> None:
        self.calls.append((run_id, summary, origin))
        self._arrived.set()

    async def wait(self, count: int = 1, timeout: float = 10.0) -> list[tuple[str, str, dict]]:
        async def _poll() -> None:
            while True:
                # Clear before re-checking, with no await between the two, so a
                # call landing here cannot have its flag cleared and be missed.
                self._arrived.clear()
                if len(self.calls) >= count:
                    return
                await self._arrived.wait()

        await asyncio.wait_for(_poll(), timeout)
        return list(self.calls)


async def test_a_node_naming_an_unconfigured_server_still_lets_the_graph_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP is an attachment to the work, so it must not be able to cancel it.

    The pre-dispatch pass used to raise on a raven-loop node whose grant came back
    with anything missing, which threw away every other node in the graph over one
    name the host does not configure. Now it is a notice: the run happens, the
    caller is told which node lost what, and that node's own reply carries the
    same sentence.
    """

    class _NoServers:
        kind = "raven-loop"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def resolve_mcp_grant(self, mcps: list[str] | None = None) -> McpGrant:
            return McpGrant(missing=tuple(MissingServer(name=n, reason="not_configured") for n in (mcps or [])))

        async def run(self, task: str, *, task_id: str, workspace, executor, **kwargs) -> str:
            self.calls.append(task_id)
            return f"OUT[{task_id}]"

    # `{mcp_file}` is what makes the row mcp-injectable, which is the condition
    # for the node's grant to be resolved at all.
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat {mcp_file}")],
    )
    backend = _NoServers()
    monkeypatch.setattr(tool, "_resolve_node", lambda node: backend)

    out = await tool.execute(
        task_summary="run the graph under test",
        nodes=[
            {
                "id": "a",
                "subagent": "echo",
                "node_summary": "wants a server nobody configured",
                "prompt_template": "go",
                "mcps": ["ghost"],
            },
            {
                "id": "b",
                "subagent": "echo",
                "node_summary": "wants nothing at all",
                "prompt_template": "go too",
            },
        ],
        background=False,
    )

    text = getattr(out, "model_text", None) or str(out)
    assert "2 completed" in text, "one unresolved server must not cost the graph its run"
    assert backend.calls == ["a", "b"], "both nodes must have been dispatched"
    assert "ghost" in text and "not configured" in text, "the caller is still told what was lost"


async def test_run_subagent_dag_tool_end_to_end(tmp_path: Path) -> None:
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    assert tool.name == "run_subagent_dag"
    assert "echo" in tool.description

    out = await tool.execute(
        task_summary="run the graph under test",
        nodes=[
            {"id": "a", "subagent": "echo", "node_summary": "say hello world", "prompt_template": "hello world"},
            {
                "id": "b",
                "subagent": "echo",
                "node_summary": "echo node a's output",
                "prompt_template": "{{ a.output }}",
                "depends_on": ["a"],
            },
        ],
        background=False,
    )
    assert "2 completed" in out.model_text
    assert "hello world" in out.model_text  # a's output flowed to b (the sink) and back
    assert "(instance:" not in out.model_text  # stateless nodes must not include instance tags


async def test_the_tool_lets_a_continued_instance_replace_its_mcps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the tool, so the pre-dispatch validation runs for real.

    A stateful group used to be refused outright for naming ``mcps`` on any node
    after the one that opens the session, which left the per-session replacement
    this MR implements unreachable from the graph tool: a group could neither
    change nor clear its grant between turns. Both nodes must be accepted *and*
    each must resolve its own list -- the second one's ``[]`` is the release, not
    an omission that would leave the first node's servers attached.
    """
    resolved: list[tuple[str, Any]] = []

    class _Recording:
        kind = "cli"

        def resolve_mcp_grant(self, mcps: list[str] | None = None) -> McpGrant:
            resolved.append((self.node_id, mcps))
            return McpGrant()

        def __init__(self, node_id: str) -> None:
            self.node_id = node_id
            self.calls: list[dict] = []

        async def run(self, task: str, *, task_id: str, workspace, executor, **kwargs) -> str:
            self.calls.append({"task_id": task_id, "mcp_grant": kwargs.get("mcp_grant")})
            return f"OUT[{task_id}]"

    # `{mcp_file}` in both templates is what makes the row mcp-injectable, and
    # `resume_command` is what makes it stateful -- the two conditions a node
    # needs before its `mcps` is resolved at all.
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[
            ThirdPartyCliSubagentConfig(
                name="holder",
                command="cat {mcp_file} {agent_id}",
                resume_command="cat {mcp_file} {agent_id}",
            )
        ],
    )
    backends = {"open": _Recording("open"), "later": _Recording("later")}
    monkeypatch.setattr(tool, "_resolve_node", lambda node: backends[node.id])

    out = await tool.execute(
        task_summary="run the graph under test",
        nodes=[
            {
                "id": "open",
                "subagent": "holder",
                "node_summary": "open the session",
                "prompt_template": "first",
                "instance": "s",
                "mcps": ["db"],
            },
            {
                "id": "later",
                "subagent": "holder",
                "node_summary": "continue it with nothing attached",
                "prompt_template": "second",
                "instance": "s",
                "depends_on": ["open"],
                "mcps": [],
            },
        ],
        background=False,
    )

    # A refused graph comes back as the rendered refusal, not a run receipt, so
    # read the text either way rather than asserting through `.model_text` -- a
    # regression here must name the refusal, not raise AttributeError.
    text = getattr(out, "model_text", None) or str(out)
    assert "silently do nothing" not in text  # the old refusal
    assert "not implemented" not in text
    assert "2 completed" in text
    assert dict(resolved) == {"open": ["db"], "later": []}


async def test_run_subagent_dag_tool_dispatches_to_a_name_with_a_space(tmp_path: Path) -> None:
    # The whole user-facing path for a config the web UI already allows: the
    # roster advertises "General Audit", so a node naming it must reach it.
    # Previously the node was refused by the name charset before anything ran.
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="General Audit", command="cat")],
    )
    assert "General Audit" in tool.description

    out = await tool.execute(
        task_summary="run the graph under test",
        nodes=[
            {
                "id": "a",
                "subagent": "General Audit",
                "node_summary": "say hello world",
                "prompt_template": "hello world",
            }
        ],
        background=False,
    )
    assert "1 completed" in out.model_text
    assert "hello world" in out.model_text


class TestBackgroundRun:
    """The default shape: the call returns as soon as the graph is accepted and
    the outcome comes back as an announced turn, the way a spawn's does."""

    @pytest.fixture(autouse=True)
    async def _drain_background_runs(self):
        """Finish the background runs these tests start, before the loop closes.

        ``execute`` returns as soon as the graph is accepted, so every test here
        leaves a real task running ``cat`` as a sub-agent -- a process that never
        exits on its own. pytest-asyncio then closes the loop through
        ``_cancel_all_tasks``, which cancels every pending task and gathers them;
        cancellation has to travel down through ``_run_detached`` ->
        ``_run_group`` -> ``_run_node`` to a subprocess still being set up, and
        when it is caught mid-setup the gather never returns. Observed on a full
        run: three workers idle and one parked in that frame for over an hour.

        Draining here cancels while the loop is still healthy and, crucially,
        bounds the wait: a run that will not die becomes a named failure instead
        of a suite that hangs with no indication of which test did it.
        """
        async with draining_dag_runs():
            yield

    @staticmethod
    def _tool(tmp_path: Path, announce: Any = None) -> SubAgentDagTool:
        return SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            announce=announce,
        )

    async def test_the_fixture_drains_a_run_the_test_leaves_in_flight(self, tmp_path: Path) -> None:
        """The contract the class rests on, asserted from both sides.

        A test that only checked "nothing is in flight afterwards" would pass in
        the state where the run never started, which is the shape that hides a
        broken drain.
        """
        tool = self._tool(tmp_path)
        tool.set_context("web", "default", "web:drain")

        await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "echo", "node_summary": "say hello", "prompt_template": "hi"}],
        )
        assert tool._runs, "execute must leave a background run in flight for this to be worth draining"

        await drain_dag_runs([tool])

        assert not tool._runs

    async def test_a_run_that_ignores_cancellation_fails_named_instead_of_hanging_loop_close(self) -> None:
        """The timeout path must bound loop close, not just the drain's own wait.

        A task that suppresses CancelledError is chandler.zhang's repro shape:
        ``pytest.fail`` alone leaves it in the loop's registry, and
        ``Runner.close()`` then gathers it again without a timeout -- the
        failure message never surfaces. Reproduced with this exact shape before
        the fix: the drain timed out, raised, and ``asyncio.run`` hung in
        ``Runner.close()`` until a watchdog killed it.
        """

        async def _ignores_cancellation() -> None:
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    continue

        fake = SimpleNamespace(_cancels={}, _runs={})
        fake._runs["wedged"] = asyncio.create_task(_ignores_cancellation(), name="dag-wedged")
        await asyncio.sleep(0)  # let the task enter its first sleep, as a real run has

        with pytest.raises(pytest.fail.Exception, match="wedged"):
            await drain_dag_runs([fake], timeout=0.1, kill_grace=0.1)

        # The contract loop close depends on: a task the drain gave up on is no
        # longer anywhere Runner.close() will look.
        assert fake._runs["wedged"] not in asyncio.all_tasks()

    async def test_the_failure_path_reaps_a_subagent_child_left_running(self) -> None:
        """Killing the tree is part of the ladder, not theatre.

        A wedged run's sub-agent is an orphaned child of the worker; failing
        without killing it leaks a live process per failure. ``sleep`` stands in
        for it because it is the one child command every POSIX box has.
        """
        import subprocess as _sp

        child = _sp.Popen(["sleep", "300"])
        try:
            fake = SimpleNamespace(_cancels={}, _runs={})
            fake._runs["wedged"] = asyncio.create_task(self._noop_wedge(), name="dag-wedged")
            await asyncio.sleep(0)

            with pytest.raises(pytest.fail.Exception):
                await drain_dag_runs([fake], timeout=0.1, kill_grace=0.1)

            assert child.poll() is not None, "the wedged run's child must be dead before the failure is raised"
        finally:
            if child.poll() is None:
                child.kill()

    async def test_the_failure_path_reaps_the_whole_launcher_process_group(self) -> None:
        """Raven's launcher topology, not just a leaf process.

        CLI sub-agents are spawned with `start_new_session=True`, so the
        launcher doubles as its group's leader and Codex-style workers live
        inside that group: killing the launcher alone leaves them running. The
        leaf test above covers the non-leader branch; this one covers the group.
        """
        import subprocess as _sp

        launcher = _sp.Popen(["sh", "-c", "sleep 300 & wait"], start_new_session=True)
        pgid = launcher.pid
        try:
            fake = SimpleNamespace(_cancels={}, _runs={})
            fake._runs["wedged"] = asyncio.create_task(self._noop_wedge(), name="dag-wedged")
            await asyncio.sleep(0)

            with pytest.raises(pytest.fail.Exception):
                await drain_dag_runs([fake], timeout=0.1, kill_grace=0.1)

            # Reaping the launcher is what lets its killed worker reparent to
            # init and be reaped; until then the dead worker is a zombie that
            # still holds the group, so a bare signal-0 probe would read the
            # group as alive and the test would pass on the very bug it
            # exists to catch.
            launcher.wait(timeout=5)
            import time as _time

            deadline = _time.monotonic() + 5
            while _time.monotonic() < deadline:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("the launcher's process group outlived the drain")
        finally:
            try:
                os.killpg(pgid, 9)
            except ProcessLookupError:
                pass

    async def test_the_failure_message_names_the_run_that_landed_during_grace(self) -> None:
        """The diagnostic must come from the first timed-out set, not the second.

        The grace period exists so a task whose awaited child was killed can
        land. When it does, the post-grace set is empty -- and a diagnostic
        built from that set loses the very run the failure exists to name,
        leaving "... ignored cancellation for Xs: " with a blank after the
        colon.
        """

        async def _lands_when_its_child_dies() -> None:
            proc = await asyncio.create_subprocess_exec("sleep", "300")
            while True:
                try:
                    await proc.wait()
                    return
                except asyncio.CancelledError:
                    # Swallow the drain's cancel: only the child kill lets
                    # proc.wait() return, which is the grace path under test.
                    continue

        fake = SimpleNamespace(_cancels={}, _runs={})
        fake._runs["landing"] = asyncio.create_task(_lands_when_its_child_dies(), name="dag-landing")
        # One generous first timeout so the task has spawned its child before
        # the kill runs; the spawn itself takes milliseconds.
        await asyncio.sleep(0.05)

        with pytest.raises(pytest.fail.Exception, match="dag-landing"):
            await drain_dag_runs([fake], timeout=1.0, kill_grace=2.0)

    async def _noop_wedge(self) -> None:
        """A task that ignores cancellation, shared by the failure-path tests."""
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue

    async def test_the_call_returns_before_the_graph_does(self, tmp_path: Path) -> None:
        announces = _Announces()
        tool = self._tool(tmp_path, announces)
        tool.set_context("web", "default", "web:sess1")

        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[
                {"id": "a", "subagent": "echo", "node_summary": "say hello world", "prompt_template": "hello world"},
                {
                    "id": "b",
                    "subagent": "echo",
                    "node_summary": "echo node a's output",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ],
        )

        assert "started in the background" in out.model_text
        assert "2 nodes" in out.model_text
        # This text is the only advertisement dag_status and cancel_dag get, so it
        # carries the route as well as the names.
        assert "dag_status" in out.model_text
        assert "cancel_dag" in out.model_text
        assert "tool_call" in out.model_text, "the controls are unnameable without the route"
        # The outcome cannot be in the result -- nothing has run yet.
        assert "completed" not in out.model_text

        run_id, summary, origin = (await announces.wait())[0]
        assert run_id in out.model_text, "the announce must name the run the call reported"
        assert "2 completed" in summary
        assert "hello world" in summary  # a's output flowed to b and into the announce
        assert origin == {"channel": "web", "chat_id": "default", "session_key": "web:sess1"}

    async def test_the_acceptance_text_advertises_the_controls_only_when_reachable(self, tmp_path: Path) -> None:
        reachable = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            control_reachable=lambda: True,
        )
        muted = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            control_reachable=lambda: False,
        )
        node = {"id": "a", "subagent": "echo", "node_summary": "say hello", "prompt_template": "hi"}

        reachable.set_context("web", "default", "web:sess1")
        out = await reachable.execute(task_summary="run the graph under test", nodes=[node])
        assert "dag_status" in out.model_text
        assert "cancel_dag" in out.model_text
        assert "tool_call" in out.model_text, "the controls are unnameable without the route"

        muted.set_context("web", "default", "web:sess2")
        out = await muted.execute(task_summary="run the graph under test", nodes=[node])
        assert "started in the background" in out.model_text
        assert "dag_status" not in out.model_text
        assert "cancel_dag" not in out.model_text

    async def test_the_acceptance_text_mutes_the_hint_when_the_predicate_raises(self, tmp_path: Path) -> None:
        # The predicate runs after the background task exists, so a failing
        # one must mute the hint, never turn the acceptance into an error.
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            control_reachable=lambda: (_ for _ in ()).throw(AttributeError("no controller")),
        )
        tool.set_context("web", "default", "web:sess3")

        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "echo", "node_summary": "say hello", "prompt_template": "hi"}],
        )

        assert "started in the background" in out.model_text
        assert "dag_status" not in out.model_text
        assert "cancel_dag" not in out.model_text

    async def test_two_overlapping_runs_each_report_to_their_own_turn(self, tmp_path: Path) -> None:
        """Backgrounding makes concurrent runs on one shared tool the normal case.

        The reply address and tool row belong to the call, not to the tool, so
        each run has to carry its own -- holding the latest on the instance
        would send the first run's result and graph to the second one's chat.
        """
        announces = _Announces()
        tool = self._tool(tmp_path, announces)
        events: list[tuple[str, str, str | None]] = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def sink(conversation, name, payload):
            events.append((payload["run_id"], conversation, payload.get("tool_call_id")))
            if name == "dag_run_started":
                # Hold each run at its first event so both are genuinely in
                # flight together, rather than racing to finish first.
                started.set()
                await release.wait()

        tool.set_progress_sink(sink)

        tool.set_context("web", "one", "web:sess1")
        tool.set_tool_call_id("call-1")
        first = await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"}],
        )
        await asyncio.wait_for(started.wait(), 10)

        started.clear()
        tool.set_context("web", "two", "web:sess2")
        tool.set_tool_call_id("call-2")
        second = await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"}],
        )
        await asyncio.wait_for(started.wait(), 10)

        release.set()
        calls = await announces.wait(count=2)

        run_one, run_two = str(first.model_text).split()[2], str(second.model_text).split()[2]
        addressed = {run_id: origin["session_key"] for run_id, _, origin in calls}
        assert addressed == {run_one: "web:sess1", run_two: "web:sess2"}
        assert {(conv, call_id) for run_id, conv, call_id in events if run_id == run_one} == {("web:sess1", "call-1")}
        assert {(conv, call_id) for run_id, conv, call_id in events if run_id == run_two} == {("web:sess2", "call-2")}

    async def test_a_rejected_graph_is_refused_in_the_callers_own_turn(self, tmp_path: Path) -> None:
        """Backgrounding must not downgrade a refusal into an announcement a turn
        later -- including the roster check, which the runner only reaches once
        the call has already returned."""
        announces = _Announces()
        tool = self._tool(tmp_path, announces)

        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "nope", "node_summary": "node a", "prompt_template": "hi"}],
        )

        assert out.startswith("Error: invalid DAG")
        assert announces.calls == []
        assert not (tmp_path / ".ravenx_dag").exists()

    def test_the_flag_is_declared_and_defaults_to_true(self, tmp_path: Path) -> None:
        """Backgrounding is opt-out, so the schema has to carry the flag and say
        which way it points -- a model reading the description alone would
        otherwise assume the old blocking behaviour it was trained on."""
        schema = self._tool(tmp_path).parameters
        assert "background" not in schema["required"]
        described = schema["properties"]["background"]
        assert described["type"] == "boolean"
        assert "Default true" in described["description"]

    @pytest.mark.parametrize("stop", ["by_session", "all", "by_id"])
    async def test_a_background_run_is_reachable_by_stop_and_shutdown(self, tmp_path: Path, stop: str) -> None:
        """A backgrounded run dispatches the same detached CLI children a spawn
        does -- its own process group, no timeout, unreachable by the gateway's
        Ctrl-C. If `/stop` and the shutdown sweep cannot find it, `cancel_all`'s
        whole reason for existing is defeated and those children outlive the
        gateway, still writing to the workspace.

        `by_id` is the overlay's kill button, keyed on the id a run reports. It
        reaches this run only because the adoption above puts it in the index
        that route reads -- neither half was written with the other in view.
        """
        from raven.agent.subagent.manager import SubagentManager

        class _Provider:
            def get_default_model(self) -> str:
                return "m"

        mgr = SubagentManager(provider=_Provider(), workspace=tmp_path)
        announces = _Announces()
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            announce=announces,
            gate=mgr.dispatch_gate,
            adopt=mgr.adopt_background_run,
        )
        tool.set_context("web", "default", "web:sess1")
        started = asyncio.Event()
        release = asyncio.Event()

        async def sink(conversation, name, payload):
            if name == "dag_run_started":
                started.set()
                await release.wait()

        tool.set_progress_sink(sink)
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"}],
        )
        run_id = str(out.model_text).split()[2]
        await asyncio.wait_for(started.wait(), 10)

        if stop == "by_session":
            stopped = await mgr.cancel_by_session("web:sess1")
        elif stop == "all":
            stopped = await mgr.cancel_all()
        else:
            stopped = int(await mgr.cancel_by_id(run_id))

        assert stopped == 1, "the stop path did not reach the background run"
        assert tool.active_run_ids() == []
        # A run torn down under the user's feet has nothing to report back.
        assert announces.calls == []

    async def test_a_stopped_run_announces_nothing(self, tmp_path: Path) -> None:
        """`run_dag` returns normally on a stop, with everything skipped. Turning
        that into an announcement would spend a turn narrating what the user just
        cancelled -- a cancelled spawn stays silent for the same reason."""
        announces = _Announces()
        tool = self._tool(tmp_path, announces)
        tool.set_context("web", "default", "web:sess1")
        started = asyncio.Event()
        release = asyncio.Event()

        async def sink(conversation, name, payload):
            if name == "dag_run_started":
                started.set()
                await release.wait()

        tool.set_progress_sink(sink)
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"}],
        )
        run_id = str(out.model_text).split()[2]
        await asyncio.wait_for(started.wait(), 10)

        assert tool.request_cancel(run_id) is True
        release.set()
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run_id not in tool.active_run_ids():
                break

        await asyncio.sleep(0.05)
        assert announces.calls == []

    async def test_a_run_is_charged_to_the_shared_dispatch_budget(self, tmp_path: Path) -> None:
        """A background run returns instantly and announces itself back as a new
        turn, which can submit more runs -- the loop the dispatch budget exists
        to bound. The concurrency gate does not: each dispatch frees its slot."""
        charged: list[str | None] = []

        def charge(session_key: str | None) -> str | None:
            charged.append(session_key)
            return "Error: budget spent." if len(charged) > 2 else None

        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            charge=charge,
        )
        tool.set_context("web", "default", "web:sess1")

        # A fresh id per submission: ids are unique per conversation, so reusing
        # one would refuse the graph before it ever reached the budget, which is
        # what this test is about.
        def node(nid: str) -> dict:
            return {
                "id": nid,
                "subagent": "echo",
                "node_summary": "draw on the shared dispatch budget",
                "prompt_template": "hi",
            }

        first = await tool.execute(task_summary="run the graph under test", nodes=[node("a")])
        second = await tool.execute(task_summary="run the graph under test", nodes=[node("b")], background=False)
        third = await tool.execute(task_summary="run the graph under test", nodes=[node("c")])

        assert "started in the background" in str(first)
        assert "1 completed" in str(second), "a foreground run draws on the same budget"
        assert third == "Error: budget spent."
        assert charged == ["web:sess1"] * 3

        # A graph that never passes validation must not spend budget either.
        await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "d", "subagent": "nope", "node_summary": "node d", "prompt_template": "hi"}],
        )
        assert len(charged) == 3

    async def test_a_collapsed_run_closes_the_graph_it_drew(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A drawn graph settles on a terminal event, and only on one.

        A collapse mid-run leaves the nodes drawn and running. Blocking, the
        error landed in the same turn as the tool result, next to the stalled
        graph; backgrounded, the row already said "started" and the error goes
        only to the model -- so without a terminal event the graph reads as
        still running for as long as the tab stays open.
        """
        import raven.agent.subagent.dag_tool as tool_mod

        events: list[tuple[str, dict]] = []

        async def _publish(name: str, value: dict) -> None:
            events.append((name, value))

        async def _crash_after_drawing(spec: Any, *, progress_publisher: Any, run_id: str, **kw: Any) -> None:
            await progress_publisher("dag_run_started", {"run_id": run_id, "nodes": [{"id": "a"}]})
            raise RuntimeError("store write failed")

        monkeypatch.setattr(tool_mod, "run_dag", _crash_after_drawing)
        announces = _Announces()
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            progress_publisher=_publish,
            announce=announces,
        )
        tool.set_context("web", "default", "web:sess1")

        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"}],
        )
        run_id = str(out.model_text).split()[2]
        await announces.wait()

        names = [name for name, _ in events]
        assert names == ["dag_run_started", "dag_run_completed"], names
        manifest = events[-1][1]["manifest"]
        assert events[-1][1]["run_id"] == run_id
        # The projection reads `manifest` unconditionally; it is what marks the
        # run finished, and the reason belongs with it.
        assert "store write failed" in manifest["error"]

    async def test_a_stopped_run_closes_the_graph_the_way_a_collapsed_one_does(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both stop routes have to settle the drawing, not just one.

        `dag.cancel` sets the run's event and lets the runner return, so the
        graph closes on a real manifest. `/stop` and the shutdown sweep instead
        cancel the task -- a route this branch opened by adopting the run into
        the manager's index -- and `CancelledError` is not an `Exception`, so
        the collapse path above does not see it.
        """
        import raven.agent.subagent.dag_tool as tool_mod

        events: list[tuple[str, dict]] = []
        drawn = asyncio.Event()

        async def _publish(name: str, value: dict) -> None:
            events.append((name, value))

        async def _draw_then_hang(spec: Any, *, progress_publisher: Any, run_id: str, **kw: Any) -> None:
            await progress_publisher("dag_run_started", {"run_id": run_id, "nodes": [{"id": "a"}]})
            drawn.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(tool_mod, "run_dag", _draw_then_hang)
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            progress_publisher=_publish,
        )
        tool.set_context("web", "default", "web:sess1")

        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"}],
        )
        run_id = str(out.model_text).split()[2]
        await drawn.wait()

        # Exactly what `SubagentManager.cancel_by_session` does to the task it
        # was handed by `adopt_background_run`.
        task = tool._runs[run_id]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        names = [name for name, _ in events]
        assert names == ["dag_run_started", "dag_run_completed"], names
        assert events[-1][1]["manifest"]["stopped"] is True

    async def test_a_collapsed_run_still_names_itself(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every shape the summary takes has to identify its own run.

        A finished run is named by its first line and its run dir, and a failed
        node rides inside one. A run that collapses outright had only the bare
        exception -- which reaches the agent a turn later as a message of its
        own, unattributable to any of the graphs it has in flight.
        """
        import raven.agent.subagent.dag_tool as tool_mod

        async def _boom(*a, **kw):
            raise RuntimeError("backend exploded")

        monkeypatch.setattr(tool_mod, "run_dag", _boom)
        announces = _Announces()
        tool = self._tool(tmp_path, announces)
        tool.set_context("web", "default", "web:sess1")
        node = {"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"}

        out = await tool.execute(task_summary="run the graph under test", nodes=[dict(node)])
        run_id = str(out.model_text).split()[2]
        _, summary, _ = (await announces.wait())[0]

        assert run_id in summary
        assert "backend exploded" in summary
        # The announce is a verbatim copy, so the foreground result is the same
        # text -- the id belongs to the summary, not to any announce framing.
        foreground = await tool.execute(task_summary="run the graph under test", nodes=[dict(node)], background=False)
        assert str(foreground).startswith("Error running DAG ")
        assert "backend exploded" in str(foreground)

    async def test_a_run_with_no_announcer_still_completes(self, tmp_path: Path) -> None:
        """Hosts that never wire one (the CLI before its scheduler exists, tests)
        must not turn a finished graph into an unretrievable crash."""
        tool = self._tool(tmp_path, None)
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"}],
        )
        run_id = str(out.model_text).split()[2]

        for _ in range(100):
            await asyncio.sleep(0.05)
            if run_id not in tool.active_run_ids():
                break
        assert run_id not in tool.active_run_ids()
        run = await tool.read_run(run_id)
        assert run["summary"]["completed"] == 1

    async def test_a_background_run_records_under_the_session_that_started_it(self, tmp_path: Path) -> None:
        """The history root is the submitting turn's, not whatever is current
        when the graph finishes -- a reader between turns has only the session
        key to look under, and a run that wrote elsewhere is unreachable."""
        announces = _Announces()
        tool = self._tool(tmp_path, announces)
        tool.set_context("web", "default", "web:sess1")

        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"}],
        )
        run_id = str(out.model_text).split()[2]
        await announces.wait()

        run = await tool.read_run(run_id, "web:sess1")
        assert run["summary"]["completed"] == 1


class TestSubagentRoster:
    """A node picks its `subagent` from this listing, so it needs at least what
    `spawn` shows — including which agents are stateful and which can read local
    files, since those are what the node-level `instance` field and the choice
    between path and content placeholders key off."""

    def test_capability_tags_are_rendered_for_every_agent(self, tmp_path: Path) -> None:
        """Both tags always render, positive or negative: the model has to
        confirm a capability before relying on it, and a missing tag reads the
        same as a roster that never mentioned it."""
        desc = SubAgentDagTool(
            workspace=tmp_path,
            agents=[
                ThirdPartyCliSubagentConfig(
                    name="Coder",
                    command="claude -p {prompt} --session-id {agent_id}",
                    resume_command="claude -p {prompt} --resume {agent_id}",
                ),
                ThirdPartyCliSubagentConfig(name="Boxed", command="cat", reads_local_files=False),
            ],
        ).description

        assert "Coder [stateful, local-files, no-progress]" in desc
        assert "Boxed [stateless, no-local-files, no-progress]" in desc

    def test_the_tags_are_explained_once_in_the_field_that_they_gate(self, tmp_path: Path) -> None:
        """Tags the model cannot interpret are just noise, and the pre-check would
        then reject graphs over a rule it was never told. The field descriptions
        are that explanation -- they ride in the same payload as the roster (both
        `to_schema()` and a tool_search hit carry description + parameters), so a
        second copy in the tool description is prose the model pays for twice."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        )
        node = tool.parameters["properties"]["nodes"]["items"]["properties"]

        assert "[stateful]" in node["instance"]["description"]
        assert "[no-local-files]" in node["prompt_template"]["description"]
        # The roster still shows each agent's tags; the rules are not restated.
        assert "[stateless, local-files, no-progress]" in tool.description
        assert "[no-local-files]" not in tool.description

    def test_descriptions_are_surfaced_not_just_names(self, tmp_path: Path) -> None:
        desc = SubAgentDagTool(
            workspace=tmp_path,
            agents=[
                ThirdPartyCliSubagentConfig(
                    name="Coder",
                    description="Handles coding tasks.",
                    command="cat",
                ),
                ThirdPartyCliSubagentConfig(name="Bare", command="cat"),
            ],
        ).description

        assert "Coder [stateless, local-files, no-progress] (Handles coding tasks.)" in desc
        assert "Bare [stateless, local-files, no-progress]" in desc
        assert "Bare [stateless, local-files] (" not in desc  # no description -> no empty parens

    def test_roster_matches_spawns_rendering(self, tmp_path: Path) -> None:
        """The two tools must describe the same agent the same way."""
        cfgs = [
            ThirdPartyCliSubagentConfig(name="Coder", description="Codes things.", command="cat"),
            ThirdPartyCliSubagentConfig(name="Writer", description="Writes things.", command="cat"),
        ]
        expected = format_agent_listing([third_party_agent_meta(c) for c in cfgs])
        desc = SubAgentDagTool(workspace=tmp_path, agents=cfgs).description

        assert expected in desc

    def test_a_backend_that_fails_to_build_is_not_advertised(self, tmp_path: Path) -> None:
        """The roster is captured in the same loop as the executors, so it can
        never list an agent a node would then fail to dispatch to."""

        class _Bad:
            name = "Broken"
            kind = "nope"
            description = "should never be advertised"

        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[_Bad(), ThirdPartyCliSubagentConfig(name="Good", command="cat")],
        )

        assert "Broken" not in tool.description
        assert "Good" in tool.description
        assert "Good" in tool.registry.names()

    def test_no_configured_agents_still_advertises_the_built_in_ones(self, tmp_path: Path) -> None:
        """The roster is never empty now: the package's built-in rows are on the
        table whether or not config names any external agent, which is what lets a
        default install orchestrate a graph at all."""
        desc = SubAgentDagTool(workspace=tmp_path, agents=[]).description
        assert "(none configured)" not in desc
        assert GENERIC_AGENT in desc


class TestSubagentEnum:
    """`run_dag` rejects the whole graph over one unknown name, so a single
    typo costs the entire call. Constrain it in the schema instead."""

    def _subagent_field(self, tool: SubAgentDagTool) -> dict:
        return tool.parameters["properties"]["nodes"]["items"]["properties"]["subagent"]

    def test_enum_lists_the_configured_roster(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[
                ThirdPartyCliSubagentConfig(name="Writer", command="cat"),
                ThirdPartyCliSubagentConfig(name="Coder", command="cat"),
            ],
        )
        names = self._subagent_field(tool)["enum"]
        assert "Coder" in names and "Writer" in names

    def test_clearing_external_config_leaves_the_built_in_rows_in_the_enum(self, tmp_path: Path) -> None:
        """A hot-apply that clears every external agent used to leave the enum
        empty, which some providers reject as an unsatisfiable tool schema while
        the field stays required. It cannot happen now -- the built-in rows are
        seeds, not config -- and the enum keeps naming them."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="Coder", command="cat")],
        )
        tool.set_agents([])
        names = self._subagent_field(tool)["enum"]
        assert "Coder" not in names
        assert GENERIC_AGENT in names

    def test_schema_is_rebuilt_and_never_mutates_the_module_constant(self, tmp_path: Path) -> None:
        """The constant is shared by every instance; annotating it in place
        would leak one tool's roster into another's schema."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="Coder", command="cat")],
        )
        tool.parameters  # noqa: B018 - the property is what would mutate

        assert "enum" not in _NODE_SCHEMA["properties"]["subagent"]
        fresh = SubAgentDagTool(workspace=tmp_path, agents=[])
        assert "Coder" not in self._subagent_field(fresh)["enum"]

    def test_enum_follows_a_hot_applied_roster(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(workspace=tmp_path, agents=[])
        assert "Late" not in self._subagent_field(tool)["enum"]
        tool.set_agents([ThirdPartyCliSubagentConfig(name="Late", command="cat")])
        assert "Late" in self._subagent_field(tool)["enum"]


class TestGuideSkillPointer:
    """The node-wiring rules live in the skill, not in this description, so the
    description has to send the agent there before it guesses a graph shape."""

    def test_description_requires_reading_the_guide_first(self, tmp_path: Path) -> None:
        desc = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        ).description

        assert "REQUIRED FIRST STEP" in desc
        assert f'read_skill("{GUIDE_SKILL_ID}")' in desc
        # Still carries the runtime-only facts the skill body cannot know.
        assert "echo" in desc

    def test_pointer_is_dropped_when_the_guide_is_not_installed(self, tmp_path: Path) -> None:
        """Better no instruction than one that sends the agent after a skill
        that cannot resolve — that costs a wasted turn on every DAG call."""
        desc = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            guide_skill_id=None,
        ).description

        assert "REQUIRED FIRST STEP" not in desc
        assert "read_skill" not in desc
        assert "echo" in desc

    def test_loop_drops_the_pointer_only_on_a_definite_miss(self) -> None:
        """A registry that says "absent" suppresses the pointer; one that is
        missing or throwing keeps it — the shipped skill is there by default,
        so losing the instruction is the worse of the two failures."""
        from raven.agent.loop.main import AgentLoop

        class _Reg:
            def __init__(self, found):
                self._found = found

            def get(self, name, source=None):
                if self._found is Ellipsis:
                    raise RuntimeError("registry down")
                return object() if self._found else None

        def _resolve(registry) -> str | None:
            loop = object.__new__(AgentLoop)
            loop.context = type("C", (), {"skills": type("S", (), {"registry": registry})()})()
            return AgentLoop._dag_guide_skill_id(loop)

        assert _resolve(_Reg(True)) == GUIDE_SKILL_ID
        assert _resolve(_Reg(False)) is None
        assert _resolve(_Reg(Ellipsis)) == GUIDE_SKILL_ID  # raising
        assert _resolve(None) == GUIDE_SKILL_ID  # no registry wired

    def test_guide_id_resolves_against_the_shipped_registry(self, tmp_path: Path) -> None:
        """Pins the two halves together: the id the tool prints must be the id
        ``read_skill`` can actually resolve, or the instruction is a dead end."""
        from raven.agent.tools.skill_hub import lookup_on_disk, split_qualified_id
        from raven.memory_engine.skill_forge import LocalSkillCatalog

        registry = LocalSkillCatalog(tmp_path, start_watcher=False).registry
        source, native = split_qualified_id(GUIDE_SKILL_ID)
        assert lookup_on_disk(registry, source, native) is not None


async def test_run_subagent_dag_tool_unknown_subagent(tmp_path: Path) -> None:
    tool = SubAgentDagTool(workspace=tmp_path, agents=[])
    out = await tool.execute(
        task_summary="run the graph under test",
        nodes=[{"id": "a", "subagent": "nope", "node_summary": "node a", "prompt_template": "x"}],
    )
    assert out.startswith("Error")


async def test_run_subagent_dag_tool_fans_progress_to_sink(tmp_path: Path) -> None:
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    # The loop delivers the turn's conversation via set_context.
    tool.set_context("web", "default", "web:sess1")
    events: list[tuple[str, str, dict]] = []

    async def sink(conversation, name, payload):
        events.append((conversation, name, payload))

    tool.set_progress_sink(sink)
    await tool.execute(
        task_summary="run the graph under test",
        nodes=[
            {"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"},
            {
                "id": "b",
                "subagent": "echo",
                "node_summary": "node b, downstream of a",
                "prompt_template": "{{ a.output }}",
                "depends_on": ["a"],
            },
        ],
        background=False,
    )

    assert all(conv == "web:sess1" for conv, _, _ in events)
    names = [n for _, n, _ in events]
    assert names[0] == "dag_run_started"
    assert names[-1] == "dag_run_completed"
    assert any(
        n == "dag_node_updated" and p.get("node") == "b" and p.get("status") == "completed" for _, n, p in events
    )
    # the completed event carries the authoritative manifest
    completed = [p for _, n, p in events if n == "dag_run_completed"][0]
    assert "manifest" in completed and "files" in completed["manifest"]


class TestCallAndResultLabels:
    """What a transcript shows for a DAG call when it cannot show the graph.

    The row's label is built before the run, from the raw arguments; the result
    line is what survives the loop's 200-char preview clamp. Neither may
    degrade to a node id picked out of the arguments blob at random.
    """

    @staticmethod
    def _tool(tmp_path: Path) -> SubAgentDagTool:
        return SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        )

    def test_display_call_counts_nodes_and_names_the_first_few(self, tmp_path: Path) -> None:
        label = self._tool(tmp_path).display_call(
            {
                "nodes": [
                    {"id": "fetch", "subagent": "echo", "prompt_template": "x"},
                    {"id": "parse", "subagent": "echo", "prompt_template": "x"},
                    {"id": "report", "subagent": "echo", "prompt_template": "x"},
                ]
            }
        )
        assert label == "3 nodes: fetch, parse, report"

    def test_display_call_elides_a_long_roster(self, tmp_path: Path) -> None:
        label = self._tool(tmp_path).display_call(
            {"nodes": [{"id": f"n{i}", "subagent": "echo", "prompt_template": "x"} for i in range(6)]}
        )
        assert label == "6 nodes: n0, n1, n2 (+3 more)"

    def test_display_call_declines_malformed_arguments(self, tmp_path: Path) -> None:
        """display_call runs before validation, so it must never raise -- None
        hands the row back to the UI's generic argument preview."""
        tool = self._tool(tmp_path)
        assert tool.display_call({}) is None
        assert tool.display_call({"nodes": "not-a-list"}) is None
        assert tool.display_call({"nodes": []}) is None

    async def test_result_preview_names_the_run_and_its_tally(self, tmp_path: Path) -> None:
        out = await self._tool(tmp_path).execute(
            task_summary="run the graph under test",
            nodes=[
                {"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"},
                {
                    "id": "b",
                    "subagent": "echo",
                    "node_summary": "node b, downstream of a",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ],
            background=False,
        )
        preview = getattr(out, "display_text", None)
        assert preview is not None, "the result must carry a display string for the transcript"
        # The loop clamps the preview at 200 chars. The tally has to be inside
        # that window whatever the workspace path length is, so it leads.
        assert "2/2 completed" in preview[:200]
        assert "mas_dag" in preview

    async def test_result_preview_names_the_failed_nodes(self, tmp_path: Path) -> None:
        """A failure the user cannot see the id of is a failure they cannot act
        on -- and the full per-node list is exactly what the clamp cuts off."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[
                ThirdPartyCliSubagentConfig(name="echo", command="cat"),
                ThirdPartyCliSubagentConfig(name="broken", command="false"),
            ],
        )
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[
                {"id": "boom", "subagent": "broken", "node_summary": "the node that fails", "prompt_template": "x"},
                {
                    "id": "after",
                    "subagent": "echo",
                    "node_summary": "read boom's output after it fails",
                    "prompt_template": "{{ boom.output }}",
                    "depends_on": ["boom"],
                },
            ],
            background=False,
        )
        assert out.display_text is not None
        assert "1 failed (boom)" in out.display_text
        assert "1 skipped" in out.display_text


async def test_run_subagent_dag_tool_stamps_tool_call_id_on_every_event(tmp_path: Path) -> None:
    """A consumer that renders the graph under the tool row it belongs to needs
    to know which call each event is for -- keying on run_id alone forces it to
    guess when a turn issues more than one DAG call."""
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    tool.set_context("tui", "default", "tui:sess1")
    tool.set_tool_call_id("call-42")
    events: list[tuple[str, dict]] = []

    async def sink(conversation, name, payload):
        events.append((name, payload))

    tool.set_progress_sink(sink)
    await tool.execute(
        task_summary="run the graph under test",
        nodes=[{"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"}],
        background=False,
    )

    assert events, "expected at least one progress event"
    assert all(p.get("tool_call_id") == "call-42" for _, p in events)


async def test_run_subagent_dag_tool_omits_tool_call_id_when_host_sets_none(tmp_path: Path) -> None:
    """Hosts that never call set_tool_call_id (IM channels, tests) must not get
    a null key smuggled into the payload -- the web consumer folds the payload
    verbatim into its projection."""
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    tool.set_context("im", "default", "im:sess1")
    events: list[dict] = []

    async def sink(conversation, name, payload):
        events.append(payload)

    tool.set_progress_sink(sink)
    await tool.execute(
        task_summary="run the graph under test",
        nodes=[{"id": "a", "subagent": "echo", "node_summary": "node a", "prompt_template": "hi"}],
        background=False,
    )

    assert events
    assert all("tool_call_id" not in p for p in events)


class TestCapabilityGate:
    """The roster's tags are a promise about what a node may ask of an agent.
    Enforced here, before dispatch: both mistakes otherwise *run* and come back
    with output that silently pretends the capability was there."""

    @staticmethod
    def _tool(tmp_path: Path) -> SubAgentDagTool:
        return SubAgentDagTool(
            workspace=tmp_path,
            agents=[
                ThirdPartyCliSubagentConfig(name="stateless_local", command="cat"),
                ThirdPartyCliSubagentConfig(name="boxed", command="cat", reads_local_files=False),
                ThirdPartyCliSubagentConfig(
                    name="resumable",
                    command="cat {agent_id}",
                    resume_command="cat {agent_id}",
                ),
            ],
        )

    async def test_reused_handle_on_a_stateless_agent_runs_no_node(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[
                {
                    "id": "draft",
                    "subagent": "stateless_local",
                    "node_summary": "write the draft",
                    "prompt_template": "write",
                    "instance": "author",
                },
                {
                    "id": "revise",
                    "subagent": "stateless_local",
                    "node_summary": "revise the draft",
                    "prompt_template": "revise",
                    "instance": "author",
                },
            ],
        )

        assert out.startswith("Error: invalid DAG")
        assert "No sub-agent was run." in out
        assert f'read_skill("{GUIDE_SKILL_ID}")' in out
        # Nothing was dispatched, so no run directory exists to read back.
        assert not (tmp_path / "sessions").exists()

    async def test_path_placeholder_to_a_boxed_agent_runs_no_node(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[
                {
                    "id": "a",
                    "subagent": "stateless_local",
                    "node_summary": "produce the upstream file",
                    "prompt_template": "upstream",
                },
                {
                    "id": "b",
                    "subagent": "boxed",
                    "node_summary": "read a's output file path",
                    "prompt_template": "read {{ a.output_path }}",
                    "depends_on": ["a"],
                },
            ],
        )

        assert out.startswith("Error: invalid DAG")
        assert "{{ a.output }}" in out  # the fix, not just the complaint
        assert "No sub-agent was run." in out
        assert not (tmp_path / "sessions").exists()

    async def test_a_graph_that_respects_the_tags_still_runs(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[
                {
                    "id": "a",
                    "subagent": "stateless_local",
                    "node_summary": "say hello world",
                    "prompt_template": "hello world",
                },
                {
                    "id": "b",
                    "subagent": "boxed",
                    "node_summary": "echo node a's output",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ],
            background=False,
        )

        assert "2 completed" in out.model_text
        assert "hello world" in out.model_text

    async def test_reused_handle_is_allowed_on_a_stateful_agent(self, tmp_path: Path) -> None:
        """The gate must not reject the shape the guide's own pipeline example
        teaches — it only fires when the agent cannot honour the handle."""
        tool = self._tool(tmp_path)
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[
                {
                    "id": "draft",
                    "subagent": "resumable",
                    "node_summary": "write the draft",
                    "prompt_template": "write",
                    "instance": "author",
                },
                {
                    "id": "revise",
                    "subagent": "resumable",
                    "node_summary": "revise using the draft's output",
                    "prompt_template": "{{ draft.output }}",
                    "depends_on": ["draft"],
                    "instance": "author",
                },
            ],
            background=False,
        )

        # A rejection is a plain error string; a graph that ran comes back as a
        # ToolResult carrying the transcript label alongside the model text.
        assert isinstance(out, ToolResult), f"gate rejected the graph: {out}"

    async def test_hot_applied_config_moves_the_gate_with_the_roster(self, tmp_path: Path) -> None:
        """The capability map is rebuilt in the same pass as the roster, so a
        webui config change can never leave the two describing different agents."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="agent", command="cat")],
        )
        tool.set_agents([ThirdPartyCliSubagentConfig(name="agent", command="cat", reads_local_files=False)])

        assert "agent [stateless, no-local-files, no-progress]" in tool.description
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[
                {
                    "id": "a",
                    "subagent": "agent",
                    "node_summary": "produce the upstream file",
                    "prompt_template": "upstream",
                },
                {
                    "id": "b",
                    "subagent": "agent",
                    "node_summary": "read a's output file path",
                    "prompt_template": "{{ a.output_path }}",
                    "depends_on": ["a"],
                },
            ],
        )
        assert out.startswith("Error: invalid DAG")


class TestValidationErrorGuidesRetry:
    """A rejected graph is the one moment we know the model got the shape wrong,
    and the description it read once is long past. The result has to carry the
    way back itself."""

    async def test_structural_failure_points_at_the_guide(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        )
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[
                {
                    "id": "a",
                    "subagent": "echo",
                    "node_summary": "read b's output",
                    "prompt_template": "{{ b.output }}",
                    "depends_on": ["b"],
                },
                {
                    "id": "b",
                    "subagent": "echo",
                    "node_summary": "read a's output",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ],
        )

        assert "cycle" in out
        assert f'read_skill("{GUIDE_SKILL_ID}")' in out
        assert "retry" in out
        assert not (tmp_path / "sessions").exists()

    async def test_no_guide_installed_means_no_dead_end_advice(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            guide_skill_id=None,
        )
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[{"id": "a", "subagent": "nope", "node_summary": "node a", "prompt_template": "hi"}],
        )

        assert out.startswith("Error: invalid DAG")
        assert "read_skill" not in out

    async def test_one_sentence_boundary_between_detail_and_advice(self, tmp_path: Path) -> None:
        """Validation messages are written without a trailing period, so the
        renderer supplies one — else the detail runs into the next sentence."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        )
        out = await tool.execute(
            task_summary="run the graph under test",
            nodes=[
                {"id": "a", "subagent": "echo", "prompt_template": "hi"},
                {"id": "a", "subagent": "echo", "prompt_template": "hi"},
            ],
        )

        assert "duplicate node ids: ['a']. No sub-agent was run." in out


# --- references across runs in one conversation ---------------------------


async def test_a_later_run_reads_an_earlier_runs_output() -> None:
    # The whole point of the @runs/ prefix: run dirs live under the session's
    # metadata directory, which no relative path from the workdir reaches, so
    # without it a chain of graphs in one conversation can pass nothing along.
    backend = _InMemBackend()
    first = await run_dag(
        parse_dag_spec(
            {
                "task_summary": "run the graph under test",
                "nodes": [
                    {"id": "plan", "subagent": "x", "node_summary": "draft the plan", "prompt_template": "draft it"}
                ],
            }
        ),
        resolve=_by_name({"x": _FakeExec()}),
        backend=backend,
        workdir="/w",
        run_root="/hist/mas_dag",
    )

    second = await run_dag(
        parse_dag_spec(
            {
                "task_summary": "run the graph under test",
                "nodes": [
                    {
                        "id": "build",
                        "subagent": "x",
                        "node_summary": "build using an earlier run's plan",
                        "prompt_template": f"earlier: {{{{ ref:@runs/{first.run_id}/plan.out.md }}}}",
                    }
                ],
            }
        ),
        resolve=_by_name({"x": _FakeExec()}),
        backend=backend,
        workdir="/w",
        run_root="/hist/mas_dag",
    )

    assert second.summary["completed"] == 1
    prompt = backend.files[f"/hist/mas_dag/{second.run_id}/build.prompt.md"].decode()
    assert "OUT[plan]:draft it" in prompt


async def test_a_reference_under_the_history_root_needs_the_grant() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {
                    "id": "n",
                    "subagent": "x",
                    "node_summary": "read a file under the history root",
                    "prompt_template": "{{ ref:/hist/spawn/call1/result.md }}",
                }
            ],
        }
    )
    backend = _InMemBackend()
    backend.files["/hist/spawn/call1/result.md"] = b"A SPAWN RECORD"

    with pytest.raises(DagValidationError, match="outside"):
        await run_dag(
            spec, resolve=_by_name({"x": _FakeExec()}), backend=backend, workdir="/w", run_root="/hist/mas_dag"
        )

    result = await run_dag(
        spec,
        resolve=_by_name({"x": _FakeExec()}),
        backend=backend,
        workdir="/w",
        run_root="/hist/mas_dag",
        subagents_root="/hist",
    )
    assert result.summary["completed"] == 1
    prompt = backend.files[f"/hist/mas_dag/{result.run_id}/n.prompt.md"].decode()
    # Fenced as a file read, which is what it is -- the label follows the kind of
    # reference, not the directory the file turned out to sit in. Asserted
    # structurally, not against a second wrap_untrusted call, since the fence
    # mints a fresh nonce each time.
    lines = prompt.splitlines()
    assert lines[0].startswith("[BEGIN UNTRUSTED file ")
    assert lines[1] == "A SPAWN RECORD"
    assert lines[2].startswith("[END UNTRUSTED file ")


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        # The three subtrees workdir._PROTECTED_SUBTREES fences off. They sit
        # beside the history root under agent home, so a root that stopped at
        # agent home instead would reach all three.
        "/home/agent/user_memory/facts.md",
        "/home/agent/skills/x/SKILL.md",
        "/home/agent/sessions/grp/other.jsonl",
    ],
)
async def test_the_grant_stops_at_this_conversations_history(path: str) -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {
                    "id": "n",
                    "subagent": "x",
                    "node_summary": "read a referenced file",
                    "prompt_template": f"{{{{ ref:{path} }}}}",
                }
            ],
        }
    )
    backend = _InMemBackend()
    backend.files[path] = b"SHOULD NOT BE READABLE"
    with pytest.raises(DagValidationError, match="outside"):
        await run_dag(
            spec,
            resolve=_by_name({"x": _FakeExec()}),
            backend=backend,
            workdir="/w",
            run_root="/home/agent/sessions/grp/chat/subagents/mas_dag",
            subagents_root="/home/agent/sessions/grp/chat/subagents",
        )


async def test_the_tool_grants_this_conversations_history_and_nothing_wider(tmp_path: Path) -> None:
    # A bound working directory is what makes this test say anything: with none,
    # `workdir.current()` falls back to the workspace and agent home *is* the
    # first root, so a wider second root would be indistinguishable from it.
    home = tmp_path / "workspace"
    home.mkdir()
    (home / "user_memory").mkdir()
    (home / "user_memory" / "facts.md").write_text("USER SECRET FACT", encoding="utf-8")
    (home / "sessions" / "grp").mkdir(parents=True)
    (home / "sessions" / "grp" / "other.jsonl").write_text("ANOTHER CHAT", encoding="utf-8")
    ws = tmp_path / "tmp" / "web"  # a sibling of agent home, as workdir.default_channel_root gives
    ws.mkdir(parents=True)
    (ws / "brief.md").write_text("A WORKDIR FILE", encoding="utf-8")

    tool = SubAgentDagTool(
        workspace=home,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    tool.set_context("web", "victim", "web:victim")

    with workdir.bind(ws):
        # Foreground, so the outcome is this call's own result: what is asserted
        # is that the node ran and read the file, not that it was accepted.
        ok = await tool.execute(
            task_summary="run the graph under test",
            nodes=[
                {
                    "id": "reads_workdir",
                    "subagent": "echo",
                    "node_summary": "read a file under the workdir",
                    "prompt_template": f"{{{{ ref:{ws / 'brief.md'} }}}}",
                }
            ],
            background=False,
        )
        assert "1 completed" in ok.model_text

        # Its own run history is the second root, reached by absolute path.
        run_dir = Path(str(ok.model_text).split("Run dir: ")[1].splitlines()[0])
        history = await tool.execute(
            task_summary="run the graph under test",
            nodes=[
                {
                    "id": "reads_history",
                    "subagent": "echo",
                    "node_summary": "read a file under the history root",
                    "prompt_template": f"{{{{ ref:{run_dir / 'reads_workdir.out.md'} }}}}",
                }
            ],
            background=False,
        )
        assert "1 completed" in history.model_text

        # The protected subtrees beside it are not. Fresh ids, so each is
        # refused for its path rather than for reusing one.
        for nid, target in (
            ("reads_memory", home / "user_memory" / "facts.md"),
            ("reads_other_chat", home / "sessions" / "grp" / "other.jsonl"),
            ("reads_above", tmp_path / "outside.md"),
        ):
            refused = await tool.execute(
                task_summary="run the graph under test",
                nodes=[
                    {
                        "id": nid,
                        "subagent": "echo",
                        "node_summary": "read a path outside the grant",
                        "prompt_template": f"{{{{ ref:{target} }}}}",
                    }
                ],
            )
            assert "invalid DAG" in refused, nid
            assert "outside the session workdir and its sub-agent history" in refused, nid


# --- node ids are unique per session, and addressable across runs ---------


async def _run(spec_nodes: list[dict], backend, root: str, history: str = "/hist"):
    return await run_dag(
        parse_dag_spec({"task_summary": "run the graph under test", "nodes": spec_nodes}),
        resolve=_by_name({"x": _FakeExec()}),
        backend=backend,
        workdir="/w",
        run_root=root,
        subagents_root=history,
    )


async def test_a_bare_node_id_reaches_an_earlier_run_with_no_depends_on() -> None:
    backend = _InMemBackend()
    first = await _run(
        [{"id": "plan", "subagent": "x", "node_summary": "draft the plan", "prompt_template": "draft it"}],
        backend,
        "/hist/mas_dag",
    )

    second = await _run(
        [
            {
                "id": "build",
                "subagent": "x",
                "node_summary": "build using the plan's text and path",
                "prompt_template": "text={{ plan.output }} path={{ plan.output_path }}",
            }
        ],
        backend,
        "/hist/mas_dag",
    )

    assert second.summary["completed"] == 1
    prompt = backend.files[f"/hist/mas_dag/{second.run_id}/build.prompt.md"].decode()
    # A node's output is sub-agent-authored by construction, so the content
    # form arrives fenced; the path form beside it is unaffected -- a path
    # needs no fence. Asserted structurally, not against a second
    # wrap_untrusted call, since the fence mints a fresh nonce each time.
    lines = prompt.splitlines()
    assert lines[0].startswith("text=[BEGIN UNTRUSTED subagent ")
    assert lines[1] == "OUT[plan]:draft it"
    assert lines[2].startswith("[END UNTRUSTED subagent ")
    assert lines[2].endswith(f"] path=/hist/mas_dag/{first.run_id}/plan.out.md")


async def test_depends_on_may_name_a_node_an_earlier_run_completed() -> None:
    # Stating the dependency is legal even though it orders nothing: the graph
    # reads as a whole either way, and a model that writes the edge defensively
    # should not have its whole graph refused for it.
    backend = _InMemBackend()
    first = await _run(
        [{"id": "plan", "subagent": "x", "node_summary": "draft the plan", "prompt_template": "draft it"}],
        backend,
        "/hist/mas_dag",
    )

    second = await _run(
        [
            {
                "id": "build",
                "subagent": "x",
                "node_summary": "build using the plan's text and path",
                "depends_on": ["plan"],
                "prompt_template": "text={{ plan.output }} path={{ plan.output_path }}",
            }
        ],
        backend,
        "/hist/mas_dag",
    )

    assert second.summary["completed"] == 1
    prompt = backend.files[f"/hist/mas_dag/{second.run_id}/build.prompt.md"].decode()
    # A node's output is sub-agent-authored by construction, so the content
    # form arrives fenced; the path form beside it is unaffected -- a path
    # needs no fence. Asserted structurally, not against a second
    # wrap_untrusted call, since the fence mints a fresh nonce each time.
    lines = prompt.splitlines()
    assert lines[0].startswith("text=[BEGIN UNTRUSTED subagent ")
    assert lines[1] == "OUT[plan]:draft it"
    assert lines[2].startswith("[END UNTRUSTED subagent ")
    assert lines[2].endswith(f"] path=/hist/mas_dag/{first.run_id}/plan.out.md")


async def test_a_cross_run_dependency_neither_blocks_nor_unterminals_a_node() -> None:
    # The scheduler waits on statuses of *this* run. An edge out of the graph
    # has none, so it must not be waited on (the node would never become
    # ready) nor counted as a dependent (the node would stop being terminal).
    backend = _InMemBackend()
    await _run(
        [{"id": "plan", "subagent": "x", "node_summary": "draft the plan", "prompt_template": "draft it"}],
        backend,
        "/hist/mas_dag",
    )

    second = await _run(
        [
            {
                "id": "build",
                "subagent": "x",
                "node_summary": "build without reading the plan's output",
                "depends_on": ["plan"],
                "prompt_template": "no placeholder",
            }
        ],
        backend,
        "/hist/mas_dag",
    )

    assert second.summary == {"completed": 1, "failed": 0, "skipped": 0, "cancelled": 0, "total": 1}
    assert [out["node"] for out in second.terminal_outputs] == ["build"]
    # The declared edge stays on the record: it is what the node asked for.
    assert second.files[0]["depends_on"] == ["plan"]


async def test_a_cross_run_dependency_does_not_read_as_a_cycle() -> None:
    # Kahn counts unmet in-edges. Counting the out-of-graph one would leave
    # every node's indegree above zero and report the graph as cyclic.
    backend = _InMemBackend()
    await _run(
        [{"id": "plan", "subagent": "x", "node_summary": "draft the plan", "prompt_template": "draft it"}],
        backend,
        "/hist/mas_dag",
    )

    second = await _run(
        [
            {
                "id": "a",
                "subagent": "x",
                "node_summary": "read the plan's output",
                "depends_on": ["plan"],
                "prompt_template": "{{ plan.output }}",
            },
            {
                "id": "b",
                "subagent": "x",
                "node_summary": "read node a's output",
                "depends_on": ["a", "plan"],
                "prompt_template": "{{ a.output }}",
            },
        ],
        backend,
        "/hist/mas_dag",
    )

    assert second.summary["completed"] == 2
    assert [out["node"] for out in second.terminal_outputs] == ["b"]


async def test_a_local_failure_still_cascades_past_a_cross_run_dependency() -> None:
    # `b` has one satisfied edge and one failing one. The out-of-graph edge
    # must not shadow the in-graph one when failures are propagated.
    backend = _InMemBackend()
    await _run(
        [{"id": "plan", "subagent": "x", "node_summary": "draft the plan", "prompt_template": "draft it"}],
        backend,
        "/hist/mas_dag",
    )

    result = await run_dag(
        parse_dag_spec(
            {
                "task_summary": "run the graph under test",
                "nodes": [
                    {"id": "a", "subagent": "x", "node_summary": "the node that fails", "prompt_template": "hi"},
                    {
                        "id": "b",
                        "subagent": "x",
                        "node_summary": "read both a's output and the plan's",
                        "depends_on": ["a", "plan"],
                        "prompt_template": "{{ a.output }} {{ plan.output }}",
                    },
                ],
            }
        ),
        resolve=_by_name({"x": _FakeExec(fail_ids={"a"})}),
        backend=backend,
        workdir="/w",
        run_root="/hist/mas_dag",
        subagents_root="/hist",
    )

    assert result.summary["failed"] == 1
    assert result.summary["skipped"] == 1


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("failed", "which failed in run"),
        ("skipped", "which run 'runOld' skipped"),
    ],
)
async def test_depends_on_a_node_that_produced_nothing_is_refused(state: str, expected: str) -> None:
    backend = _InMemBackend()
    _seed_history(
        backend,
        [{"run_id": "runOld", "nodes": ["plan"], "summary": {"completed": 0}, "status": {"plan": state}}],
    )

    with pytest.raises(DagValidationError, match=expected):
        await _run(
            [
                {
                    "id": "build",
                    "subagent": "x",
                    "node_summary": "build off a node that produced nothing",
                    "depends_on": ["plan"],
                    "prompt_template": "hi",
                }
            ],
            backend,
            "/hist/mas_dag",
        )


async def test_depends_on_an_id_no_run_has_produced_is_still_refused() -> None:
    with pytest.raises(DagValidationError, match="depends on unknown 'ghost'"):
        await _run(
            [
                {
                    "id": "build",
                    "subagent": "x",
                    "node_summary": "build off an id no run produced",
                    "depends_on": ["ghost"],
                    "prompt_template": "hi",
                }
            ],
            _InMemBackend(),
            "/hist/mas_dag",
        )


async def test_there_is_no_run_qualifier_on_a_node_reference() -> None:
    # A node id already names one node per conversation, so a run qualifier
    # would only ever restate what the id says -- and could contradict it.
    # Pinning a run is the ``ref:@runs/`` file form's job. This shape matches
    # no known placeholder, so it is not resolved as a node reference -- it
    # passes through into the prompt as ordinary text instead.
    backend = _InMemBackend()
    result = await _run(
        [
            {
                "id": "build",
                "subagent": "x",
                "node_summary": "build off a run-qualified reference",
                "prompt_template": "{{ @runs/some-run/plan.output }}",
            }
        ],
        backend,
        "/hist/mas_dag",
    )

    assert result.summary["completed"] == 1
    prompt = backend.files[f"/hist/mas_dag/{result.run_id}/build.prompt.md"].decode()
    assert "{{ @runs/some-run/plan.output }}" in prompt


async def test_a_node_input_rejects_a_run_key() -> None:
    with pytest.raises(DagValidationError, match="nothing more to qualify"):
        await _run(
            [
                {
                    "id": "build",
                    "subagent": "x",
                    "node_summary": "build off an input with a rejected run key",
                    "prompt_template": "{{ inputs.prev }}",
                    "inputs": {"prev": {"node": "plan", "run": "some-run"}},
                }
            ],
            _InMemBackend(),
            "/hist/mas_dag",
        )


def _seed_history(backend: _InMemBackend, entries: list[dict]) -> None:
    """Hand-write a session index plus the output files its entries claim."""
    for entry in entries:
        for node_id in entry.get("nodes", []):
            if entry.get("status", {}).get(node_id, "completed") == "completed":
                backend.files[f"/hist/mas_dag/{entry['run_id']}/{node_id}.out.md"] = (
                    f"FROM-{entry['run_id'].upper()}".encode()
                )
    backend.files["/hist/mas_dag/index.json"] = json.dumps(entries).encode()


async def test_the_file_form_pins_a_run_a_bare_id_cannot_reach() -> None:
    # Ids are unique from now on, but a history written before that rule can
    # hold the same id twice; the index resolves such an id to its most recent
    # producer. Reading the older one by path is the way back to it.
    backend = _InMemBackend()
    _seed_history(
        backend,
        [
            {"run_id": "runA", "nodes": ["plan"], "status": {"plan": "completed"}, "summary": {"completed": 1}},
            {"run_id": "runB", "nodes": ["plan"], "status": {"plan": "completed"}, "summary": {"completed": 1}},
        ],
    )

    bare = await _run(
        [
            {
                "id": "n1",
                "subagent": "x",
                "node_summary": "read the plan's output",
                "prompt_template": "{{ plan.output }}",
            }
        ],
        backend,
        "/hist/mas_dag",
    )
    assert "FROM-RUNB" in backend.files[f"/hist/mas_dag/{bare.run_id}/n1.prompt.md"].decode()

    pinned = await _run(
        [
            {
                "id": "n2",
                "subagent": "x",
                "node_summary": "read the plan's output file from run a",
                "prompt_template": "{{ ref:@runs/runA/plan.out.md }}",
            }
        ],
        backend,
        "/hist/mas_dag",
    )
    assert "FROM-RUNA" in backend.files[f"/hist/mas_dag/{pinned.run_id}/n2.prompt.md"].decode()


async def test_a_run_that_recorded_no_outcome_is_not_addressable_by_id() -> None:
    # What a history written before per-node outcomes were indexed looks like.
    # The id is still taken, so it cannot be reused; it just cannot be read by
    # name, and the refusal hands over the file form instead of telling the
    # caller to wait for a run that is long over.
    backend = _InMemBackend()
    _seed_history(backend, [{"run_id": "runOld", "nodes": ["plan"], "summary": {"completed": 1}}])

    with pytest.raises(DagValidationError, match="recorded no outcome for it"):
        await _run(
            [
                {
                    "id": "n1",
                    "subagent": "x",
                    "node_summary": "read the plan's output",
                    "prompt_template": "{{ plan.output }}",
                }
            ],
            backend,
            "/hist/mas_dag",
        )
    with pytest.raises(DagValidationError, match="already used by run"):
        await _run(
            [{"id": "plan", "subagent": "x", "node_summary": "retry the plan", "prompt_template": "retry"}],
            backend,
            "/hist/mas_dag",
        )

    ok = await _run(
        [
            {
                "id": "n2",
                "subagent": "x",
                "node_summary": "read the old run's plan file",
                "prompt_template": "{{ ref:@runs/runOld/plan.out.md }}",
            }
        ],
        backend,
        "/hist/mas_dag",
    )
    assert "FROM-RUNOLD" in backend.files[f"/hist/mas_dag/{ok.run_id}/n2.prompt.md"].decode()


async def test_a_node_input_takes_another_runs_output() -> None:
    backend = _InMemBackend()
    first = await _run(
        [{"id": "plan", "subagent": "x", "node_summary": "draft the plan", "prompt_template": "draft it"}],
        backend,
        "/hist/mas_dag",
    )

    result = await _run(
        [
            {
                "id": "build",
                "subagent": "x",
                "node_summary": "build using the plan as an input",
                "prompt_template": "text={{ inputs.prev }} path={{ inputs.prev.path }}",
                "inputs": {"prev": {"node": "plan"}},
            }
        ],
        backend,
        "/hist/mas_dag",
    )
    prompt = backend.files[f"/hist/mas_dag/{result.run_id}/build.prompt.md"].decode()
    # A node's output is sub-agent-authored by construction, so the content
    # form arrives fenced; the path form beside it is unaffected -- a path
    # needs no fence. Asserted structurally, not against a second
    # wrap_untrusted call, since the fence mints a fresh nonce each time.
    lines = prompt.splitlines()
    assert lines[0].startswith("text=[BEGIN UNTRUSTED subagent ")
    assert lines[1] == "OUT[plan]:draft it"
    assert lines[2].startswith("[END UNTRUSTED subagent ")
    assert lines[2].endswith(f"] path=/hist/mas_dag/{first.run_id}/plan.out.md")


async def test_a_node_input_may_also_name_a_dependency_of_this_graph() -> None:
    backend = _InMemBackend()
    result = await _run(
        [
            {"id": "up", "subagent": "x", "node_summary": "produce the upstream value", "prompt_template": "hello"},
            {
                "id": "down",
                "subagent": "x",
                "node_summary": "consume the upstream value as an input",
                "prompt_template": "{{ inputs.from_up }}",
                "depends_on": ["up"],
                "inputs": {"from_up": {"node": "up"}},
            },
        ],
        backend,
        "/hist/mas_dag",
    )
    assert result.summary["completed"] == 2
    prompt = backend.files[f"/hist/mas_dag/{result.run_id}/down.prompt.md"].decode()
    assert "OUT[up]:hello" in prompt


async def test_reusing_a_node_id_from_an_earlier_run_is_refused() -> None:
    backend = _InMemBackend()
    await _run(
        [{"id": "plan", "subagent": "x", "node_summary": "draft the plan", "prompt_template": "draft it"}],
        backend,
        "/hist/mas_dag",
    )

    with pytest.raises(DagValidationError, match="already used by run"):
        await _run(
            [
                {
                    "id": "plan",
                    "subagent": "x",
                    "node_summary": "draft the plan again, reusing the id",
                    "prompt_template": "draft it again",
                }
            ],
            backend,
            "/hist/mas_dag",
        )


async def test_an_id_is_claimed_at_run_start_so_a_concurrent_graph_cannot_take_it() -> None:
    # Both runs are validated before either finishes; without claiming ids at
    # init the session would end up with two nodes answering to 'plan'.
    backend = _InMemBackend()
    started = asyncio.Event()
    release = asyncio.Event()

    class _Slow:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, mode=None) -> str:
            started.set()
            await release.wait()
            return "slow"

    first = asyncio.create_task(
        run_dag(
            parse_dag_spec(
                {
                    "task_summary": "run the graph under test",
                    "nodes": [
                        {"id": "plan", "subagent": "s", "node_summary": "claim the plan id", "prompt_template": "hi"}
                    ],
                }
            ),
            resolve=_by_name({"s": _Slow()}),
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
        )
    )
    await started.wait()
    try:
        with pytest.raises(DagValidationError, match="already used by run"):
            await _run(
                [
                    {
                        "id": "plan",
                        "subagent": "x",
                        "node_summary": "claim the plan id first",
                        "prompt_template": "mine now",
                    }
                ],
                backend,
                "/hist/mas_dag",
            )
    finally:
        release.set()
        await first


async def test_a_node_that_no_run_produced_is_still_refused() -> None:
    with pytest.raises(DagValidationError, match="undeclared dependency"):
        await _run(
            [
                {
                    "id": "build",
                    "subagent": "x",
                    "node_summary": "read a ghost node's output",
                    "prompt_template": "{{ ghost.output }}",
                }
            ],
            _InMemBackend(),
            "/hist/mas_dag",
        )


# --- an id being taken is not the same as its output being readable ---------


async def _seeded_outcomes(backend: _InMemBackend) -> None:
    """One finished run holding a completed, a failed and a skipped node."""
    await run_dag(
        parse_dag_spec(
            {
                "task_summary": "run the graph under test",
                "nodes": [
                    {
                        "id": "ok_node",
                        "subagent": "x",
                        "node_summary": "the node that succeeds",
                        "prompt_template": "fine",
                    },
                    {
                        "id": "bad_node",
                        "subagent": "x",
                        "node_summary": "the node that fails",
                        "prompt_template": "explode",
                    },
                    {
                        "id": "downstream",
                        "subagent": "x",
                        "node_summary": "read the failing node's output",
                        "prompt_template": "{{ bad_node.output }}",
                        "depends_on": ["bad_node"],
                    },
                ],
            }
        ),
        resolve=_by_name({"x": _FakeExec(fail_ids={"bad_node"})}),
        backend=backend,
        workdir="/w",
        run_root="/hist/mas_dag",
    )


@pytest.mark.parametrize(
    ("template", "inputs", "expected"),
    [
        ("{{ bad_node.output }}", {}, "failed in run"),
        ("{{ bad_node.output_path }}", {}, "failed in run"),
        ("{{ downstream.output }}", {}, "skipped, so it wrote no output"),
        ("{{ inputs.p }}", {"p": {"node": "bad_node"}}, "failed in run"),
        ("{{ inputs.p.path }}", {"p": {"node": "downstream"}}, "skipped, so it wrote no output"),
    ],
)
async def test_referencing_a_node_with_no_output_is_refused_before_dispatch(
    template: str, inputs: dict, expected: str
) -> None:
    # The whole point of splitting claimed ids from readable ones: these used to
    # be accepted and then die at render time, after the graph was admitted.
    backend = _InMemBackend()
    await _seeded_outcomes(backend)
    runs_before = {p for p in backend.files if p.endswith(".prompt.md")}

    with pytest.raises(DagValidationError, match=expected):
        await _run(
            [
                {
                    "id": "later",
                    "subagent": "x",
                    "node_summary": "read an earlier node's missing output",
                    "prompt_template": template,
                    "inputs": inputs,
                }
            ],
            backend,
            "/hist/mas_dag",
        )
    # Refused, so not one node of the new graph was dispatched.
    assert {p for p in backend.files if p.endswith(".prompt.md")} == runs_before


async def test_a_failed_nodes_id_stays_taken() -> None:
    backend = _InMemBackend()
    await _seeded_outcomes(backend)
    with pytest.raises(DagValidationError, match="already used by run"):
        await _run(
            [
                {
                    "id": "bad_node",
                    "subagent": "x",
                    "node_summary": "retry using the failed node's id",
                    "prompt_template": "retry",
                }
            ],
            backend,
            "/hist/mas_dag",
        )


async def test_a_completed_sibling_of_a_failed_node_is_still_readable() -> None:
    backend = _InMemBackend()
    await _seeded_outcomes(backend)
    result = await _run(
        [
            {
                "id": "later",
                "subagent": "x",
                "node_summary": "read the sibling that succeeded",
                "prompt_template": "{{ ok_node.output }}",
            }
        ],
        backend,
        "/hist/mas_dag",
    )
    assert "OUT[ok_node]:fine" in backend.files[f"/hist/mas_dag/{result.run_id}/later.prompt.md"].decode()


async def test_referencing_an_in_flight_node_does_not_suggest_re_creating_it() -> None:
    backend = _InMemBackend()
    started, release = asyncio.Event(), asyncio.Event()

    class _Slow:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, mode=None) -> str:
            started.set()
            await release.wait()
            return "eventually"

    first = asyncio.create_task(
        run_dag(
            parse_dag_spec(
                {
                    "task_summary": "run the graph under test",
                    "nodes": [
                        {
                            "id": "slow_node",
                            "subagent": "s",
                            "node_summary": "a node that runs slowly",
                            "prompt_template": "hi",
                        }
                    ],
                }
            ),
            resolve=_by_name({"s": _Slow()}),
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
        )
    )
    await started.wait()
    try:
        with pytest.raises(DagValidationError, match="has not finished writing") as caught:
            await _run(
                [
                    {
                        "id": "later",
                        "subagent": "x",
                        "node_summary": "reference the in-flight node's output",
                        "prompt_template": "{{ slow_node.output }}",
                    }
                ],
                backend,
                "/hist/mas_dag",
            )
        # Its id is taken, so "put both nodes in one graph" would be advice the
        # uniqueness check refuses; the message must not offer it.
        assert "depends_on" not in str(caught.value)
        with pytest.raises(DagValidationError, match="already used by run"):
            await _run(
                [
                    {
                        "id": "slow_node",
                        "subagent": "x",
                        "node_summary": "reuse the slow node's id",
                        "prompt_template": "mine",
                    }
                ],
                backend,
                "/hist/mas_dag",
            )
    finally:
        release.set()
        await first

    after = await _run(
        [
            {
                "id": "later2",
                "subagent": "x",
                "node_summary": "reference the in-flight node's output again",
                "prompt_template": "{{ slow_node.output }}",
            }
        ],
        backend,
        "/hist/mas_dag",
    )
    assert "eventually" in backend.files[f"/hist/mas_dag/{after.run_id}/later2.prompt.md"].decode()


@pytest.mark.parametrize(
    ("node_id", "template"),
    [("n_ref", "{{ ref:gone.md }}"), ("n_ref_path", "{{ ref_path:gone.md }}")],
)
async def test_a_missing_file_reads_the_same_for_both_ref_forms(node_id: str, template: str) -> None:
    # Literal ids, not ones derived from the template: `hash()` on `str` is
    # salted per process, so a derived id is a different id every run and two of
    # them collide often enough to matter -- and a collision fails on the
    # uniqueness rule instead of on what this test is about.
    result = await _run(
        [
            {
                "id": node_id,
                "subagent": "x",
                "node_summary": "read a file that does not exist",
                "prompt_template": template,
            }
        ],
        _InMemBackend(),
        "/hist/mas_dag",
    )
    entry = result.files[0]
    assert entry["status"] == "failed"
    assert entry["error"].startswith(f"{template} points at '/w/gone.md', which is not a file it can read"), entry[
        "error"
    ]


async def test_a_cancellation_on_the_run_started_publish_is_covered_too() -> None:
    # The ids become durable inside `store.init`, so the handler has to cover
    # every await after it -- not just the node dispatch. The run-started publish
    # is the first, and it goes to a host sink that really suspends, while the
    # shutdown sweep cancels in-flight runs including ones that have just started.
    published = asyncio.Event()

    async def publisher(name: str, payload: dict) -> None:
        if name == "dag_run_started":
            published.set()
            await asyncio.Event().wait()

    backend = _InMemBackend()
    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(
                {
                    "task_summary": "run the graph under test",
                    "nodes": [{"id": "plan", "subagent": "x", "node_summary": "node plan", "prompt_template": "hi"}],
                }
            ),
            resolve=_by_name({"x": _FakeExec()}),
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
            progress_publisher=publisher,
        )
    )
    await published.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # No node ever ran, so the claim is all there is -- and it has to read as over.
    nodes = await read_session_nodes(backend, "/hist/mas_dag")
    assert nodes.state["plan"] == "skipped"
    assert nodes.owner["plan"] == json.loads(backend.files["/hist/mas_dag/index.json"].decode())[-1]["run_id"]


async def test_an_outer_cancellation_still_records_the_run_as_over() -> None:
    # `/stop` and the shutdown sweep cancel the run task rather than setting
    # `cancel`, so `_finalize` never runs. Without an outcome written on the way
    # out, the ids claimed at `init` stay `running` in the index forever: a
    # conversation could then neither reuse nor read them, for a run that is
    # definitively over.
    started = asyncio.Event()

    class _Blocking(_FakeExec):
        async def run(self, task, **kw):  # type: ignore[override]
            started.set()
            await asyncio.Event().wait()

    backend = _InMemBackend()
    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(
                {
                    "task_summary": "run the graph under test",
                    "nodes": [{"id": "plan", "subagent": "x", "node_summary": "node plan", "prompt_template": "hi"}],
                }
            ),
            resolve=_by_name({"x": _Blocking()}),
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    entries = json.loads(backend.files["/hist/mas_dag/index.json"].decode())
    assert entries[-1]["status"] == {"plan": "cancelled"}, entries[-1]

    nodes = await read_session_nodes(backend, "/hist/mas_dag")
    assert nodes.state["plan"] == "cancelled"
    assert not nodes.is_readable("plan")

    # And the advice the two refusals give no longer contradict: neither offers
    # a reference that the other denies.
    with pytest.raises(DagValidationError, match="no output, so there is nothing to reference either"):
        await _run(
            [{"id": "plan", "subagent": "x", "node_summary": "node plan, run again", "prompt_template": "again"}],
            backend,
            "/hist/mas_dag",
        )
    with pytest.raises(DagValidationError, match="was stopped mid-run"):
        await _run(
            [
                {
                    "id": "other",
                    "subagent": "x",
                    "node_summary": "read plan's output",
                    "prompt_template": "{{ plan.output }}",
                }
            ],
            backend,
            "/hist/mas_dag",
        )


async def test_an_outer_cancellation_reaps_this_runs_memory_pollers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node that already completed before the cancellation lands has already
    scheduled its own memory-record poller (fire-and-forget, per `_run_node`).

    `_RECORD_TASKS` alone is reachable by no cancellation path -- it exists
    only to keep the task from being garbage-collected -- so without a
    run-scoped set that this run's own cancellation branch reaps, that poller
    would keep running with nothing left to stop it.
    """
    from raven.agent.subagent import dag_runner as runner_mod
    from raven.agent.subagent_memory import EverosIdentity

    poller_started = asyncio.Event()
    poller_cancelled = asyncio.Event()

    async def _fake_record(**kwargs: Any) -> None:
        poller_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            poller_cancelled.set()
            raise

    monkeypatch.setattr(runner_mod, "record_memories", _fake_record)

    identity = EverosIdentity(user_id="raven-code", agent_id=None, base_url="http://everos.test", session_prefix="cli:")
    blocked = asyncio.Event()

    class _Blocking(_FakeExec):
        async def run(self, task, **kw):  # type: ignore[override]
            blocked.set()
            await asyncio.Event().wait()

    backend = _InMemBackend()
    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(
                {
                    "task_summary": "run the graph under test",
                    "nodes": [
                        {"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "hi"},
                        {"id": "b", "subagent": "y", "node_summary": "node b", "prompt_template": "hi"},
                    ],
                }
            ),
            resolve=_by_name({"x": _FakeExec(), "y": _Blocking()}),
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
            everos_for=lambda name: identity if name == "x" else None,
        )
    )
    await blocked.wait()
    await asyncio.wait_for(poller_started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(poller_cancelled.wait(), timeout=2)


# --- the index is the uniqueness guarantee, so its writes are serialized ------


class _SuspendingBackend(_InMemBackend):
    """A backend whose I/O actually suspends, as a docker/e2b/remote one would.

    ``LocalFileBackend``'s methods are ``async def`` with no ``await`` inside,
    so today's read-modify-write of the index never yields and the races below
    cannot be observed through it. The backend is a duck-typed contract, though,
    and ``run_dag`` names those other backends in its own docstring -- so the
    guard is tested against a backend that exercises the contract's async-ness.
    """

    async def read_file(self, path: str) -> bytes:
        await asyncio.sleep(0)
        return await super().read_file(path)

    async def write_file(self, path: str, data: bytes) -> None:
        await asyncio.sleep(0)
        return await super().write_file(path, data)

    async def file_exists(self, path: str) -> bool:
        await asyncio.sleep(0)
        return await super().file_exists(path)


class _Yielding:
    async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, mode=None) -> str:
        for _ in range(10):
            await asyncio.sleep(0)
        return f"OUT[{task_id}]"


async def test_concurrent_runs_cannot_both_claim_one_node_id() -> None:
    # Read, validate and claim is a check-then-act sequence: without one guard
    # over all three, every one of these passes the uniqueness check against an
    # index that does not yet hold 'plan', and the session ends up with four
    # nodes answering to that name.
    backend = _SuspendingBackend()
    graphs = [
        run_dag(
            parse_dag_spec(
                {
                    "task_summary": "run the graph under test",
                    "nodes": [
                        {"id": "plan", "subagent": "y", "node_summary": "claim the plan id", "prompt_template": "go"}
                    ],
                }
            ),
            resolve=_by_name({"y": _Yielding()}),
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
        )
        for _ in range(4)
    ]
    outcomes = await asyncio.gather(*graphs, return_exceptions=True)

    accepted = [o for o in outcomes if not isinstance(o, BaseException)]
    refused = [o for o in outcomes if isinstance(o, DagValidationError)]
    assert len(accepted) == 1, outcomes
    assert len(refused) == 3
    assert all("already used by run" in str(o) for o in refused)
    # Exactly one node produced an output under that name.
    assert len([p for p in backend.files if p.endswith("/plan.out.md")]) == 1


async def test_concurrent_runs_do_not_drop_each_others_index_entries() -> None:
    backend = _SuspendingBackend()
    ids = [f"node{i}" for i in range(8)]
    await asyncio.gather(
        *[
            run_dag(
                parse_dag_spec(
                    {
                        "task_summary": "run the graph under test",
                        "nodes": [
                            {
                                "id": node_id,
                                "subagent": "y",
                                "node_summary": "one of several concurrent nodes",
                                "prompt_template": "go",
                            }
                        ],
                    }
                ),
                resolve=_by_name({"y": _Yielding()}),
                backend=backend,
                workdir="/w",
                run_root="/hist/mas_dag",
            )
            for node_id in ids
        ]
    )
    entries = json.loads(backend.files["/hist/mas_dag/index.json"].decode())
    assert len(entries) == 8
    assert {n for e in entries for n in e["nodes"]} == set(ids)
    # Every entry finished, so every one carries its per-node outcome.
    assert all("status" in e for e in entries)


async def test_cross_run_reference_works_over_the_real_file_backend(tmp_path: Path) -> None:
    # Every other cross-run test drives _InMemBackend, which reimplements
    # join_path/abspath/file_exists -- the three the new resolution and the
    # existence check lean on. This one drives the real LocalFileBackend over a
    # real directory, so the index round-trips through actual JSON on disk and
    # the paths handed downstream are ones the filesystem agrees exist.
    root = tmp_path / "hist" / "mas_dag"
    common: dict = {
        "resolve": _by_name({"x": _FakeExec()}),
        "backend": LocalFileBackend(),
        "workdir": str(tmp_path / "wd"),
        "run_root": str(root),
        "subagents_root": str(tmp_path / "hist"),
    }
    (tmp_path / "wd").mkdir()

    first = await run_dag(
        parse_dag_spec(
            {
                "task_summary": "run the graph under test",
                "nodes": [
                    {
                        "id": "seed",
                        "subagent": "x",
                        "node_summary": "seed a value for later runs to read",
                        "prompt_template": "make it",
                    }
                ],
            }
        ),
        **common,
    )
    assert (root / first.run_id / "seed.out.md").is_file()

    second = await run_dag(
        parse_dag_spec(
            {
                "task_summary": "run the graph under test",
                "nodes": [
                    {
                        "id": "consumer",
                        "subagent": "x",
                        "node_summary": "consume the seed node's output",
                        # The out-of-graph edge belongs on the real backend too:
                        # it is the scheduler that has to treat it as satisfied,
                        # and a run that never becomes ready writes no prompt.
                        "depends_on": ["seed"],
                        "prompt_template": "text={{ seed.output }}\npath={{ seed.output_path }}\ninput={{ inputs.p }}",
                        "inputs": {"p": {"node": "seed"}},
                    }
                ],
            }
        ),
        **common,
    )
    prompt = (root / second.run_id / "consumer.prompt.md").read_text(encoding="utf-8")
    # `{{ seed.output }}` and `{{ inputs.p }}` are different raw placeholders,
    # so render_prompt resolves -- and fences -- them separately, with
    # unrelated nonces; `seed.output_path` is a path form, so the line between
    # them is unaffected. Asserted structurally, not against a second
    # wrap_untrusted call, since the fence mints a fresh nonce each time.
    lines = prompt.splitlines()
    assert lines[0].startswith("text=[BEGIN UNTRUSTED subagent ")
    assert lines[1] == "OUT[seed]:make it"
    assert lines[2].startswith("[END UNTRUSTED subagent ")
    assert lines[3] == f"path={root / first.run_id / 'seed.out.md'}"
    assert lines[4].startswith("input=[BEGIN UNTRUSTED subagent ")
    assert lines[5] == "OUT[seed]:make it"
    assert lines[6].startswith("[END UNTRUSTED subagent ")

    # The index on disk carries both runs, with the outcome that makes 'seed'
    # readable at all.
    entries = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert [e["run_id"] for e in entries] == [first.run_id, second.run_id]
    assert entries[0]["status"] == {"seed": "completed"}

    # And the id stays taken on the real backend too.
    with pytest.raises(DagValidationError, match="already used by run"):
        await run_dag(
            parse_dag_spec(
                {
                    "task_summary": "run the graph under test",
                    "nodes": [
                        {
                            "id": "seed",
                            "subagent": "x",
                            "node_summary": "seed a value again, reusing the id",
                            "prompt_template": "again",
                        }
                    ],
                }
            ),
            **common,
        )


# --- what a node did on the way ------------------------------------------
#
# A node's account of itself used to end at the manifest's counters: the panel
# could say it called nine tools and never say which, and a node still running
# had nothing on disk at all. Both are the same seam a spawned call already
# uses -- the difference was only that the runner never opened it.


class _PublishingExec(_FakeExec):
    """A backend that reports its turn, the way the acp lane does."""

    def __init__(self) -> None:
        super().__init__()
        self.seen_live: list[object] = []

    async def run(self, task: str, **kw) -> str:
        from raven.agent.subagent import activity
        from raven.agent.subagent.dag_store import node_live_key

        self.seen_live.append(activity.live(node_live_key(self.run_id, kw["task_id"])))
        activity.note_transcript([{"role": "tool", "name": "read", "content": "file body"}])
        return await super().run(task, **kw)


async def test_a_node_writes_down_its_own_transcript() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "hello"}],
        }
    )
    exec_ = _PublishingExec()
    exec_.run_id = ""
    backend = _InMemBackend()

    result = await run_dag(
        spec,
        resolve=_by_name({"x": exec_}),
        backend=backend,
        workdir="/w",
        run_root="/hist/mas_dag",
    )

    written = backend.files[f"/hist/mas_dag/{result.run_id}/a.transcript.jsonl"].decode()
    assert '"name": "read"' in written


async def test_a_node_in_flight_is_findable_in_the_live_index() -> None:
    """The transcript file is written when the node ends, and a reader watching
    a node that is still going needs an answer before then."""
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "hello"}],
        }
    )
    exec_ = _PublishingExec()
    backend = _InMemBackend()

    # The run id is minted inside run_dag, so the backend cannot know it in
    # advance -- it is read back out of the store the runner writes to.
    import raven.agent.subagent.dag_runner as runner_mod

    mint = runner_mod.make_run_id
    minted: list[str] = []

    def _mint() -> str:
        minted.append(mint())
        exec_.run_id = minted[-1]
        return minted[-1]

    runner_mod.make_run_id = _mint
    try:
        await run_dag(spec, resolve=_by_name({"x": exec_}), backend=backend, workdir="/w", run_root="/hist/mas_dag")
    finally:
        runner_mod.make_run_id = mint

    assert exec_.seen_live and exec_.seen_live[0] is not None


async def test_a_node_in_flight_is_findable_by_instance_too() -> None:
    """A node is a turn of an instance a spawn or a direct chat may also address,
    so the conversation view has to reach it -- and that view holds
    ``(session_key, agent, handle)``, never the run id in the node's live key."""

    class _ByInstance(_FakeExec):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[object] = []

        async def run(self, task: str, **kw) -> str:
            from raven.agent.subagent import activity

            self.seen.append(activity.live_instance("web:sess1", "x", "refactor-auth"))
            return await super().run(task, **kw)

    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "node a",
                    "prompt_template": "hi",
                    "instance": "refactor-auth",
                }
            ],
        }
    )
    exec_ = _ByInstance()
    await run_dag(
        spec,
        resolve=_by_name({"x": exec_}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="web:sess1",
    )

    assert exec_.seen and exec_.seen[0] is not None


async def test_a_node_with_no_instance_is_indexed_under_its_node_id() -> None:
    """The same handle rule the instance log uses, so both name one conversation."""

    class _ByNodeId(_FakeExec):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[object] = []

        async def run(self, task: str, **kw) -> str:
            from raven.agent.subagent import activity

            self.seen.append(activity.live_instance("web:sess1", "x", "a"))
            return await super().run(task, **kw)

    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "hi"}],
        }
    )
    exec_ = _ByNodeId()
    await run_dag(
        spec,
        resolve=_by_name({"x": exec_}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="web:sess1",
    )

    assert exec_.seen and exec_.seen[0] is not None


async def test_the_live_index_does_not_outlive_the_node() -> None:
    from raven.agent.subagent import activity
    from raven.agent.subagent.dag_store import node_live_key

    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "hello"}],
        }
    )
    result = await run_dag(
        spec,
        resolve=_by_name({"x": _FakeExec()}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
    )

    assert activity.live(node_live_key(result.run_id, "a")) is None


class _BlockingExec(_FakeExec):
    """Holds one node inside its dispatch until the test lets go of it."""

    def __init__(self, block_id: str) -> None:
        super().__init__()
        self.block_id = block_id
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, task: str, *, task_id: str, **kwargs: Any) -> str:
        if task_id == self.block_id:
            self.entered.set()
            await self.release.wait()
        return await super().run(task, task_id=task_id, **kwargs)


def _two_node_spec() -> dict:
    return {
        "task_summary": "run two nodes under test",
        "nodes": [
            {"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "hi"},
            {
                "id": "b",
                "subagent": "x",
                "node_summary": "node b, downstream of a",
                "prompt_template": "{{ a.output }}",
                "depends_on": ["a"],
            },
        ],
    }


async def test_a_stop_separates_the_node_that_was_running_from_the_one_that_never_ran() -> None:
    backend = _InMemBackend()
    agent = _BlockingExec("a")
    cancel = asyncio.Event()
    events: list[dict] = []

    async def publisher(name: str, payload: dict) -> None:
        if name == "dag_node_updated":
            events.append(payload)

    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(_two_node_spec()),
            resolve=_by_name({"x": agent}),
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
            cancel=cancel,
            progress_publisher=publisher,
        )
    )
    await asyncio.wait_for(agent.entered.wait(), 5)
    cancel.set()
    result = await asyncio.wait_for(task, 5)

    assert {e["node"]: e["status"] for e in result.files} == {"a": "cancelled", "b": "skipped"}
    assert result.summary["cancelled"] == 1
    assert result.summary["skipped"] == 1
    assert result.summary["completed"] == 0
    # The last thing a client heard about `a` was that it started, so the stop
    # has to be published or the node is drawn as running forever.
    assert any(e.get("node") == "a" and e.get("status") == "cancelled" for e in events)


async def test_a_cancelled_node_keeps_the_start_time_its_dispatch_gave_it() -> None:
    backend = _InMemBackend()
    agent = _BlockingExec("a")
    cancel = asyncio.Event()

    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(_two_node_spec()),
            resolve=_by_name({"x": agent}),
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
            cancel=cancel,
        )
    )
    await asyncio.wait_for(agent.entered.wait(), 5)
    cancel.set()
    result = await asyncio.wait_for(task, 5)

    entry = next(e for e in result.files if e["node"] == "a")
    assert entry["started_at"] is not None
    assert entry["ended_at"] >= entry["started_at"]


async def test_an_outer_cancellation_records_the_running_node_as_cancelled() -> None:
    backend = _InMemBackend()
    agent = _BlockingExec("a")

    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(_two_node_spec()),
            resolve=_by_name({"x": agent}),
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
        )
    )
    await asyncio.wait_for(agent.entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    entries = json.loads(backend.files["/hist/mas_dag/index.json"].decode())
    assert entries[-1]["status"] == {"a": "cancelled", "b": "skipped"}, entries[-1]

    nodes = await read_session_nodes(backend, "/hist/mas_dag")
    assert nodes.state["a"] == "cancelled"
    assert not nodes.is_readable("a")


async def test_the_transcript_label_names_the_cancelled_nodes() -> None:
    backend = _InMemBackend()
    agent = _BlockingExec("a")
    cancel = asyncio.Event()

    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(_two_node_spec()),
            resolve=_by_name({"x": agent}),
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
            cancel=cancel,
        )
    )
    await asyncio.wait_for(agent.entered.wait(), 5)
    cancel.set()
    result = await asyncio.wait_for(task, 5)

    label = SubAgentDagTool._result_label(result)
    assert "1 cancelled" in label
    assert "1 skipped" in label


async def test_referencing_a_cancelled_node_says_it_was_stopped_not_skipped() -> None:
    backend = _InMemBackend()
    agent = _BlockingExec("a")
    cancel = asyncio.Event()

    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(
                {
                    "task_summary": "run the graph under test",
                    "nodes": [{"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "hi"}],
                }
            ),
            resolve=_by_name({"x": agent}),
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
            cancel=cancel,
        )
    )
    await asyncio.wait_for(agent.entered.wait(), 5)
    cancel.set()
    await asyncio.wait_for(task, 5)

    with pytest.raises(DagValidationError, match="was stopped mid-run"):
        await _run(
            [
                {
                    "id": "next",
                    "subagent": "x",
                    "node_summary": "read the cancelled node's output",
                    "prompt_template": "{{ a.output }}",
                }
            ],
            backend,
            "/hist/mas_dag",
        )


def test_dag_mints_an_instance_for_a_stateful_node(tmp_path: Path) -> None:
    cfg = ThirdPartyCliSubagentConfig(name="worker", command="cat {agent_id}", resume_command="cat --resume {agent_id}")
    tool = SubAgentDagTool(workspace=tmp_path, agents=[cfg])
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [{"id": "research", "subagent": "worker", "prompt_template": "go"}],
        }
    )
    minted_spec, auto = tool._mint_missing_instances(spec, tool._capability_map())
    assert auto == frozenset({"research"})
    assert re.fullmatch(r"research-[0-9a-f]{6}", minted_spec.nodes[0].instance)


def test_dag_leaves_a_stateless_node_without_an_instance(tmp_path: Path) -> None:
    cfg = ThirdPartyCliSubagentConfig(name="worker", command="cat")
    tool = SubAgentDagTool(workspace=tmp_path, agents=[cfg])
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [{"id": "research", "subagent": "worker", "prompt_template": "go"}],
        }
    )
    minted_spec, auto = tool._mint_missing_instances(spec, tool._capability_map())
    assert auto == frozenset()
    assert minted_spec.nodes[0].instance is None


def test_dag_never_overwrites_an_instance_the_model_chose(tmp_path: Path) -> None:
    cfg = ThirdPartyCliSubagentConfig(name="worker", command="cat {agent_id}", resume_command="cat --resume {agent_id}")
    tool = SubAgentDagTool(workspace=tmp_path, agents=[cfg])
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [{"id": "research", "subagent": "worker", "prompt_template": "go", "instance": "author"}],
        }
    )
    minted_spec, auto = tool._mint_missing_instances(spec, tool._capability_map())
    assert auto == frozenset()
    assert minted_spec.nodes[0].instance == "author"


def test_dag_minting_does_not_repeat_across_two_submissions(tmp_path: Path) -> None:
    """The regression lock on cross-run collision: two graphs whose nodes share
    an id must not share a session."""
    cfg = ThirdPartyCliSubagentConfig(name="worker", command="cat {agent_id}", resume_command="cat --resume {agent_id}")
    tool = SubAgentDagTool(workspace=tmp_path, agents=[cfg])
    raw = {
        "task_summary": "run the research node under test",
        "nodes": [{"id": "research", "subagent": "worker", "prompt_template": "go"}],
    }
    first, _ = tool._mint_missing_instances(parse_dag_spec(raw), tool._capability_map())
    second, _ = tool._mint_missing_instances(parse_dag_spec(raw), tool._capability_map())
    assert first.nodes[0].instance != second.nodes[0].instance


async def test_run_dag_records_whether_each_instance_was_minted() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "node a, with a chosen instance",
                    "prompt_template": "go",
                    "instance": "chosen",
                },
                {
                    "id": "b",
                    "subagent": "x",
                    "node_summary": "node b, with a minted instance",
                    "prompt_template": "go",
                    "instance": "b-abc123",
                },
            ],
        }
    )

    result = await run_dag(
        spec,
        resolve=_by_name({"x": _FakeExec()}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        auto_instances=frozenset({"b"}),
    )

    by_node = {entry["node"]: entry for entry in result.files}
    assert by_node["a"]["instance_auto"] is False
    assert by_node["b"]["instance_auto"] is True


async def test_dag_summary_names_each_stateful_node_handle(tmp_path: Path) -> None:
    cfg = ThirdPartyCliSubagentConfig(name="worker", command="cat {agent_id}", resume_command="cat --resume {agent_id}")
    tool = SubAgentDagTool(workspace=tmp_path, agents=[cfg])
    result = await tool.execute(
        task_summary="run the graph under test",
        nodes=[{"id": "research", "subagent": "worker", "node_summary": "research the topic", "prompt_template": "go"}],
        background=False,
    )
    assert re.search(r"- research \[\w+\] \(instance: research-[0-9a-f]{6}\):", str(result))


# --- the graph-level confirm gate ----------------------------------------
#
# The gate a playbook's ``confirm: true`` lands on. Before it existed, the only
# place that honoured a playbook's confirm was the passive funnel, ahead of the
# turn -- so this parameter had to exist before that funnel could be deleted
# without the gate going with it.


def _one_node() -> list[dict]:
    return [{"id": "publish", "subagent": "on", "node_summary": "publish the result", "prompt_template": "ship it"}]


def _confirming_tool(tmp_path: Path, answer, asked: list[str]):
    cfg = ThirdPartyCliSubagentConfig(name="on", command="cat")

    async def ask(conversation: str, question: str) -> bool:
        asked.append(question)
        if isinstance(answer, Exception):
            raise answer
        return answer

    # Through the constructor, which is how the loop supplies it. These tests used
    # to call a `set_ask` setter instead, and that is precisely why they all passed
    # while the registered `run_subagent_dag` had no asker at all.
    return SubAgentDagTool(workspace=tmp_path, agents=[cfg], session_dir=lambda _k: tmp_path, ask=ask)


async def test_confirm_false_asks_nobody(tmp_path: Path) -> None:
    asked: list[str] = []
    tool = _confirming_tool(tmp_path, True, asked)
    tool._registry._backends["on"] = _FakeExec()

    out = await tool.execute(_one_node(), task_summary="run the graph under test", background=False)

    assert asked == []
    assert isinstance(out, ToolResult), out


async def test_confirm_true_dispatches_only_on_approval(tmp_path: Path) -> None:
    asked: list[str] = []
    tool = _confirming_tool(tmp_path, True, asked)
    backend = _FakeExec()
    tool._registry._backends["on"] = backend

    out = await tool.execute(_one_node(), task_summary="run the graph under test", background=False, confirm=True)

    assert len(asked) == 1
    assert isinstance(out, ToolResult), out
    assert backend.calls  # the node actually ran


async def test_a_refused_graph_runs_nothing(tmp_path: Path) -> None:
    asked: list[str] = []
    tool = _confirming_tool(tmp_path, False, asked)
    backend = _FakeExec()
    tool._registry._backends["on"] = backend

    out = await tool.execute(_one_node(), task_summary="run the graph under test", background=False, confirm=True)

    assert not backend.calls, "a refused graph must cost zero dispatches"
    assert "did not approve" in str(out)
    assert "Do not re-submit" in str(out)


async def test_the_question_names_every_step(tmp_path: Path) -> None:
    """One gate for the whole graph means approving it approves every step.

    So the steps have to be in the question -- a bare "run 6 nodes?" asks someone
    to agree to something they were not shown.
    """
    asked: list[str] = []
    tool = _confirming_tool(tmp_path, True, asked)
    tool._registry._backends["on"] = _FakeExec()

    nodes = [
        {"id": "draft", "subagent": "on", "node_summary": "write the draft", "prompt_template": "write"},
        {
            "id": "publish",
            "subagent": "on",
            "node_summary": "publish the draft",
            "prompt_template": "ship",
            "depends_on": ["draft"],
        },
    ]
    await tool.execute(nodes, task_summary="run the graph under test", background=False, confirm=True)

    assert "draft" in asked[0]
    assert "publish" in asked[0]


async def test_an_unreachable_asker_is_a_no(tmp_path: Path) -> None:
    """A gate that cannot be delivered must not become a gate that passed."""
    asked: list[str] = []
    tool = _confirming_tool(tmp_path, RuntimeError("broker gone"), asked)
    backend = _FakeExec()
    tool._registry._backends["on"] = backend

    out = await tool.execute(_one_node(), task_summary="run the graph under test", background=False, confirm=True)

    assert not backend.calls
    assert "did not approve" in str(out)


async def test_no_ask_channel_dispatches_and_says_so(tmp_path: Path) -> None:
    """Not every surface can put a question to a human -- a cron trigger, an IM
    channel with no interactive reply. Letting the absence of one disable the
    feature outright is the worse failure, so it proceeds; the log is what makes
    the decision visible rather than silent."""
    cfg = ThirdPartyCliSubagentConfig(name="on", command="cat")
    tool = SubAgentDagTool(workspace=tmp_path, agents=[cfg], session_dir=lambda _k: tmp_path)
    backend = _FakeExec()
    tool._registry._backends["on"] = backend

    out = await tool.execute(_one_node(), task_summary="run the graph under test", background=False, confirm=True)

    assert isinstance(out, ToolResult), out
    assert backend.calls


async def test_a_refused_graph_is_not_charged(tmp_path: Path) -> None:
    """The gate sits ahead of billing: a graph the user turned down must not
    spend the session's sub-agent budget either."""
    asked: list[str] = []
    charged: list[str | None] = []
    tool = _confirming_tool(tmp_path, False, asked)
    tool._charge = lambda key: charged.append(key) or None
    tool._registry._backends["on"] = _FakeExec()

    await tool.execute(_one_node(), task_summary="run the graph under test", background=False, confirm=True)

    assert charged == []


async def test_a_node_naming_an_agent_nothing_resolves_is_refused_before_the_run() -> None:
    """Refused up front, not when the scheduler reaches that node.

    ``resolve`` answering ``None`` is how "this name is on no table" arrives, and
    a graph half-run before it surfaces leaves nodes whose output later nodes
    were written to read. The tool checks the same thing against the registry for
    the model's benefit; this one guards the runner against any caller.
    """
    with pytest.raises(DagValidationError, match="names unknown sub-agent"):
        await run_dag(
            parse_dag_spec(
                {
                    "task_summary": "run the graph under test",
                    "nodes": [{"id": "a", "subagent": "ghost", "node_summary": "node a", "prompt_template": "hi"}],
                }
            ),
            resolve=_by_name({}),
            backend=_InMemBackend(),
            workdir="/w",
            run_root="/hist/mas_dag",
        )


def test_the_node_resolver_hands_an_acp_backend_the_session_dir_rule(tmp_path: Path) -> None:
    """The same hand-over ``SubagentManager._resolve_backend`` makes, and for the
    reason the capture on the far side makes it necessary: an acp backend builds
    its resident unprompted-turn recorder on the first prompt it sends and keeps
    whatever resolver is bound by then, so a graph that dispatches before any
    spawn or direct chat would leave that connection unable to record. The
    registry hands both lanes the same instance -- ``build`` narrows only the
    built-in kind -- so this is about which lane goes first, not about which
    object each holds."""
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyAcpSubagentConfig(name="oncall", command="true")],
    )

    backend = tool._resolve_node(DagNodeSpec(id="a", subagent="oncall", node_summary="node a", prompt_template="hi"))

    assert backend is not None
    assert backend._session_dir_for == tool._session_dir_for
    assert Path(backend._session_dir_for("web:s1")).is_relative_to(tmp_path)


async def test_a_node_writes_its_question_into_the_instance_log(tmp_path: Path) -> None:
    """The turn's own prompt, without which the next turn merges into it.

    ``foldDirectTurns`` starts a message at a ``user`` row, so a turn with no
    question of its own is not merely missing a row -- the turn after it has its
    steps and its answer drawn as a continuation of this one. The live read does
    emit the question from the activity, so it appeared while the node ran and
    then vanished when the node landed and this log became the source.
    """
    from raven.agent.subagent.instance_log import transcript_path

    session_dir = tmp_path / "s"
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "count the todos",
                    "prompt_template": "count the todos",
                    "instance": "h1",
                }
            ],
        }
    )
    await run_dag(
        spec,
        resolve=_by_name({"x": _FakeExec()}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root=str(session_dir / "subagents" / "mas_dag"),
        session_key="web:s1",
        subagents_root=str(session_dir / "subagents"),
    )

    rows = [
        json.loads(line)
        for line in transcript_path(session_dir, "x", "h1").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [r["content"] for r in rows if r.get("role") == "user"] == ["count the todos"]


async def test_a_node_writes_its_answer_into_the_instance_log(tmp_path: Path) -> None:
    """The turn's own answer, for a transport that reports no closing text.

    ``add_turn_to_instance_log`` derives the answer from ``output`` when the
    activity reports no ``closing`` -- which is every transport that cannot tell
    narration from the reply, the cli lane among them. The DAG lane passed no
    ``output``, so such a node's turn ended on its last step and carried no
    answer row at all, while ``spawn`` and the direct chat both passed one.

    That row is also the conclusion an everos extraction reads, so without it a
    trace-sourced record would describe the work and never the result.

    Deliberately the plain ``_FakeExec``: the ``reply=`` form calls
    ``note_closing``, which supplies the answer by the other route and would let
    this pass with the ``output`` argument removed.
    """
    from raven.agent.subagent.instance_log import transcript_path

    session_dir = tmp_path / "s"
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "count the todos",
                    "prompt_template": "count the todos",
                    "instance": "h1",
                }
            ],
        }
    )
    await run_dag(
        spec,
        resolve=_by_name({"x": _FakeExec()}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root=str(session_dir / "subagents" / "mas_dag"),
        session_key="web:s1",
        subagents_root=str(session_dir / "subagents"),
    )

    rows = [
        json.loads(line)
        for line in transcript_path(session_dir, "x", "h1").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["content"] == "OUT[a]:count the todos"


async def test_a_failed_node_writes_the_failure_into_the_instance_log(tmp_path: Path) -> None:
    """``append_turn`` promises a failed turn is appended as the error it ended
    with, and the runner holds that error the whole time."""
    from raven.agent.subagent.instance_log import transcript_path

    class _Exploding(_FakeExec):
        async def run(self, task: str, **kw) -> str:
            raise RuntimeError("the node blew up")

    session_dir = tmp_path / "s"
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "go", "instance": "h1"}
            ],
        }
    )
    await run_dag(
        spec,
        resolve=_by_name({"x": _Exploding()}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root=str(session_dir / "subagents" / "mas_dag"),
        session_key="web:s1",
        subagents_root=str(session_dir / "subagents"),
    )

    body = transcript_path(session_dir, "x", "h1").read_text(encoding="utf-8")

    assert "the node blew up" in body


# --- memory record ---------------------------------------------------------


async def _run_one_node_dag(
    tmp_path: Path,
    *,
    node_id: str = "n1",
    subagent: str = "x",
    prompt: str = "hi",
    output: str | None = None,
    everos_for: "Any | None" = None,
    instance: str | None = None,
) -> DagRunResult:
    """Run a single-node DAG over the real file backend, under `tmp_path`.

    Uses `LocalFileBackend`, like `test_cross_run_reference_works_over_the_real_file_backend`,
    so the memory record a node leaves behind can be read back as a real file.

    `subagents_root` is passed so the node's turn actually reaches the instance
    log instead of short-circuiting on `_add_node_to_instance_log`'s own guard --
    the parent of `run_root`, the same relationship `run_dag`'s docstring
    describes between the two in the real layout.
    """
    (tmp_path / "wd").mkdir()
    return await run_dag(
        parse_dag_spec(
            {
                "task_summary": "run the graph under test",
                "nodes": [
                    {
                        "id": node_id,
                        "subagent": subagent,
                        "node_summary": "the node under test",
                        "prompt_template": prompt,
                        **({"instance": instance} if instance else {}),
                    }
                ],
            }
        ),
        resolve=_by_name({subagent: _FakeExec(reply=output)}),
        backend=LocalFileBackend(),
        workdir=str(tmp_path / "wd"),
        run_root=str(tmp_path / "hist" / "mas_dag"),
        subagents_root=str(tmp_path / "hist"),
        session_key="web:sess1",
        everos_for=everos_for,
    )


async def _drain_record_tasks() -> None:
    """Wait for every memory-record background task the runner scheduled.

    Copies `_RECORD_TASKS` before gathering it: each task's own done-callback
    discards itself from that set as it finishes, and iterating a set something
    else is concurrently mutating is a bug waiting to happen.
    """
    from raven.agent.subagent import dag_runner as runner_mod

    await asyncio.gather(*list(runner_mod._RECORD_TASKS))


async def test_a_node_leaves_a_memory_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent import dag_runner as runner_mod
    from raven.agent.subagent_memory import EverosIdentity

    async def _fake_record(**kwargs: Any) -> None:
        await kwargs["write"]('{"agent": "Raven-Code", "status": "settled", "memories": []}')

    monkeypatch.setattr(runner_mod, "record_memories", _fake_record)

    identity = EverosIdentity(user_id="raven-code", agent_id=None, base_url="http://everos.test", session_prefix="cli:")
    result = await _run_one_node_dag(tmp_path, everos_for=lambda _name: identity)
    # The record is scheduled fire-and-forget once the node releases its
    # semaphore slot; run_dag returns without waiting for it, so drain it
    # explicitly before reading what it wrote.
    await _drain_record_tasks()
    written = Path(result.dir) / "n1.memory.json"
    assert json.loads(written.read_text(encoding="utf-8"))["status"] == "settled"


async def test_a_node_without_an_everos_identity_schedules_no_memory_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import dag_runner as runner_mod

    calls: list[Any] = []

    async def _fake_record(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(runner_mod, "record_memories", _fake_record)
    await _run_one_node_dag(tmp_path, everos_for=lambda _name: None)
    assert not runner_mod._RECORD_TASKS
    assert calls == []


async def test_a_node_schedules_no_memory_task_without_everos_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import dag_runner as runner_mod

    calls: list[Any] = []

    async def _fake_record(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(runner_mod, "record_memories", _fake_record)
    await _run_one_node_dag(tmp_path)
    assert not runner_mod._RECORD_TASKS
    assert calls == []


async def test_a_node_passes_its_instance_to_the_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner is the only place `node.instance` can reach the recorder, and
    every stateful node now has one -- minted by the tool when the author named
    none."""
    from raven.agent.subagent import dag_runner as runner_mod
    from raven.agent.subagent_memory import EverosIdentity

    seen: dict[str, Any] = {}

    async def _fake_record(**kwargs: Any) -> None:
        seen.update(kwargs)
        await kwargs["write"]("{}")

    monkeypatch.setattr(runner_mod, "record_memories", _fake_record)
    identity = EverosIdentity(user_id="u", agent_id=None, base_url="http://everos.test", session_prefix="cli:")
    await _run_one_node_dag(tmp_path, everos_for=lambda _name: identity, instance="audit-a3f9c1")
    await _drain_record_tasks()

    assert seen["instance"] == "audit-a3f9c1"


async def test_dag_node_primes_a_trace_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `trace` node's record is primed with the same turn the log records,
    under the session id the host mints from this run and this node."""
    from raven.agent import subagent_memory as memory_mod
    from raven.agent.subagent import dag_runner as runner_mod
    from raven.agent.subagent_memory import EverosIdentity

    primed: list[tuple[str, list[dict]]] = []

    async def _fake_prime(*, identity, session_id, turn, client=None) -> bool:
        primed.append((session_id, turn))
        return True

    async def _unavailable_everos(*args: Any, **kwargs: Any) -> list:
        raise RuntimeError("no live everos in tests")

    monkeypatch.setattr(runner_mod, "prime_from_turn", _fake_prime)
    # The prime lands (faked above), but the poll after it is real: nothing in
    # this test process is listening at the identity's base url, so make the
    # first look fail fast instead of sleeping through the whole poll budget.
    monkeypatch.setattr(memory_mod, "collect_memories", _unavailable_everos)

    identity = EverosIdentity(
        user_id="liv",
        agent_id="coder",
        base_url="http://everos.test",
        session_prefix="cli:",
        source="trace",
    )
    result = await _run_one_node_dag(
        tmp_path,
        node_id="inspect",
        subagent="Coder",
        prompt="read it",
        output="no readme",
        everos_for=lambda _name: identity,
    )
    await _drain_record_tasks()

    assert primed
    session_id, turn = primed[0]
    assert session_id == f"trace:Coder:{result.run_id}:inspect"
    assert turn[0]["content"] == "read it"
    assert turn[-1]["content"] == "no readme"


async def test_a_capped_node_reply_still_lands_whole_in_its_output_file() -> None:
    """``.out.md`` is what the reader, the next node's placeholder and the record
    all resolve to, so the reply cap must not reach it.

    The cap belongs at the boundary that has a context window to protect -- which
    is why the terminal outputs handed back to the main agent are capped again on
    the way out, and this file is not.
    """

    class _Verbose:
        """A backend that caps its reply the way the real ones do."""

        async def run(self, task: str, *, task_id: str, workspace, executor, **_: Any) -> str:
            return await clamp_output("y" * 400, 200, agent="verbose")

    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [{"id": "a", "subagent": "v", "node_summary": "say a lot", "prompt_template": "go"}],
        }
    )
    backend = _InMemBackend()
    result = await run_dag(
        spec,
        resolve=_by_name({"v": _Verbose()}),
        backend=backend,
        workdir="/w",
        run_root="/hist/mas_dag",
    )

    entry = next(e for e in result.files if e["node"] == "a")
    assert backend.files[entry["output_file"]].decode() == "y" * 400
    # Not a claim that anything was capped here -- 400 is far under the DAG's own
    # 128000 terminal limit. What it pins is that the main agent now receives the
    # whole answer where it used to receive the node backend's capped 200.
    assert len(result.terminal_outputs[0]["text"]) == 400
    manifest = json.loads(backend.files[posixpath.join(posixpath.dirname(entry["output_file"]), "manifest.json")])
    assert manifest["a"]["output_truncated"] is True
    assert manifest["a"]["output_chars_total"] == 400


from raven.agent.subagent.dag_runner import _cascade_failures, _mark_stopped, _tally


def test_cascade_leaves_an_exception_nodes_dependents_pending():
    deps = {"a": [], "b": ["a"], "c": ["b"]}
    status = {"a": "exception", "b": "pending", "c": "pending"}
    _cascade_failures(deps, status)
    assert status == {"a": "exception", "b": "pending", "c": "pending"}


def test_cascade_still_skips_behind_a_failed_node():
    deps = {"a": [], "b": ["a"]}
    status = {"a": "failed", "b": "pending"}
    _cascade_failures(deps, status)
    assert status["b"] == "skipped"


def test_cascade_skips_dependents_once_an_exception_becomes_failed():
    deps = {"a": [], "b": ["a"]}
    status = {"a": "exception", "b": "pending"}
    _cascade_failures(deps, status)
    status["a"] = "failed"
    _cascade_failures(deps, status)
    assert status["b"] == "skipped"


def test_mark_stopped_cancels_a_suspended_node():
    status = {"a": "exception", "b": "running", "c": "pending"}
    _mark_stopped(status)
    assert status == {"a": "cancelled", "b": "cancelled", "c": "skipped"}


def test_tally_does_not_count_exception_as_a_terminal_state():
    counts = _tally({"a": "completed", "b": "exception"})
    assert counts == {"total": 2, "completed": 1, "failed": 0, "skipped": 0, "cancelled": 0}


# --- adjudication desk: node decision and waiter coordination ---------


async def test_desk_resolve_wakes_the_waiter():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk

    desk = AdjudicationDesk()
    event = desk.open("a")
    assert desk.is_open("a") is True
    assert desk.resolve("a", "continue", "try the staging token") is True
    await asyncio.wait_for(event.wait(), timeout=1)
    answer = desk.take("a")
    assert answer.decision == "continue"
    assert answer.message == "try the staging token"


def test_desk_refuses_a_node_it_is_not_waiting_on():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk

    desk = AdjudicationDesk()
    assert desk.resolve("nope", "abandon", None) is False


def test_desk_take_is_once_only():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk

    desk = AdjudicationDesk()
    desk.open("a")
    desk.resolve("a", "abandon", None)
    assert desk.take("a").decision == "abandon"
    assert desk.take("a") is None


def test_desk_close_stops_it_being_open():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk

    desk = AdjudicationDesk()
    desk.open("a")
    desk.close("a")
    assert desk.is_open("a") is False
    assert desk.open_nodes() == set()
    assert desk.resolve("a", "continue", "x") is False


def test_desk_lists_every_open_node():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk

    desk = AdjudicationDesk()
    desk.open("a")
    desk.open("b")
    assert desk.open_nodes() == {"a", "b"}


async def test_await_adjudications_continues_a_node():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("a")
    status = {"a": "exception"}
    errors: dict[str, str] = {}
    continuations: dict[str, str] = {}

    async def _answer():
        await asyncio.sleep(0)
        desk.resolve("a", "continue", "use the staging token")

    await asyncio.gather(
        _await_adjudications(desk, status, errors, continuations, timeout_s=5, cancel=None),
        _answer(),
    )
    assert status["a"] == "pending"
    assert continuations["a"] == "use the staging token"


async def test_await_adjudications_abandons_a_node():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("a")
    status = {"a": "exception"}
    errors: dict[str, str] = {}

    async def _answer():
        await asyncio.sleep(0)
        desk.resolve("a", "abandon", None)

    await asyncio.gather(
        _await_adjudications(desk, status, errors, {}, timeout_s=5, cancel=None),
        _answer(),
    )
    assert status["a"] == "failed"
    assert "abandoned" in errors["a"]


async def test_await_adjudications_fails_the_node_on_timeout():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("a")
    status = {"a": "exception"}
    errors: dict[str, str] = {}
    await _await_adjudications(desk, status, errors, {}, timeout_s=0.01, cancel=None)
    assert status["a"] == "failed"
    assert "timed out" in errors["a"]


async def test_await_adjudications_gives_up_when_the_run_is_cancelled():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("a")
    cancel = asyncio.Event()
    cancel.set()
    status = {"a": "exception"}
    await _await_adjudications(desk, status, {}, {}, timeout_s=60, cancel=cancel)
    assert status["a"] == "exception"
    assert desk.open_nodes() == set()


async def test_await_adjudications_closes_every_open_node_when_cancelled():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("a")
    desk.open("b")
    cancel = asyncio.Event()
    cancel.set()
    status = {"a": "exception", "b": "exception"}
    await _await_adjudications(desk, status, {}, {}, timeout_s=60, cancel=cancel)
    assert status == {"a": "exception", "b": "exception"}
    assert desk.open_nodes() == set()


async def test_await_adjudications_hard_cancel_leaves_no_stranded_task_or_open_desk():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("a")
    desk.open("b")
    status = {"a": "exception", "b": "exception"}
    baseline = asyncio.all_tasks()

    async def _wait():
        await _await_adjudications(desk, status, {}, {}, timeout_s=60, cancel=None)

    task = asyncio.create_task(_wait())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Task.cancel() only schedules delivery for a later tick, so the node's
    # own wait task may still show up as "not done" right after `await task`
    # returns; give it a bounded number of ticks to actually finish.
    stray: set[asyncio.Task] = set()
    for _ in range(10):
        stray = asyncio.all_tasks() - baseline
        if not stray:
            break
        await asyncio.sleep(0)
    assert not stray
    assert desk.open_nodes() == set()


async def test_await_adjudications_fails_a_node_the_desk_never_opened():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    status = {"a": "exception"}
    errors: dict[str, str] = {}
    # `status` and the desk disagreeing used to return immediately with
    # nothing changed, which spins run_dag's caller forever with no `await`
    # in the cycle; wrap in wait_for so a regression fails this test instead
    # of hanging the whole suite (pytest-timeout is not installed here).
    await asyncio.wait_for(
        _await_adjudications(desk, status, errors, {}, timeout_s=5, cancel=None),
        timeout=5,
    )
    assert status["a"] == "failed"
    assert "no adjudication" in errors["a"].lower()


async def test_await_adjudications_fails_a_contentless_continue():
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("a")
    status = {"a": "exception"}
    errors: dict[str, str] = {}

    async def _answer():
        await asyncio.sleep(0)
        desk.resolve("a", "continue", None)

    await asyncio.gather(
        _await_adjudications(desk, status, errors, {}, timeout_s=5, cancel=None),
        _answer(),
    )
    assert status["a"] == "failed"
    assert "abandoned" not in errors["a"]
    assert "continue" in errors["a"].lower()


# --- node verdicts: judge the node, suspend it, report it, continue it ---

_TEST_ORIGIN = {"channel": "t", "chat_id": "t", "session_key": "t"}


async def _run_two_node_dag(
    tmp_path,
    *,
    desk,
    judge_node,
    announce_exception,
    max_continuations=2,
    exec_backend=None,
    instance_a=None,
    origin=_TEST_ORIGIN,
    adjudication_timeout_s=5,
    semaphore=None,
    control_reachable=None,
):
    """A two-node chain a->b, run through one shared fake backend.

    Both nodes resolve to the same backend instance (unless ``exec_backend``
    is given) so a test can inspect every prompt either node actually
    received via its ``.calls`` list, including across a node's own
    continuations -- the four pre-existing tests below never look at
    ``.calls`` so sharing changes nothing for them. ``origin`` defaults to a
    real dict rather than ``None`` so tests exercise the guarded
    ``announce_exception`` path the only real caller always takes; a test can
    still pass ``origin=None`` explicitly to exercise the guard itself.
    """
    backend = exec_backend if exec_backend is not None else _FakeExec()
    node_a: dict[str, Any] = {"id": "a", "subagent": "x", "node_summary": "first", "prompt_template": "do a"}
    if instance_a:
        node_a["instance"] = instance_a
    spec = parse_dag_spec(
        {
            "task_summary": "two nodes",
            "nodes": [
                node_a,
                {
                    "id": "b",
                    "subagent": "x",
                    "node_summary": "second",
                    "prompt_template": "do b",
                    "depends_on": ["a"],
                },
            ],
        }
    )
    return await run_dag(
        spec,
        resolve=lambda node: backend,
        backend=LocalFileBackend(),
        workdir=str(tmp_path),
        run_root=str(tmp_path / "runs"),
        desk=desk,
        judge_node=judge_node,
        announce_exception=announce_exception,
        max_continuations=max_continuations,
        origin=origin,
        adjudication_timeout_s=adjudication_timeout_s,
        semaphore=semaphore,
        control_reachable=control_reachable,
    )


async def test_a_node_judged_not_accomplished_suspends_and_reports(tmp_path):
    """The node does not complete, its dependent does not start, and a report fires."""
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    reports = []

    async def _announce(run_id, node_id, report, origin, **_):
        reports.append((node_id, report))
        desk.resolve(node_id, "abandon", None)

    async def _judge(**kwargs):
        return Verdict(accomplished=False, category="missing_credential", what_is_missing="a token")

    result = await _run_two_node_dag(tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce)
    assert result.summary["failed"] == 1
    assert result.summary["skipped"] == 1
    assert reports[0][0] == "a"
    assert "missing_credential" in reports[0][1]


async def test_a_continued_node_runs_again_and_can_then_pass(tmp_path):
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    seen = []

    async def _announce(run_id, node_id, report, origin, **_):
        desk.resolve(node_id, "continue", "use the staging token")

    async def _judge(**kwargs):
        seen.append(kwargs)
        return Verdict(accomplished=len(seen) > 1)

    result = await _run_two_node_dag(tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce)
    assert result.summary["completed"] == 2
    assert len(seen) == 3


async def test_the_continuation_limit_fails_the_node(tmp_path):
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    reports = []

    async def _announce(run_id, node_id, report, origin, **_):
        reports.append(report)
        desk.resolve(node_id, "continue", "try again")

    async def _judge(**kwargs):
        return Verdict(accomplished=False, category="tool_failure", what_is_missing="the tool keeps dying")

    result = await _run_two_node_dag(
        tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce, max_continuations=2
    )
    assert result.summary["failed"] == 1
    assert len(reports) == 3
    assert "no adjudication is being awaited" in reports[-1]


async def test_the_verdict_is_skipped_when_no_judge_is_wired(tmp_path):
    result = await _run_two_node_dag(tmp_path, desk=None, judge_node=None, announce_exception=None)
    assert result.summary["completed"] == 2


async def test_a_bad_verdict_with_no_origin_does_not_crash_and_still_fails(tmp_path):
    """The 'origin is not None' guard must survive: SubagentManager._inject

    subscripts ``origin["channel"]`` and raises on ``None``. Nothing routes a
    real run through ``origin=None`` today, but ``run_dag``'s own signature
    allows it, so the guard is what stands between that and a TypeError deep
    inside the announce callback instead of a clean fail.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    announced = []

    async def _announce(run_id, node_id, report, origin, **_):
        announced.append((node_id, report, origin))

    async def _judge(**kwargs):
        return Verdict(accomplished=False, category="missing_credential", what_is_missing="a token")

    result = await _run_two_node_dag(
        tmp_path,
        desk=desk,
        judge_node=_judge,
        announce_exception=_announce,
        origin=None,
        adjudication_timeout_s=0.3,
    )
    assert announced == []
    assert result.summary["failed"] == 1


async def test_a_bad_verdict_blocks_the_dependent_and_clears_the_manifest_output(tmp_path):
    """Both halves of the invariant: b never dispatches, and a's manifest

    entry reports no output file -- neither is provable by the other, since
    the ready-set gate (not the pop) is what stops b's dispatch.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    fake = _FakeExec()

    async def _announce(run_id, node_id, report, origin, **_):
        desk.resolve(node_id, "abandon", None)

    async def _judge(**kwargs):
        return Verdict(accomplished=False, category="missing_credential", what_is_missing="a token")

    result = await _run_two_node_dag(
        tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce, exec_backend=fake
    )
    assert {call["task_id"] for call in fake.calls} == {"a"}
    files_by_node = {f["node"]: f for f in result.files}
    assert files_by_node["a"]["output_file"] is None


async def test_a_bad_verdict_records_the_reason_as_the_nodes_error(tmp_path):
    """A verdict that fails a node outright (no desk to suspend it on) must

    still leave its reason somewhere a caller can read -- the manifest's
    ``error`` field, populated from the same dict a crash would have used.
    """
    from raven.agent.subagent.dag_verdict import Verdict

    async def _judge(**kwargs):
        return Verdict(accomplished=False, category="missing_credential", what_is_missing="a token")

    result = await _run_two_node_dag(tmp_path, desk=None, judge_node=_judge, announce_exception=None)
    files_by_node = {f["node"]: f for f in result.files}
    assert files_by_node["a"]["error"] == "a token"


async def test_a_continued_instance_node_is_sent_only_the_follow_up_and_keeps_its_task_pinned(tmp_path):
    """A stateful node's continuation is the bare follow-up on the wire, but

    the task the judge (and dag_status/read_node) sees for it never moves off
    attempt 1's original render.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    fake = _FakeExec()
    seen = []

    async def _announce(run_id, node_id, report, origin, **_):
        desk.resolve(node_id, "continue", "use the staging token")

    async def _judge(**kwargs):
        seen.append(kwargs)
        return Verdict(accomplished=len(seen) > 1)

    result = await _run_two_node_dag(
        tmp_path,
        desk=desk,
        judge_node=_judge,
        announce_exception=_announce,
        exec_backend=fake,
        instance_a="researcher",
    )
    assert result.summary["completed"] == 2
    a_calls = [call for call in fake.calls if call["task_id"] == "a"]
    assert len(a_calls) == 2
    assert a_calls[1]["prompt"] == "use the staging token"

    store = DagRunStore(backend=LocalFileBackend(), root=str(tmp_path / "runs"), run_id=result.run_id)
    task_prompt = await store.read_text(store.prompt_path("a"))
    assert "use the staging token" not in task_prompt
    attempt_1 = await store.read_text(store.attempt_prompt_path("a", 1))
    attempt_2 = await store.read_text(store.attempt_prompt_path("a", 2))
    assert attempt_1 == task_prompt
    assert attempt_2 == "use the staging token"


async def test_a_continued_stateless_node_sends_task_plus_previous_output_plus_message(tmp_path):
    """A node with no instance has no conversation history on the other end,

    so the whole task has to travel with the follow-up, along with what the
    previous attempt returned, or the retry starts from nothing.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    fake = _FakeExec()
    seen = []

    async def _announce(run_id, node_id, report, origin, **_):
        desk.resolve(node_id, "continue", "use the staging token")

    async def _judge(**kwargs):
        seen.append(kwargs)
        return Verdict(accomplished=len(seen) > 1)

    result = await _run_two_node_dag(
        tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce, exec_backend=fake
    )
    assert result.summary["completed"] == 2
    a_calls = [call for call in fake.calls if call["task_id"] == "a"]
    assert len(a_calls) == 2
    first_prompt = a_calls[0]["prompt"]
    second_prompt = a_calls[1]["prompt"]
    assert second_prompt.startswith(first_prompt)
    assert "Your previous attempt returned:" in second_prompt
    assert f"OUT[a]:{first_prompt}" in second_prompt
    assert second_prompt.endswith("Now: use the staging token")


async def test_a_crashed_node_judge_call_is_told_it_crashed(tmp_path):
    """describe_failure's whole branch is dead unless a crashed (not just a

    completed-but-wrong) node also reaches the judge, with crashed=True and
    its error text.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    fake = _FakeExec(fail_ids={"a"})
    seen = []

    async def _announce(run_id, node_id, report, origin, **_):
        desk.resolve(node_id, "abandon", None)

    async def _judge(**kwargs):
        seen.append(kwargs)
        return Verdict(accomplished=False, category="tool_failure", what_is_missing="it crashed")

    await _run_two_node_dag(tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce, exec_backend=fake)
    assert len(seen) == 1
    assert seen[0]["crashed"] is True
    assert seen[0]["error"] == "boom a"


async def test_every_attempt_is_archived_including_the_first(tmp_path):
    """Attempt 1 must not be overwritten by attempt 2: each attempt gets its

    own output and prompt archive, not just the final one.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    seen = []

    async def _announce(run_id, node_id, report, origin, **_):
        desk.resolve(node_id, "continue", "use the staging token")

    async def _judge(**kwargs):
        seen.append(kwargs)
        return Verdict(accomplished=len(seen) > 1)

    result = await _run_two_node_dag(tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce)

    store = DagRunStore(backend=LocalFileBackend(), root=str(tmp_path / "runs"), run_id=result.run_id)
    out_1 = await store.read_text(store.attempt_output_path("a", 1))
    out_2 = await store.read_text(store.attempt_output_path("a", 2))
    prompt_1 = await store.read_text(store.attempt_prompt_path("a", 1))
    prompt_2 = await store.read_text(store.attempt_prompt_path("a", 2))
    assert out_1 != out_2
    assert prompt_1 != prompt_2


async def test_a_raising_judge_fails_open_instead_of_crashing_the_node(tmp_path):
    """A judge callback is host code the same way announce_exception is --

    matches _verdict.judge's own documented fail-open contract instead of
    taking the node (and, in production, the whole gather) down with it.
    """

    async def _judge(**kwargs):
        raise RuntimeError("the judge provider is down")

    result = await _run_two_node_dag(tmp_path, desk=None, judge_node=_judge, announce_exception=None)
    assert result.summary["completed"] == 2


async def test_a_raising_announce_fails_the_node_instead_of_stranding_it(tmp_path, monkeypatch):
    """An undeliverable report must not leave the desk open for the full

    adjudication timeout blaming the agent for silence -- once the delivery
    retries are spent it fails the node, naming the real cause. The backoff is
    zeroed because what is under test is the outcome, not the wait.
    """
    from raven.agent.subagent import dag_adjudication as adj_mod
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    monkeypatch.setattr(adj_mod, "REPORT_DELIVERY_BACKOFF_S", 0.0)
    desk = AdjudicationDesk()

    async def _announce(run_id, node_id, report, origin, **_):
        raise RuntimeError("delivery channel is down")

    async def _judge(**kwargs):
        return Verdict(accomplished=False, category="missing_credential", what_is_missing="a token")

    result = await _run_two_node_dag(
        tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce, adjudication_timeout_s=0.3
    )
    assert result.summary["failed"] == 1
    files_by_node = {f["node"]: f for f in result.files}
    assert "delivery channel is down" in files_by_node["a"]["error"]
    assert not desk.is_open("a")


async def test_a_delivery_that_fails_once_is_retried_and_the_node_is_still_adjudicated(tmp_path):
    """Delivery is a transport, not a decision.

    The injected turn can lose a race with a gateway restart or a busy submit
    queue. Failing the node on the first hiccup throws away a node that could
    still have been adjudicated, and names the transport as the node's outcome.

    Safe to retry because the announcer is all-or-nothing: the only step after
    the injection is a fire-and-forget emit that swallows its own failure, so a
    raise means nothing was delivered. `test_a_failing_delivery_marker_does_not
    _fail_the_announce` in the manager suite pins that.
    """
    from raven.agent.subagent.dag_adjudication import CONTINUE, AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    attempts: list[str] = []
    judged: list[dict] = []

    async def _announce(run_id, node_id, report, origin, **_):
        attempts.append(node_id)
        if len(attempts) == 1:
            raise RuntimeError("the submit queue was busy")
        desk.resolve(node_id, CONTINUE, "use the staging token")

    async def _judge(**kwargs):
        judged.append(kwargs)
        return Verdict(accomplished=len(judged) > 1, category="missing_credential", what_is_missing="a token")

    result = await _run_two_node_dag(
        tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce, adjudication_timeout_s=5
    )

    assert attempts == ["a", "a"], f"the failed delivery has to be attempted again, got {attempts}"
    assert result.summary["completed"] == 2, (
        f"the second delivery landed, so the node was answered and continued: {result.summary}"
    )
    error = {f["node"]: f for f in result.files}["a"]["error"] or ""
    assert "could not be delivered" not in error, "a recovered delivery must not be recorded as the node's outcome"


async def test_delivery_retries_are_bounded_and_the_node_still_fails_on_the_last_one(tmp_path, monkeypatch):
    """Retrying is not waiting forever.

    A bound run has no adjudication deadline, so an unbounded retry loop here
    would spin for the life of the turn on a transport that is not coming back.
    The attempt count is the bound, and the last failure keeps the behaviour the
    undeliverable-report guard already had.
    """
    from raven.agent.subagent import dag_adjudication as adj_mod
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    monkeypatch.setattr(adj_mod, "REPORT_DELIVERY_BACKOFF_S", 0.0)
    desk = AdjudicationDesk()
    attempts: list[str] = []

    async def _announce(run_id, node_id, report, origin, **_):
        attempts.append(node_id)
        raise RuntimeError("delivery channel is down")

    async def _judge(**_kwargs):
        return Verdict(accomplished=False, category="missing_credential", what_is_missing="a token")

    result = await _run_two_node_dag(
        tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce, adjudication_timeout_s=0.3
    )

    assert len(attempts) == adj_mod.REPORT_DELIVERY_ATTEMPTS, (
        f"every attempt and no more: {len(attempts)} of {adj_mod.REPORT_DELIVERY_ATTEMPTS}"
    )
    assert result.summary["failed"] == 1
    error = {f["node"]: f for f in result.files}["a"]["error"] or ""
    assert "delivery channel is down" in error, "the transport's own words stay in the record"
    assert str(adj_mod.REPORT_DELIVERY_ATTEMPTS) in error, "and how many times it was tried"
    assert not desk.is_open("a"), "a report nobody received must not leave the node waiting"


async def test_a_node_does_not_suspend_on_a_report_it_cannot_deliver(tmp_path) -> None:
    """No origin means the announce cannot land, so the node must fail rather than wait.

    Suspending on an undeliverable report parks the graph for the whole adjudication
    timeout and then fails the node saying nobody answered, which blames the agent for
    a question it never received.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()

    async def _announce(run_id, node_id, report, origin, **_):
        raise AssertionError("the announce must not be attempted without an origin")

    async def _judge(**kwargs):
        return Verdict(accomplished=False, category="tool_failure", what_is_missing="a token")

    result = await _run_two_node_dag(tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce, origin=None)

    # The summary alone does not discriminate: without the fix the node still ends
    # `failed`, just one whole adjudication timeout later and blaming the agent. The
    # error text is what says which of the two happened.
    assert result.summary["failed"] == 1
    by_node = {f["node"]: f for f in result.files}
    assert by_node["a"]["error"] == "a token"
    assert "timed out" not in (by_node["a"]["error"] or "")
    assert desk.open_nodes() == set()


class _AttemptTaggingExec(_FakeExec):
    """Publishes a transcript naming this node's own call count, so a test can

    tell whether the judge read the attempt that just ran or one an earlier
    attempt left behind.
    """

    def __init__(self) -> None:
        super().__init__()
        self._calls_by_node: dict[str, int] = {}

    async def run(self, task: str, **kw) -> str:
        from raven.agent.subagent import activity

        task_id = kw["task_id"]
        call_n = self._calls_by_node.get(task_id, 0) + 1
        self._calls_by_node[task_id] = call_n
        activity.note_transcript([{"role": "tool", "name": "read", "content": f"node {task_id} attempt {call_n}"}])
        return await super().run(task, **kw)


async def test_the_judge_sees_the_current_attempts_transcript(tmp_path):
    """The judge must read what this attempt published, not an empty file on

    the first attempt or a previous attempt's leftovers on a continuation --
    both symptoms of calling it before the transcript for this attempt was
    written.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    seen: list[dict] = []

    async def _announce(run_id, node_id, report, origin, **_):
        desk.resolve(node_id, "continue", "use the staging token")

    async def _judge(*, node, store, output, error, crashed):
        try:
            text = await store.read_text(store.transcript_path(node.id))
        except Exception:
            text = ""
        evidence_complete = bool(text.strip())
        seen.append({"node": node.id, "evidence": text, "evidence_complete": evidence_complete})
        a_calls = [c for c in seen if c["node"] == "a"]
        if node.id == "a" and len(a_calls) == 1:
            return Verdict(accomplished=False, what_is_missing="needs another pass")
        return Verdict(accomplished=True)

    exec_backend = _AttemptTaggingExec()
    result = await _run_two_node_dag(
        tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce, exec_backend=exec_backend
    )

    assert result.summary["completed"] == 2
    a_calls = [c for c in seen if c["node"] == "a"]
    assert len(a_calls) == 2

    first, second = a_calls
    assert first["evidence_complete"] is True
    assert "attempt 1" in first["evidence"]
    assert second["evidence_complete"] is True
    assert "attempt 2" in second["evidence"]
    assert "attempt 1" not in second["evidence"]


async def test_each_attempts_transcript_survives_the_next_attempt(tmp_path):
    """The evidence a verdict rested on must stay readable after the retry.

    `transcript_path` is overwritten by design -- it is what the judge reads, and
    the judge judges the attempt in front of it. Without a per-attempt archive
    beside it, attempt 1's evidence is gone the moment attempt 2 runs, while that
    same attempt's prompt and output both remain: the design versions all three
    the same way.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    seen = []

    async def _announce(run_id, node_id, report, origin, **_):
        desk.resolve(node_id, "continue", "use the staging token")

    async def _judge(**kwargs):
        seen.append(kwargs)
        return Verdict(accomplished=len(seen) > 1)

    exec_backend = _AttemptTaggingExec()
    result = await _run_two_node_dag(
        tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce, exec_backend=exec_backend
    )
    assert result.summary["completed"] == 2

    store = DagRunStore(backend=LocalFileBackend(), root=str(tmp_path / "runs"), run_id=result.run_id)
    first = await store.read_text(store.attempt_transcript_path("a", 1))
    second = await store.read_text(store.attempt_transcript_path("a", 2))
    latest = await store.read_text(store.transcript_path("a"))

    assert "attempt 1" in first, "the retried attempt's own evidence is no longer retrievable"
    assert "attempt 2" in second
    assert "attempt 1" not in second
    # The overwrite-in-place file still holds the latest, which is what the judge
    # reads: the archive is beside it, not instead of it.
    assert latest == second


async def test_a_successful_continuation_clears_the_earlier_attempts_error(tmp_path):
    """A node whose first attempt is judged short and whose second attempt passes
    must not still carry the first attempt's reason once it reads `completed`.

    The manifest is what a poller actually reads, so a stale error surviving
    there -- not merely in the in-memory `errors` dict -- is the bug's real
    user-visible symptom.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    seen = []

    async def _announce(run_id, node_id, report, origin, **_):
        desk.resolve(node_id, "continue", "use the staging token")

    async def _judge(**kwargs):
        seen.append(kwargs)
        return Verdict(accomplished=len(seen) > 1)

    result = await _run_two_node_dag(tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce)
    assert result.summary["completed"] == 2

    store = DagRunStore(backend=LocalFileBackend(), root=str(tmp_path / "runs"), run_id=result.run_id)
    manifest = json.loads(await store.read_text(posixpath.join(store.run_dir, "manifest.json")))
    assert manifest["a"]["status"] == "completed"
    assert manifest["a"]["error"] is None


async def test_suspended_nodes_share_one_deadline_rather_than_queueing(tmp_path) -> None:
    """Four nodes waiting on a decision must cost one timeout, not four.

    Awaited in series they cost N times the window, which contradicts both the
    per-node deadline the report promises the agent and the configured cap.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    status = {}
    for i in range(4):
        nid = f"n{i}"
        desk.open(nid)
        status[nid] = "exception"

    started = time.perf_counter()
    await _await_adjudications(desk, status, {}, {}, timeout_s=0.2, cancel=None)
    elapsed = time.perf_counter() - started

    assert all(st == "failed" for st in status.values())
    # Generous against a loaded box, but far under the 0.8s four serial windows
    # would cost, so the ordering is what this measures rather than the machine.
    assert elapsed < 0.5, f"four nodes took {elapsed:.3f}s, which looks serial"


async def test_a_decision_restarts_the_window_for_the_nodes_still_waiting() -> None:
    """The window measures silence, not the whole round.

    Reports reach the agent one turn at a time -- the conversation lane is
    serial -- so a node still queued behind another spends none of its own
    window waiting to be asked. Three decisions, each well inside one window
    but together past it: against a fixed deadline for the round the last two
    time out having never reached the agent at all.
    """
    from raven.agent.subagent.dag_adjudication import CONTINUE, AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    status = {}
    for i in range(3):
        desk.open(f"n{i}")
        status[f"n{i}"] = "exception"
    errors: dict[str, str] = {}
    continuations: dict[str, str] = {}

    window = 0.4

    async def _decide() -> None:
        for i in range(3):
            await asyncio.sleep(window / 2)
            desk.resolve(f"n{i}", CONTINUE, f"try {i}")

    driver = asyncio.create_task(_decide())
    try:
        await _await_adjudications(desk, status, errors, continuations, timeout_s=window, cancel=None)
    finally:
        await driver

    assert status == {"n0": "pending", "n1": "pending", "n2": "pending"}
    assert continuations == {"n0": "try 0", "n1": "try 1", "n2": "try 2"}
    assert errors == {}


async def test_a_bound_run_waits_without_a_deadline_until_it_is_released() -> None:
    """While the turn that owns a foreground run is alive there is no clock.

    The decision lands well past the configured window but inside a window that
    starts at release; a wait clocked from entry would already have failed the
    node when the release arrived.
    """
    from raven.agent.subagent.dag_adjudication import CONTINUE, AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("n0")
    status = {"n0": "exception"}
    errors: dict[str, str] = {}
    continuations: dict[str, str] = {}
    released = asyncio.Event()

    async def _release_later() -> None:
        await asyncio.sleep(0.3)
        released.set()

    async def _decide_later() -> None:
        await asyncio.sleep(0.4)
        desk.resolve("n0", CONTINUE, "go on")

    side = [asyncio.create_task(_release_later()), asyncio.create_task(_decide_later())]
    try:
        await _await_adjudications(desk, status, errors, continuations, timeout_s=0.2, cancel=None, released=released)
    finally:
        await asyncio.gather(*side)

    assert status == {"n0": "pending"}
    assert continuations == {"n0": "go on"}
    assert errors == {}


async def test_the_window_starts_when_the_run_is_released() -> None:
    """Released and then ignored, the node fails one window after the release, not after entry."""
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("n0")
    status = {"n0": "exception"}
    errors: dict[str, str] = {}
    released = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, released.set)

    started = time.perf_counter()
    await _await_adjudications(desk, status, errors, {}, timeout_s=0.2, cancel=None, released=released)
    elapsed = time.perf_counter() - started

    assert status == {"n0": "failed"}
    assert "timed out" in errors["n0"]
    # The node waited twice the window here, so a message naming the window as the
    # node's whole wait would be a false claim. It reports the silence instead.
    assert "Nothing was decided for 0.2s" in errors["n0"], errors["n0"]
    assert 0.35 < elapsed < 0.9, f"expected release (0.2s) + window (0.2s), got {elapsed:.3f}s"


async def test_an_already_released_run_is_clocked_from_entry() -> None:
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("n0")
    status = {"n0": "exception"}
    errors: dict[str, str] = {}
    released = asyncio.Event()
    released.set()

    started = time.perf_counter()
    await _await_adjudications(desk, status, errors, {}, timeout_s=0.2, cancel=None, released=released)
    elapsed = time.perf_counter() - started

    assert status == {"n0": "failed"}
    assert elapsed < 0.5


async def test_a_decision_landing_while_unclocked_leaves_the_wait_unclocked() -> None:
    """Two open nodes, still bound: resolving one must not start a clock for the other.

    A regression to an unconditional ``deadline = loop.time() + timeout_s`` on any
    decided node -- dropping the ``still_bound`` check -- would clock n1 the moment
    n0 resolves, and n1 would time out long before release ever arrives.
    """
    from raven.agent.subagent.dag_adjudication import CONTINUE, AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("n0")
    desk.open("n1")
    status = {"n0": "exception", "n1": "exception"}
    errors: dict[str, str] = {}
    continuations: dict[str, str] = {}
    released = asyncio.Event()

    async def _resolve_n0_early() -> None:
        await asyncio.sleep(0.05)
        desk.resolve("n0", CONTINUE, "go on n0")

    async def _release_and_resolve_n1_past_the_window() -> None:
        await asyncio.sleep(0.4)
        released.set()
        desk.resolve("n1", CONTINUE, "go on n1")

    side = [
        asyncio.create_task(_resolve_n0_early()),
        asyncio.create_task(_release_and_resolve_n1_past_the_window()),
    ]
    try:
        await _await_adjudications(desk, status, errors, continuations, timeout_s=0.2, cancel=None, released=released)
    finally:
        await asyncio.gather(*side)

    assert status == {"n0": "pending", "n1": "pending"}
    assert continuations == {"n0": "go on n0", "n1": "go on n1"}
    assert errors == {}


def test_the_report_has_one_shape_addressed_to_the_agent():
    """Both lanes hand the report to the main agent now, so there is one text.

    The deadline line and the resolve_dag_node line are what a person being asked
    synchronously could not use; nobody is asked that way any more.
    """
    from raven.agent.subagent.dag_runner import _exception_report
    from raven.agent.subagent.dag_verdict import Verdict

    spec = parse_dag_spec(
        {
            "task_summary": "one node",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "first", "prompt_template": "do a"}],
        }
    )
    shared = {
        "run_id": "r1",
        "node": spec.nodes[0],
        "verdict": Verdict(accomplished=False, category="missing_credential", what_is_missing="a token"),
        "attempt": 1,
        "remaining": 2,
        "blocked": ["b"],
        "timeout_s": 600.0,
    }

    report = _exception_report(**shared)

    assert "deciding within 600s" in report
    assert "restarted by each decision" in report
    assert "resolve_dag_node" in report
    for kept in ("missing_credential", "a token", "2 continuation(s) left", "blocked while this waits: b"):
        assert kept in report
    with pytest.raises(TypeError):
        _exception_report(**shared, answered_in_turn=True)


class _StepsThenSilentExec(_FakeExec):
    """Publishes steps on a node's first attempt and nothing on the next.

    The OpenAI backend's own shape: an answer given directly, with no
    reasoning steps, publishes an empty transcript rather than no transcript.
    """

    def __init__(self) -> None:
        super().__init__()
        self._calls_by_node: dict[str, int] = {}

    async def run(self, task: str, **kw) -> str:
        from raven.agent.subagent import activity

        task_id = kw["task_id"]
        call_n = self._calls_by_node.get(task_id, 0) + 1
        self._calls_by_node[task_id] = call_n
        activity.note_transcript(
            [{"role": "tool", "name": "read", "content": f"node {task_id} attempt {call_n}"}] if call_n == 1 else []
        )
        return await super().run(task, **kw)


async def test_an_empty_attempt_does_not_inherit_the_previous_transcript(tmp_path):
    """An attempt that published nothing must not be judged on the last one's evidence.

    Skipping the write for an empty list leaves the previous attempt's file in
    place, and `_node_evidence` reads that file: the attempt-2 judge is handed
    attempt 1's steps and told the evidence is complete. The attempt-2 archive
    the design promises is missing for the same reason.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    seen = []

    async def _announce(run_id, node_id, report, origin, **_):
        desk.resolve(node_id, "continue", "answer it directly this time")

    async def _judge(**kwargs):
        seen.append(kwargs)
        return Verdict(accomplished=len(seen) > 1)

    result = await _run_two_node_dag(
        tmp_path,
        desk=desk,
        judge_node=_judge,
        announce_exception=_announce,
        exec_backend=_StepsThenSilentExec(),
    )
    assert result.summary["completed"] == 2

    store = DagRunStore(backend=LocalFileBackend(), root=str(tmp_path / "runs"), run_id=result.run_id)
    latest = await store.read_text(store.transcript_path("a"))
    assert latest.strip() == "", "attempt 2 published nothing, so the file the judge reads must say nothing"
    assert "attempt 1" not in latest

    first = await store.read_text(store.attempt_transcript_path("a", 1))
    second = await store.read_text(store.attempt_transcript_path("a", 2))
    assert "attempt 1" in first, "the earlier attempt's own evidence stays readable"
    assert second.strip() == "", "the promised attempt-2 archive exists and is honestly empty"


async def test_a_node_that_names_an_instance_runs_at_that_instance_s_mode() -> None:
    """The effort level a user set on an instance reaches the graph, on the same
    terms the message list does: a node naming an `instance` is asking to
    continue that conversation, and the mode is part of what it continues."""
    execu = _FakeExec()
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "continue the researcher",
                    "prompt_template": "hello",
                    "instance": "researcher",
                },
                {"id": "b", "subagent": "x", "node_summary": "a fresh session", "prompt_template": "hello"},
            ],
        }
    )

    await run_dag(
        spec,
        resolve=_by_name({"x": execu}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="cli:direct",
        mode_for=lambda skey, agent, instance: {"researcher": "deep"}.get(instance),
    )

    by_id = {call["task_id"]: call for call in execu.calls}
    assert by_id["a"]["mode"] == "deep"
    # An unnamed node is asked about no instance, so there is nothing to inherit:
    # its handle is its own node id, which is not a conversation anyone named.
    assert by_id["b"]["mode"] is None


async def test_a_runner_with_no_mode_source_dispatches_without_one() -> None:
    """The unwired host (tests, an offline entry point) is the pre-modes
    behaviour unchanged, not a crash and not a guess."""
    execu = _FakeExec()
    spec = parse_dag_spec(
        {
            "task_summary": "run the graph under test",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "continue the researcher",
                    "prompt_template": "hello",
                    "instance": "researcher",
                }
            ],
        }
    )

    await run_dag(
        spec,
        resolve=_by_name({"x": execu}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="cli:direct",
    )

    assert execu.calls[0]["mode"] is None


async def test_an_unstarted_background_run_retires_every_per_run_entry(tmp_path: Path) -> None:
    """[leak] A task cancelled before its first tick never enters _run, whose
    finally is the normal retirement site. The done-callback must retire the
    per-run entries too, or active_run_ids() lists a dead run forever, its
    node rows stay pinned running, and request_cancel claims a live run."""
    import asyncio

    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )

    async def _never(*_a, **_kw):
        await asyncio.sleep(60)

    tool._run_detached = _never

    await tool.execute(
        task_summary="background run for the leak probe",
        nodes=[{"id": "a", "subagent": "echo", "node_summary": "n", "prompt_template": "p"}],
        background=True,
    )
    (run_id,) = list(tool._runs)
    assert run_id in tool._cancels

    tool._runs[run_id].cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert run_id not in tool._runs
    assert run_id not in tool._cancels, "an unstarted run must not pin liveness forever"
    assert run_id not in tool._desks


def test_the_agent_report_names_the_route_not_just_the_call():
    """`resolve_dag_node` is schema-hidden, so naming it alone is not an instruction.

    The model looks for the name in its own tool list, does not find it, and reports
    that it has no way to answer -- the node then waits out its whole timeout. The
    report has to spell the invocation the model can actually issue.
    """
    from raven.agent.subagent.dag_runner import _exception_report
    from raven.agent.subagent.dag_verdict import Verdict

    spec = parse_dag_spec(
        {
            "task_summary": "one node",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "first", "prompt_template": "do a"}],
        }
    )
    report = _exception_report(
        run_id="r1",
        node=spec.nodes[0],
        verdict=Verdict(accomplished=False, category="missing_credential", what_is_missing="a token"),
        attempt=1,
        remaining=2,
        blocked=["b"],
        timeout_s=600.0,
    )

    assert "tool_call" in report, "the only route to a schema-hidden tool has to be named"
    assert "resolve_dag_node" in report, "and the name tool_call is to forward to"
    assert "not in your tool list" in report, (
        "a model that just failed to find the name needs to be told why, or it reads "
        "the instruction as stale rather than as one it can act on"
    )
    # The arguments still have to be answerable without guessing the schema.
    for field in ("run_id", "node_id", "decision", "continue", "abandon", "r1"):
        assert field in report


async def _run_with_reachability(tmp_path, reachable, *, timeout_s=30):
    """One suspending node, run with the given reachability predicate.

    The timeout is long on purpose: the bug this guards is that the node waits
    it out, so a short one would let the broken path pass on elapsed time.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    announced: list[tuple] = []

    async def _announce(run_id, node_id, report, origin, **_):
        announced.append((run_id, node_id, report, origin))

    async def _judge(**kwargs):
        return Verdict(accomplished=False, category="tool_failure", what_is_missing="a token")

    started = time.monotonic()
    result = await _run_two_node_dag(
        tmp_path,
        desk=desk,
        judge_node=_judge,
        announce_exception=_announce,
        adjudication_timeout_s=timeout_s,
        control_reachable=reachable,
    )
    return result, desk, announced, time.monotonic() - started


async def test_a_node_does_not_suspend_when_no_route_can_answer_it(tmp_path) -> None:
    """`tool_call` gone means `resolve_dag_node` is unnameable, so no answer can arrive.

    Suspending anyway parks the node for the whole adjudication timeout and then
    records that nobody answered -- blaming the agent for a question it had no way
    to answer. The same reasoning the undeliverable-report guard already applies to
    the outbound leg, applied to the return leg.
    """
    result, desk, _announced, elapsed = await _run_with_reachability(tmp_path, lambda: False)

    assert result.summary["failed"] == 1
    by_node = {f["node"]: f for f in result.files}
    error = by_node["a"]["error"] or ""
    assert "no route" in error.lower(), f"the error must name the real cause, got: {error!r}"
    assert "tool_call" in error, "and point at the switch that causes it"
    assert "timed out" not in error, "the agent must not be blamed for the host's missing route"
    assert desk.open_nodes() == set()
    assert elapsed < 10, f"the node must fail at once, not wait out the timeout (took {elapsed:.1f}s)"


async def test_a_raising_reachability_predicate_fails_the_node_closed(tmp_path) -> None:
    """Fail closed, matching how the acceptance text already treats a raising predicate."""
    result, desk, _announced, elapsed = await _run_with_reachability(
        tmp_path, lambda: (_ for _ in ()).throw(AttributeError("no controller"))
    )

    assert result.summary["failed"] == 1
    by_node = {f["node"]: f for f in result.files}
    assert "no route" in (by_node["a"]["error"] or "").lower()
    assert desk.open_nodes() == set()
    assert elapsed < 10


async def test_an_unwired_reachability_predicate_still_suspends(tmp_path) -> None:
    """`None` means the host never wired it, which must keep meaning "assume reachable".

    Every pre-existing runner fixture omits the predicate; reading absent as
    unreachable would silently turn all of them into immediate failures.
    """
    result, desk, announced, _elapsed = await _run_with_reachability(tmp_path, None, timeout_s=2)

    assert len(announced) == 1, "the report still goes out when reachability is unknown"
    assert "no route" not in ({f["node"]: f for f in result.files}["a"]["error"] or "").lower(), (
        "an unwired predicate must not be read as a missing route"
    )
    assert desk.open_nodes() == set()


async def test_the_tool_hands_its_reachability_predicate_to_the_runner(tmp_path, monkeypatch):
    """The runner cannot ask whether an answer can come back unless it is given the predicate.

    The tool has held it since the acceptance text needed it; without this the
    runner defaults to "assume reachable" and the fail-fast path is dead code in
    production however well it is covered by unit tests.
    """
    from raven.agent.subagent import dag_tool as tool_mod

    seen = []

    async def _fake_run_dag(spec, **kwargs):
        seen.append(kwargs.get("control_reachable"))
        raise RuntimeError("far enough")

    monkeypatch.setattr(tool_mod, "run_dag", _fake_run_dag)

    def _predicate() -> bool:
        return False

    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        control_reachable=_predicate,
    )
    tool.set_context("web", "default", "web:sess1")
    node = {"id": "a", "subagent": "echo", "node_summary": "say hello", "prompt_template": "hi"}

    await tool.execute(task_summary="backgrounded", nodes=[node])
    await asyncio.gather(*list(tool._runs.values()), return_exceptions=True)

    assert seen == [_predicate], "the runner must get the tool's own predicate, not a copy of the fold condition"


async def test_the_acceptance_text_names_the_route_to_the_hidden_controls(tmp_path) -> None:
    """Advertising `dag_status(...)` names a tool absent from the model's schema.

    The acceptance text is the only advertisement these get, so it has to say how
    they are reached, the same way the exception report does.
    """
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        control_reachable=lambda: True,
    )
    tool.set_context("web", "default", "web:sess1")

    out = await tool.execute(
        task_summary="run the graph under test",
        nodes=[{"id": "a", "subagent": "echo", "node_summary": "say hello", "prompt_template": "hi"}],
    )

    assert "tool_call" in out.model_text, "the controls are unnameable without the route"
    assert "dag_status" in out.model_text
    assert "cancel_dag" in out.model_text
    assert "resolve_dag_node" in out.model_text


def test_the_report_names_no_route_when_there_is_none() -> None:
    """A node with no answer route is failed, not suspended -- the report must say so.

    The announce is not gated on suspension, so this report reaches the model
    anyway. Naming an invocation in it asks the model to answer a node that no
    longer accepts an answer, through a route the host does not have: the same
    dishonest advertisement this branch exists to remove, in the state the branch
    itself introduced.
    """
    from raven.agent.subagent.dag_runner import _exception_report
    from raven.agent.subagent.dag_verdict import Verdict

    spec = parse_dag_spec(
        {
            "task_summary": "one node",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "first", "prompt_template": "do a"}],
        }
    )
    shared = {
        "run_id": "r1",
        "node": spec.nodes[0],
        "verdict": Verdict(accomplished=False, category="tool_failure", what_is_missing="a token"),
        "attempt": 1,
        "remaining": 2,
        "blocked": ["b"],
        "timeout_s": 600.0,
    }

    routed = _exception_report(**shared)
    unrouted = _exception_report(**shared, route_available=False)

    assert "tool_call" in routed, "the control: with a route, the invocation is named"

    assert "resolve_dag_node" not in unrouted, "no route means no call to name"
    assert "Answer" not in unrouted, "and no instruction to answer something that accepts no answer"
    # Naming tool_call as the *reason* is diagnostic, not an instruction: it points
    # whoever reads this at the switch that caused it.
    assert "tool_call is not available" in unrouted
    assert "deciding within" not in unrouted, "there is no deadline: the node is not waiting"
    assert "no route" in unrouted.lower(), "it has to say why this line of the graph is dead"
    # What the decision would have turned on is still reported, because the agent
    # may still re-plan around the dead node.
    for kept in ("tool_failure", "a token", "blocked while this waits: b"):
        assert kept in unrouted


@pytest.mark.parametrize(
    ("kwargs", "expected", "why"),
    [
        ({}, True, "a node that suspends really is waiting for a decision"),
        ({"max_continuations": 0}, False, "the continuation limit is spent, so the desk never opens"),
        ({"control_reachable": lambda: False}, False, "no route means the desk never opens either"),
    ],
)
async def test_the_announcer_is_told_whether_a_decision_is_pending(tmp_path, kwargs, expected, why) -> None:
    """The flag has to track suspension, not the fact that a report exists.

    The announce is not gated on suspension -- a terminal node's report still goes
    out, because the agent replans from it -- so the announcer is the only place
    that can tell the two apart, and it can only do that if the runner says which
    this is.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    seen: list[bool] = []

    async def _announce(run_id, node_id, report, origin, *, awaiting_decision):
        seen.append(awaiting_decision)

    async def _judge(**_kw):
        return Verdict(accomplished=False, category="tool_failure", what_is_missing="a token")

    await _run_two_node_dag(
        tmp_path,
        desk=AdjudicationDesk(),
        judge_node=_judge,
        announce_exception=_announce,
        adjudication_timeout_s=1,
        **kwargs,
    )

    assert seen, "the report goes out in every one of these states"
    assert seen[0] is expected, why


async def test_the_no_route_failure_keeps_the_judge_s_reason(tmp_path) -> None:
    """Two different facts, and the run record is the only place both reach a node's reader.

    `errors` becomes the run record's `error` field, which the manifest carries and
    `dag_status` renders, so replacing the judge's finding with the routing
    explanation keeps "why nobody could be asked" and loses "why the node failed"
    for anyone asking about the node. The injected report carries both and is durable
    in its own right, as the turn it was injected as -- but it is found by reading the
    conversation rather than the run.
    """
    result, _desk, _announced, _elapsed = await _run_with_reachability(tmp_path, lambda: False)

    error = {f["node"]: f for f in result.files}["a"]["error"] or ""
    assert "a token" in error, "the judge said what was missing; the record has to keep it"
    assert "no route" in error.lower(), "and why it could not be adjudicated"
    assert "timed out" not in error


# --- a foreground run hands its reports to the turn awaiting it ------------------


def _foreground_tool(tmp_path: Path, verdicts: list[Any], **kwargs: Any) -> SubAgentDagTool:
    """A graph tool over the real ``cat`` sub-agent, judged by a scripted verdict list.

    Each judge call pops the next verdict; once the list is spent every node is
    accomplished. ``cat`` echoes the prompt, so a node finishes in milliseconds
    and the test spends its time on the handoff, which is what is under test.
    """
    from raven.agent.subagent.dag_verdict import Verdict

    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        **kwargs,
    )
    tool.set_context("web", "default", "web:fg")
    script = list(verdicts)

    async def _judge(**_kwargs: Any) -> Verdict:
        return script.pop(0) if script else Verdict(accomplished=True)

    tool._judge_node = lambda: _judge  # type: ignore[method-assign]
    return tool


def _falls_short(what: str = "a token"):
    from raven.agent.subagent.dag_verdict import Verdict

    return Verdict(accomplished=False, category="missing_credential", what_is_missing=what)


_CHAIN = [
    {"id": "a", "subagent": "echo", "node_summary": "first", "prompt_template": "do a"},
    {"id": "b", "subagent": "echo", "node_summary": "second", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
]
_PAIR = [
    {"id": "a", "subagent": "echo", "node_summary": "first", "prompt_template": "do a"},
    {"id": "b", "subagent": "echo", "node_summary": "second", "prompt_template": "do b"},
]
_SOLO = [{"id": "a", "subagent": "echo", "node_summary": "only", "prompt_template": "do a"}]


async def test_a_foreground_call_returns_the_first_report_while_the_graph_keeps_running(tmp_path: Path) -> None:
    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short()])

        out = await tool.execute(task_summary="fg", nodes=_CHAIN, background=False)

        text = out.model_text
        assert "did not accomplish its task" in text and "a token" in text
        assert "resolve_dag_node" in text
        assert "returned before the graph finished" in text, "the agent is told the protocol"
        (run_id,) = list(tool._runs)
        assert not tool._runs[run_id].done(), "the graph is still running"
        assert tool.is_foreground(run_id)


async def test_resolving_the_node_and_awaiting_returns_the_final_result(tmp_path: Path) -> None:
    from raven.agent.subagent.dag_adjudication import Final

    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short()])
        await tool.execute(task_summary="fg", nodes=_CHAIN, background=False)
        (run_id,) = list(tool._runs)

        assert tool.resolve_node(run_id, "a", "continue", "use the staging token")
        event = await tool.await_run(run_id)

        assert isinstance(event, Final) and not event.stopped
        final = tool.render_event(run_id, event)
        assert "2 completed" in final.model_text
        assert await tool.await_run(run_id) in (event, None), "after the run ends there is nothing more to wait for"


async def test_a_second_suspended_node_is_handed_over_on_the_next_await(tmp_path: Path) -> None:
    from raven.agent.subagent.dag_adjudication import Final, Report

    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short("token a"), _falls_short("token b")])
        first = await tool.execute(task_summary="fg", nodes=_PAIR, background=False)
        (run_id,) = list(tool._runs)
        first_node = "a" if "node 'a'" in first.model_text else "b"
        other = "b" if first_node == "a" else "a"

        assert tool.resolve_node(run_id, first_node, "continue", "here you go")
        second = await tool.await_run(run_id)

        assert isinstance(second, Report) and second.node_id == other, "one report per take, the other node's"
        assert tool.resolve_node(run_id, other, "abandon", None)
        final = await tool.await_run(run_id)
        assert isinstance(final, Final)
        assert "1 completed" in tool.render_event(run_id, final).model_text
        assert "1 failed" in tool.render_event(run_id, final).model_text


async def test_a_foreground_run_with_no_suspension_returns_the_summary_as_before(tmp_path: Path) -> None:
    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [])
        out = await tool.execute(task_summary="fg", nodes=_CHAIN, background=False)
        assert "2 completed" in out.model_text
        assert "returned before the graph finished" not in out.model_text
        assert not tool._runs, "a finished run leaves no task behind"
        assert not tool._outboxes


async def test_a_terminal_report_does_not_return_the_blocking_call(tmp_path: Path) -> None:
    """A node that exhausts its continuations is failed, not suspended: the call that
    resolves its last real suspension must reach the run's summary, not a report the
    agent can never resolve."""
    from raven.agent.subagent.dag_adjudication import Final
    from raven.config.raven import SubagentDagConfig

    async with draining_dag_runs():
        tool = _foreground_tool(
            tmp_path,
            [_falls_short(), _falls_short()],
            verdict_config=SubagentDagConfig(max_continuations=1),
        )
        first = await tool.execute(task_summary="fg", nodes=_CHAIN[:1], background=False)
        assert "returned before the graph finished" in first.model_text, "the real suspension carries the tail"
        (run_id,) = list(tool._runs)

        assert tool.resolve_node(run_id, "a", "continue", "use the staging token")
        event = await tool.await_run(run_id)

        assert isinstance(event, Final), f"expected the run's summary, got {event!r}"
        final = tool.render_event(run_id, event)
        assert "finished:" in final.model_text
        assert "returned before the graph finished" not in final.model_text


async def test_a_backgrounded_run_still_announces_a_terminal_report(tmp_path: Path) -> None:
    """The suspended-only guard belongs to the foreground closure alone: a background
    run's announcer must still learn that a node exhausted its continuations."""
    from raven.config.raven import SubagentDagConfig

    reported: list[str] = []

    async def _announce_exception(run_id: str, node_id: str, report: str, origin: dict, **_kwargs: Any) -> None:
        reported.append(node_id)

    async with draining_dag_runs():
        tool = _foreground_tool(
            tmp_path,
            [_falls_short(), _falls_short()],
            announce_exception=_announce_exception,
            verdict_config=SubagentDagConfig(max_continuations=1),
        )
        await tool.execute(task_summary="bg", nodes=_CHAIN[:1], background=True)
        (run_id,) = list(tool._runs)
        for _ in range(300):
            if reported:
                break
            await asyncio.sleep(0.01)

        assert tool.resolve_node(run_id, "a", "continue", "use the staging token")
        for _ in range(300):
            if len(reported) >= 2:
                break
            await asyncio.sleep(0.01)

        assert reported == ["a", "a"], "both the suspension and the terminal report reach the announcer"


async def test_cancelling_the_awaiting_call_cancels_the_run(tmp_path: Path) -> None:
    class _Sleeper:
        kind = "raven-loop"

        async def run(self, task: str, **_kwargs: Any) -> str:
            await asyncio.sleep(60)
            return "never"

    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [])
        tool._resolve_node = lambda node: _Sleeper()  # type: ignore[method-assign]
        call = asyncio.create_task(tool.execute(task_summary="fg", nodes=_CHAIN[:1], background=False))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if tool._runs:
                break
        (run_id,) = list(tool._runs)
        run_task = tool._runs[run_id]

        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.gather(run_task, return_exceptions=True)

        assert run_task.cancelled()
        assert run_id not in tool._runs and run_id not in tool._outboxes


# --- a turn's end releases the foreground runs it still binds --------------------


async def _both_suspended(tool: SubAgentDagTool, run_id: str) -> None:
    for _ in range(300):
        desk = tool._desks.get(run_id)
        if desk is not None and desk.open_nodes() == {"a", "b"}:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("both nodes should have suspended by now")


async def test_release_re_sends_the_unanswered_report_and_clocks_the_run(tmp_path: Path) -> None:
    """A released foreground run becomes a backgrounded one, with full information."""
    from raven.config.raven import SubagentDagConfig

    re_sent: list[tuple[str, str, dict]] = []
    results: list[str] = []

    async def _announce_exception(run_id: str, node_id: str, report: str, origin: dict, **_kwargs: Any) -> None:
        re_sent.append((node_id, report, origin))

    async def _announce(run_id: str, summary: str, origin: dict) -> None:
        results.append(summary)

    async with draining_dag_runs():
        tool = _foreground_tool(
            tmp_path,
            [_falls_short("token a"), _falls_short("token b")],
            announce=_announce,
            announce_exception=_announce_exception,
            verdict_config=SubagentDagConfig(adjudication_timeout_seconds=0.3),
        )
        first = await tool.execute(task_summary="fg", nodes=_PAIR, background=False)
        (run_id,) = list(tool._runs)
        run_task = tool._runs[run_id]
        await _both_suspended(tool, run_id)
        handed = "a" if "node 'a'" in first.model_text else "b"
        buffered = "b" if handed == "a" else "a"

        await tool.release_turn("web:fg", flush=True)

        assert not tool.is_foreground(run_id)
        assert [node for node, _, _ in re_sent] == [handed, buffered], (
            "both are unanswered: the turn took one and never decided it, and never saw the other"
        )
        assert re_sent[0][2]["session_key"] == "web:fg"
        assert "returned before the graph finished" not in re_sent[0][1], "an announced report carries no tail"

        await asyncio.wait_for(run_task, timeout=5)
        assert len(results) == 1 and "2 failed" in results[0], (
            "clocked after release, both nodes time out and the summary announces"
        )


async def test_release_re_sends_a_report_the_turn_took_but_never_answered(tmp_path: Path) -> None:
    """A take is not an answer.

    One node, so nothing else can be re-sent in its place. The turn was handed the
    report and ended without deciding; the node is still open on the desk and no
    other route will ever ask about it again, which is what the returned tail
    promises and what a backgrounded run would have done.
    """
    from raven.config.raven import SubagentDagConfig

    re_sent: list[str] = []

    async def _announce_exception(run_id: str, node_id: str, report: str, origin: dict, **_kwargs: Any) -> None:
        re_sent.append(node_id)

    async def _announce(run_id: str, summary: str, origin: dict) -> None:
        return None

    async with draining_dag_runs():
        tool = _foreground_tool(
            tmp_path,
            [_falls_short("token a")],
            announce=_announce,
            announce_exception=_announce_exception,
            verdict_config=SubagentDagConfig(adjudication_timeout_seconds=0.3),
        )
        first = await tool.execute(task_summary="fg", nodes=_SOLO, background=False)
        assert "node 'a'" in first.model_text
        run_task = tool._runs[next(iter(tool._runs))]

        await tool.release_turn("web:fg", flush=True)

        assert re_sent == ["a"], "the turn took this report and never answered it"
        await asyncio.wait_for(run_task, timeout=5)


async def test_release_does_not_re_send_a_report_the_turn_answered(tmp_path: Path) -> None:
    """The guard on the other side: a decided node must not be asked about twice."""
    from raven.agent.subagent.dag_adjudication import CONTINUE
    from raven.config.raven import SubagentDagConfig

    re_sent: list[str] = []

    async def _announce_exception(run_id: str, node_id: str, report: str, origin: dict, **_kwargs: Any) -> None:
        re_sent.append(node_id)

    async def _announce(run_id: str, summary: str, origin: dict) -> None:
        return None

    async with draining_dag_runs():
        tool = _foreground_tool(
            tmp_path,
            [_falls_short("token a")],
            announce=_announce,
            announce_exception=_announce_exception,
            verdict_config=SubagentDagConfig(adjudication_timeout_seconds=0.3),
        )
        await tool.execute(task_summary="fg", nodes=_SOLO, background=False)
        run_id = next(iter(tool._runs))
        run_task = tool._runs[run_id]
        assert tool.resolve_node(run_id, "a", CONTINUE, "here is the token")

        await tool.release_turn("web:fg", flush=True)

        assert re_sent == [], "an answered report is not re-sent"
        await asyncio.wait_for(run_task, timeout=5)


async def test_release_without_flush_drops_the_report_but_still_announces_the_result(tmp_path: Path) -> None:
    from raven.config.raven import SubagentDagConfig

    re_sent: list[str] = []
    results: list[str] = []

    async def _announce_exception(run_id: str, node_id: str, report: str, origin: dict, **_kwargs: Any) -> None:
        re_sent.append(node_id)

    async def _announce(run_id: str, summary: str, origin: dict) -> None:
        results.append(summary)

    async with draining_dag_runs():
        tool = _foreground_tool(
            tmp_path,
            [_falls_short("token a"), _falls_short("token b")],
            announce=_announce,
            announce_exception=_announce_exception,
            verdict_config=SubagentDagConfig(adjudication_timeout_seconds=0.3),
        )
        await tool.execute(task_summary="fg", nodes=_PAIR, background=False)
        (run_id,) = list(tool._runs)
        run_task = tool._runs[run_id]
        await _both_suspended(tool, run_id)

        await tool.release_turn("web:fg", flush=False)

        await asyncio.wait_for(run_task, timeout=5)
        assert re_sent == []
        assert len(results) == 1


async def test_a_released_run_announces_a_terminal_report_as_a_notification(tmp_path: Path) -> None:
    """After the release there is no blocking call for the summary to reach.

    Dropping a non-suspending report is right while the run is bound: the call is
    still waiting and the summary is on its way to it. Released, that call is gone,
    so a dropped terminal report reaches nobody until the whole graph finishes --
    while `CONTEXT.md` promises a released run behaves as a backgrounded one, whose
    announcer takes notifications as readily as questions.
    """
    from raven.agent.subagent.dag_adjudication import CONTINUE
    from raven.config.raven import SubagentDagConfig

    seen: list[tuple[str, bool]] = []

    async def _announce_exception(
        run_id: str, node_id: str, report: str, origin: dict, *, awaiting_decision: bool = True
    ) -> None:
        seen.append((node_id, awaiting_decision))

    async def _announce(run_id: str, summary: str, origin: dict) -> None:
        return None

    async with draining_dag_runs():
        tool = _foreground_tool(
            tmp_path,
            [_falls_short("token a"), _falls_short("token a still")],
            announce=_announce,
            announce_exception=_announce_exception,
            verdict_config=SubagentDagConfig(max_continuations=1, adjudication_timeout_seconds=5.0),
        )
        await tool.execute(task_summary="fg", nodes=_SOLO, background=False)
        run_id = next(iter(tool._runs))
        run_task = tool._runs[run_id]

        await tool.release_turn("web:fg", flush=True)
        assert seen == [("a", True)], "the suspension replays on release, as promised"

        assert tool.resolve_node(run_id, "a", CONTINUE, "use the staging token")
        await asyncio.wait_for(run_task, timeout=10)

    assert seen == [("a", True), ("a", False)], (
        "the retry ended terminally after the release, so its report is a notification the "
        "released lane must still announce"
    )


async def test_resolving_a_queued_node_is_answered_with_the_other_node_not_its_own(tmp_path: Path) -> None:
    """The model-facing shape of the retirement rule, against a real outbox.

    Both nodes suspend; the blocking call is handed one and the other queues. The
    agent can still decide the queued one -- the report it did get names the nodes
    blocked behind it, and `dag_status` lists them. Answering it must not be
    followed by that same node's question.

    Driven through the `resolve_node` / `await_run` pair `resolve_dag_node` itself
    calls, because that tool's own tests stand on a stub loop and a stub stops
    exactly where this defect lives.

    What comes back instead is the other node's report: it was handed once, nobody
    decided it, and `Outbox` records exactly that debt. An earlier version of this
    test asserted a timeout here, on the reading that a retired report leaves
    nothing to hand over. A timeout cannot tell "the decided node came back" from
    "nothing came back at all", and what it blessed was a park that production has
    no clock to end.
    """
    from raven.agent.subagent.dag_adjudication import CONTINUE

    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short("token a"), _falls_short("token b")])
        first = await tool.execute(task_summary="fg", nodes=_PAIR, background=False)
        run_id = next(iter(tool._runs))
        try:
            await _both_suspended(tool, run_id)
            handed = "a" if "node 'a'" in first.model_text else "b"
            queued = "b" if handed == "a" else "a"

            assert tool.resolve_node(run_id, queued, CONTINUE, "here is what was missing")

            try:
                event = await asyncio.wait_for(tool.await_run(run_id), timeout=5)
            except asyncio.TimeoutError:
                raise AssertionError(
                    f"nothing came back, and nothing ever will: node {handed!r} was handed once and is "
                    "still undecided, so no node can suspend and the run cannot finish"
                ) from None
            assert getattr(event, "node_id", None) == handed, (
                f"the next question is the node still open, not {getattr(event, 'node_id', event)!r}"
            )
        finally:
            tool.request_cancel(run_id)


async def test_deciding_the_node_the_call_was_not_handed_still_ends_the_tool_call(tmp_path: Path) -> None:
    """The turn comes back, driven through the caller the model actually reaches.

    Two nodes suspend, one report goes to the blocking call and the other queues,
    and the agent decides the queued one. That is an ordinary decision: the report
    it holds names the nodes blocked behind it and `dag_status` lists them.

    Deciding it used to park the call with nothing able to wake it. Nothing can
    suspend while the runner waits -- it waits only once nothing else can run --
    so no report can arrive; the run cannot finish while a node is open, so no
    final can either; and a bound run has no deadline. `blocking_for` keeps the
    registry's ceiling off the call, asserted below, so "parked" meant "until
    something kills the turn" -- and the cancel handler aborts the whole graph.

    The outbox level is covered by `test_resolving_a_queued_node_is_answered_with_
    the_other_node_not_its_own`. This drives `ResolveDagNodeTool.execute`, because
    the absence of a ceiling is a property of the caller and the registry, not of
    the outbox.
    """
    from types import SimpleNamespace

    from raven.agent.subagent.dag_control_tools import ResolveDagNodeTool

    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short("token a"), _falls_short("token b")])
        first = await tool.execute(task_summary="fg", nodes=_PAIR, background=False)
        run_id = next(iter(tool._runs))
        try:
            await _both_suspended(tool, run_id)
            handed = "a" if "node 'a'" in first.model_text else "b"
            queued = "b" if handed == "a" else "a"

            control = ResolveDagNodeTool(
                loop=SimpleNamespace(tools=SimpleNamespace(get=lambda n: tool if n == "run_subagent_dag" else None))
            )
            control.set_context("web", "default", "web:fg")
            params = {"run_id": run_id, "node_id": queued, "decision": "continue", "message": "here it is"}
            assert control.blocking_for(params), "with a ceiling on this call the park would end as a timeout"

            try:
                out = await asyncio.wait_for(control.execute(**params), timeout=5)
            except asyncio.TimeoutError:
                raise AssertionError(
                    f"the call never returned: node {handed!r} is still open, so the runner waits with no "
                    "deadline while this taker waits for an event nobody can send"
                ) from None

            assert f"node {handed} needs a decision" in getattr(out, "display_text", ""), (
                f"the still-open node is what this call is owed, got {getattr(out, 'display_text', out)!r}"
            )
        finally:
            tool.request_cancel(run_id)


async def test_a_suspended_node_holds_no_dispatch_slot(tmp_path: Path) -> None:
    """The wait for a decision happens after the node has given its slot back.

    Structural today -- the wait sits in the wave loop, outside the `async with
    semaphore` in the node -- and nothing asserted it. A bound run has no deadline,
    so a slot held across that wait would starve every unrelated spawn and graph
    sharing the gate, with no clock to end it.
    """
    gate = asyncio.Semaphore(1)
    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short("token a")], gate=gate)
        await tool.execute(task_summary="fg", nodes=_SOLO, background=False)
        run_id = next(iter(tool._runs))
        try:
            # Let the wave loop reach the wait first. Sampling the gate the instant the
            # report comes back proves nothing: it is free then because the node has
            # released it and whatever runs next has not taken it yet.
            await asyncio.sleep(0.3)
            assert tool._desks[run_id].is_open("a"), "the node is still suspended, so this is the state under test"
            assert not gate.locked(), "the wait is holding a dispatch slot no clock will ever release"
            await asyncio.wait_for(gate.acquire(), timeout=1)
            gate.release()
        finally:
            tool.request_cancel(run_id)


async def test_a_resolve_through_the_control_tool_is_handed_the_run_it_completes(tmp_path: Path) -> None:
    """The no-yield ordering, guarded at the caller that has to keep it.

    `_retire` drops the run's outbox from the index `release_turn` reaches runs
    through, so a run finishing before a taker exists has nowhere left to deliver.
    Nothing opens that window because `ResolveDagNodeTool.execute` resolves and
    awaits with no yield between -- and that is the code this drives, over a real
    outbox. An earlier version called the tool's own `resolve_node` / `await_run`
    back to back, which pins the outbox but never executes the call site, so an
    `await` added there left it green.

    What this catches is a yield long enough to lose the race; `await sleep(0)`
    passes a tick, the run has not finished, and this stays green. The structural
    half of the claim belongs to
    `test_the_resolve_tool_parks_its_taker_before_it_yields`.
    """
    from types import SimpleNamespace

    from raven.agent.subagent.dag_control_tools import ResolveDagNodeTool

    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short("token a")])
        await tool.execute(task_summary="fg", nodes=_SOLO, background=False)
        run_id = next(iter(tool._runs))

        control = ResolveDagNodeTool(
            loop=SimpleNamespace(tools=SimpleNamespace(get=lambda n: tool if n == "run_subagent_dag" else None))
        )
        control.set_context("web", "default", "web:fg")
        out = await asyncio.wait_for(
            control.execute(run_id=run_id, node_id="a", decision="continue", message="here is the token"),
            timeout=10,
        )

        text = getattr(out, "model_text", out)
        assert "finished: 1 completed" in text, (
            f"the run finished on this decision, so its result belongs to this call, got {text[:200]!r}"
        )
        assert "will run again with your message" not in text, (
            "that is the answer given when the run had nowhere left to deliver its result"
        )


async def test_a_stopped_run_tells_the_waiting_call_that_it_was_stopped(tmp_path: Path) -> None:
    """The one sentence a hard-cancelled run gives the call that was waiting on it.

    `stop()` is how a hard cancel reaches the outbox, and it wakes every parked
    taker with `Stopped` rather than leaving it hanging. That event has exactly one
    rendering and it is all the model gets -- no result, no report, no error -- so
    if it rendered as nothing the blocking call would return nothing and the turn
    would read as a graph that simply produced no answer.

    Driven through the blocking call itself: `await_run` refuses a run that is
    already stopped, because it is no longer bound, so a `Stopped` only ever
    reaches a caller that was parked before the stop.
    """

    class _Sleeper:
        kind = "raven-loop"

        async def run(self, task: str, **_kwargs: Any) -> str:
            await asyncio.sleep(60)
            return "never"

    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [])
        tool._resolve_node = lambda node: _Sleeper()  # type: ignore[method-assign]
        call = asyncio.create_task(tool.execute(task_summary="fg", nodes=_SOLO, background=False))
        for _ in range(300):
            await asyncio.sleep(0.01)
            run_id = next(iter(tool._outboxes), None)
            if run_id is not None and tool._outboxes[run_id]._takers:
                break
        run_id = next(iter(tool._outboxes))
        assert tool._outboxes[run_id]._takers, "the blocking call never parked, so this is not the state under test"

        tool._outboxes[run_id].stop()
        out = await asyncio.wait_for(call, timeout=10)

        text = getattr(out, "model_text", out)
        assert run_id in text, f"the answer has to name the run it is about, got {text!r}"
        assert "stopped" in text, f"and say what happened to it, got {text!r}"


async def test_release_touches_only_the_named_conversation(tmp_path: Path) -> None:
    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short()])
        await tool.execute(task_summary="fg", nodes=_CHAIN, background=False)
        (run_id,) = list(tool._runs)

        await tool.release_turn("web:somebody-else", flush=True)

        assert tool.is_foreground(run_id), "another conversation's turn ending must not release this run"


async def test_release_turn_survives_a_raising_outbox_and_still_releases_the_rest(tmp_path: Path) -> None:
    """release_turn drains every outbox bound to the conversation even when one of
    them blows up: a run behind a failing release must not be left bound to a
    turn that has already ended -- unclocked, and waiting for a decision nobody
    will make."""
    second_chain = [
        {"id": "c", "subagent": "echo", "node_summary": "first", "prompt_template": "do c"},
        {
            "id": "d",
            "subagent": "echo",
            "node_summary": "second",
            "prompt_template": "{{ c.output }}",
            "depends_on": ["c"],
        },
    ]
    async with draining_dag_runs():
        tool = _foreground_tool(tmp_path, [_falls_short(), _falls_short()])
        await tool.execute(task_summary="fg1", nodes=_CHAIN, background=False)
        await tool.execute(task_summary="fg2", nodes=second_chain, background=False)
        run_ids = list(tool._runs)
        assert len(run_ids) == 2
        first_outbox, second_outbox = (tool._outboxes[run_id] for run_id in run_ids)

        async def _raise(*, flush: bool) -> None:
            raise RuntimeError("boom")

        first_outbox.release = _raise

        await tool.release_turn("web:fg", flush=True)

        assert second_outbox.released.is_set(), "the second outbox still releases after the first one raises"


def _replan_plan(run_id: str = "run-new", from_node: str = "a", reason: str = "the plan was wrong"):
    from raven.agent.subagent.dag_adjudication import ReplanPlan

    return ReplanPlan(
        run_id=run_id,
        from_node=from_node,
        reason=reason,
        nodes=(),
        backends={},
        auto_instances=frozenset(),
        notices=(),
    )


async def _run_replanned_dag(
    tmp_path,
    *,
    extra_nodes: list[dict],
    slow: set[str] = frozenset(),
    crash: set[str] = frozenset(),
    resolve_with=None,
):
    """`a` fails its verdict; `extra_nodes` run beside it, independent of `a`.

    Independent on purpose: a node that depends on `a` is still `pending` when `a`
    suspends, so a chain cannot express "in flight" at all.
    """
    from raven.agent.subagent.dag_adjudication import REPLAN, AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()
    started = asyncio.Event()
    crashed_first = asyncio.Event()

    class _Backend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, prompt: str, **_kw: Any) -> str:
            self.calls.append(prompt)
            if any(nid in prompt for nid in crash):
                crashed_first.set()
                raise RuntimeError("boom failed on its own merit")
            if any(nid in prompt for nid in slow):
                started.set()
                await asyncio.sleep(30)
            return "did it"

    backend = _Backend()

    async def _judge(*, node, store, output, error, crashed):
        return Verdict(accomplished=node.id != "a", what_is_missing="the wrong tool was used")

    async def _announce(run_id, node_id, text, origin, *, awaiting_decision):
        if slow:
            await started.wait()
        if crash:
            # The crashing node's backend sets this and raises with no await in
            # between, so `_run_node`'s except-handler has already recorded it as
            # failed by the time this wait returns -- a real happens-before, the
            # same way `started`/`slow` above pin the in-flight case, not
            # scheduling-order luck.
            await crashed_first.wait()
        desk.resolve(node_id, REPLAN, "the plan was wrong", plan=resolve_with or _replan_plan(from_node=node_id))

    spec = parse_dag_spec(
        {
            "task_summary": "replan me",
            "nodes": [
                {"id": "a", "subagent": "x", "node_summary": "first", "prompt_template": "do a"},
                *extra_nodes,
            ],
        }
    )
    return await run_dag(
        spec,
        resolve=lambda node: backend,
        backend=LocalFileBackend(),
        workdir=str(tmp_path),
        run_root=str(tmp_path / "runs"),
        desk=desk,
        judge_node=_judge,
        announce_exception=_announce,
        origin=_TEST_ORIGIN,
        adjudication_timeout_s=5,
    )


async def test_a_replan_cancels_a_node_in_flight_rather_than_draining_it(tmp_path) -> None:
    """Cancel, not drain: chosen over waiting in the design. A slow node is cut off."""
    result = await _run_replanned_dag(
        tmp_path,
        extra_nodes=[{"id": "slowpoke", "subagent": "x", "node_summary": "slow", "prompt_template": "do slowpoke"}],
        slow={"slowpoke"},
    )

    statuses = {entry["node"]: entry["status"] for entry in result.files}
    assert statuses["a"] == "failed", "the adjudicated node is given up on, not cancelled"
    assert statuses["slowpoke"] == "cancelled", "the in-flight node was cut off, not drained"
    assert result.replanned_into == "run-new"


async def test_the_wind_down_reason_names_the_new_run(tmp_path) -> None:
    result = await _run_replanned_dag(tmp_path, extra_nodes=[])

    error = next(e for e in result.files if e["node"] == "a")["error"] or ""
    assert "run-new" in error
    assert "the plan was wrong" in error


async def test_a_pending_node_is_skipped_not_failed(tmp_path) -> None:
    result = await _run_replanned_dag(
        tmp_path,
        extra_nodes=[
            {
                "id": "never_ran",
                "subagent": "x",
                "node_summary": "downstream",
                "prompt_template": "do never_ran",
                "depends_on": ["a"],
            }
        ],
    )

    statuses = {entry["node"]: entry["status"] for entry in result.files}
    assert statuses["never_ran"] == "skipped"


async def test_a_completed_node_survives_a_replan_with_its_output(tmp_path) -> None:
    result = await _run_replanned_dag(
        tmp_path,
        extra_nodes=[{"id": "done", "subagent": "x", "node_summary": "fine", "prompt_template": "do done"}],
    )

    entry = next(e for e in result.files if e["node"] == "done")
    assert entry["status"] == "completed"
    assert entry["output_file"], "its output must stay referenceable by the new run"
    assert not entry["error"], "a completed node is untouched by the wind-down"


async def test_a_replan_does_not_overwrite_a_pre_existing_failure_or_its_cascade(tmp_path) -> None:
    """`_apply_replan` must leave a node terminal on its own merit alone.

    `boom` crashes on its own and is judged accomplished regardless, so
    `_apply_verdict` never touches its failure. `blocked` is cascaded to
    `skipped` off `boom` at the top of the next loop iteration -- one round
    before the replan is even discovered there, since `_cascade_failures` runs
    first every iteration. `a` is the node that actually suspends and triggers
    the replan. None of `boom` or `blocked`'s outcome is the replan's doing,
    and overwriting either would erase the real reason it ended.
    """
    result = await _run_replanned_dag(
        tmp_path,
        extra_nodes=[
            {"id": "boom", "subagent": "x", "node_summary": "crashes", "prompt_template": "do boom"},
            {
                "id": "blocked",
                "subagent": "x",
                "node_summary": "cascaded off boom",
                "prompt_template": "do blocked",
                "depends_on": ["boom"],
            },
        ],
        crash={"boom"},
    )

    files_by_node = {entry["node"]: entry for entry in result.files}
    assert files_by_node["boom"]["status"] == "failed"
    assert files_by_node["boom"]["error"] == "boom failed on its own merit"
    assert files_by_node["blocked"]["status"] == "skipped"
    assert not files_by_node["blocked"]["error"], "a cascaded skip has no reason of its own to overwrite"
    assert files_by_node["a"]["status"] == "failed"
    assert result.replanned_into == "run-new"


async def test_a_run_that_was_not_replanned_reports_no_successor(tmp_path) -> None:
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_verdict import Verdict

    desk = AdjudicationDesk()

    async def _judge(*, node, store, output, error, crashed):
        return Verdict(accomplished=True)

    async def _announce(*_a, **_kw):
        return None

    result = await _run_two_node_dag(tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce)

    assert result.replanned_into is None


async def test_a_replan_answer_that_lands_while_the_runner_is_parked_leaves_the_node_open() -> None:
    """The REPLAN branch inside `_await_adjudications` itself, not the round-interrupt shortcut.

    Every `_run_replanned_dag`-based test resolves the desk synchronously inside
    `announce_exception`, which runs from within the same round that opened the
    node -- so `desk.replanned` is already set by the time that round's loop next
    checks it, and this function's own per-node REPLAN handling never runs at all.
    A production resolve arrives from a separate turn's tool call, an arbitrary
    wall-clock time later; a background task racing the wait is the only way to
    reproduce that ordering rather than the shortcut.
    """
    from raven.agent.subagent.dag_adjudication import REPLAN, AdjudicationDesk
    from raven.agent.subagent.dag_runner import _apply_replan, _await_adjudications

    desk = AdjudicationDesk()
    desk.open("n0")
    status = {"n0": "exception"}
    errors: dict[str, str] = {}

    async def _decide() -> None:
        await asyncio.sleep(0.05)
        desk.resolve("n0", REPLAN, "the plan was wrong", plan=_replan_plan(run_id="run-new", from_node="n0"))

    driver = asyncio.create_task(_decide())
    try:
        await _await_adjudications(desk, status, errors, {}, timeout_s=5, cancel=None)
    finally:
        await driver

    # Left at "exception", not "failed": `_apply_replan` is the run's single writer of
    # a replanned node's terminal status and reason. Marking it "failed" here, with no
    # mention of the successor, would also make `_apply_replan`'s own terminal-node
    # guard skip this node on the next loop pass, since it only converts
    # "running"/"exception"/"pending" and this node would already look resolved.
    assert status["n0"] == "exception"
    assert errors == {}

    await _apply_replan(
        _replan_plan(run_id="run-new", from_node="n0"),
        status=status,
        errors=errors,
        published_terminal=set(),
        node_started_at={},
        node_ended_at={},
        by_id={"n0": _node_spec("n0")},
        session_key=None,
        run_id="run-old",
        progress_publisher=None,
    )

    assert status["n0"] == "failed"
    assert "run-new" in errors["n0"]
    assert "the plan was wrong" in errors["n0"]


async def test_a_replan_ends_the_wait_for_the_siblings_it_never_answered() -> None:
    """Two nodes suspend together; replanning one must not leave the round waiting
    on the other.

    The deadlock this guards is mutual and unbounded on a bound foreground run,
    where `released` is unset and the wait therefore carries no deadline at all:
    `resolve_dag_node` is itself parked in `await_finalized`, which waits on this
    run's task with no timeout of its own, so the one lane that could answer `n1`
    is the lane blocked on the run that is waiting for `n1`.

    `asyncio.timeout` rather than a bare await, so a regression here fails as this
    assertion instead of hanging the suite -- there is no deadline in the code
    under test to end it.
    """
    from raven.agent.subagent.dag_adjudication import REPLAN, AdjudicationDesk
    from raven.agent.subagent.dag_runner import _await_adjudications

    desk = AdjudicationDesk()
    desk.open("n0")
    desk.open("n1")
    status = {"n0": "exception", "n1": "exception"}
    errors: dict[str, str] = {}
    released = asyncio.Event()

    async def _decide() -> None:
        await asyncio.sleep(0.05)
        desk.resolve("n0", REPLAN, "the plan was wrong", plan=_replan_plan(run_id="run-new", from_node="n0"))

    driver = asyncio.create_task(_decide())
    try:
        async with asyncio.timeout(5):
            await _await_adjudications(desk, status, errors, {}, timeout_s=600, cancel=None, released=released)
    finally:
        await driver

    # Both left for `_apply_replan`: `n1` was never asked, so calling it a timeout
    # would blame the agent for a silence its own replan is what ended.
    assert status == {"n0": "exception", "n1": "exception"}
    assert errors == {}


def _node_spec(node_id: str) -> DagNodeSpec:
    return DagNodeSpec(id=node_id, subagent="x", node_summary="a step", prompt_template="do it")


def test_the_report_offers_all_three_decisions() -> None:
    from raven.agent.subagent.dag_runner import _exception_report
    from raven.agent.subagent.dag_verdict import Verdict

    report = _exception_report(
        run_id="r1",
        node=_node_spec("a"),
        verdict=Verdict(accomplished=False, what_is_missing="the wrong tool was used"),
        attempt=1,
        remaining=2,
        blocked=["b"],
        timeout_s=600.0,
    )

    assert '"decision": "continue"' in report
    assert '"decision": "abandon"' in report
    assert '"decision": "replan"' in report
    assert '"nodes"' in report, "a replan is useless without the argument that carries the graph"


def test_a_report_with_no_continuations_left_does_not_offer_replan_yet() -> None:
    """The exhausted path is unchanged by this plan. See the open question below."""
    from raven.agent.subagent.dag_runner import _exception_report
    from raven.agent.subagent.dag_verdict import Verdict

    report = _exception_report(
        run_id="r1",
        node=_node_spec("a"),
        verdict=Verdict(accomplished=False, what_is_missing="x"),
        attempt=3,
        remaining=0,
        blocked=[],
        timeout_s=600.0,
    )

    assert '"decision": "replan"' not in report
    assert "the continuation limit is reached" in report
