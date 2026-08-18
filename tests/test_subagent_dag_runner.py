"""DAG runner + native run_subagent_dag tool (req4/P3)."""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
from pathlib import Path
from typing import Any

import pytest

from raven.agent import workdir
from raven.agent.subagent import instances as instances_mod
from raven.agent.subagent.backends import format_agent_listing, third_party_agent_meta
from raven.agent.subagent_dag import AgentCapabilities, DagValidationError, parse_dag_spec
from raven.agent.subagent_dag._store import read_session_nodes
from raven.agent.subagent_dag.backend import LocalFileBackend
from raven.agent.subagent_dag.runner import run_dag
from raven.agent.subagent_dag.tool import _NODE_SCHEMA, GUIDE_SKILL_ID, SubAgentDagTool
from raven.agent.tools.base import ToolResult
from raven.config.schema import ThirdPartyCliSubagentConfig


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

    def __init__(self, fail_ids: set[str] | None = None) -> None:
        self.fail_ids = fail_ids or set()
        self.calls: list[dict] = []

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace,
        executor,
        session_key: str | None = None,
        instance: str | None = None,
    ) -> str:
        self.calls.append({"task_id": task_id, "session_key": session_key, "instance": instance})
        if task_id in self.fail_ids:
            raise RuntimeError(f"boom {task_id}")
        return f"OUT[{task_id}]:{task}"


# --- runner --------------------------------------------------------------


async def test_run_dag_runs_a_sub_agent_whose_name_has_a_space() -> None:
    # The roster key travels from the node spec through the capability check,
    # the backend lookup and the status records. A name `spawn` accepts must
    # work here too, so the whole path is exercised rather than only parsing.
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "General Audit", "prompt_template": "hello"},
                {
                    "id": "b",
                    "subagent": "General Audit",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ]
        }
    )
    result = await run_dag(
        spec,
        subagents={"General Audit": _FakeExec()},
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
    )
    assert result.summary == {"total": 2, "completed": 2, "failed": 0, "skipped": 0, "cancelled": 0}
    # The name survives into the per-node records the UI and the resume path read.
    assert {f["subagent"] for f in result.files} == {"General Audit"}


async def test_run_dag_passes_output_downstream_and_emits_progress() -> None:
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hello"},
                {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        }
    )
    events: list[tuple[str, dict]] = []

    async def pub(name, value):
        events.append((name, value))

    result = await run_dag(
        spec,
        subagents={"x": _FakeExec()},
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


async def test_run_dag_threads_session_key_and_node_instance_to_backend() -> None:
    # Nodes sharing an `instance` handle must resume the same stateful CLI
    # session; that only works if run_dag forwards both the DAG's session_key
    # and each node's own instance down to backend.run.
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hi", "instance": "refactor-auth"},
                {"id": "b", "subagent": "x", "prompt_template": "hi again"},
            ]
        }
    )
    backend = _FakeExec()
    await run_dag(
        spec,
        subagents={"x": backend},
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


async def test_run_dag_failure_cascades_to_skip() -> None:
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hi"},
                {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        }
    )
    result = await run_dag(
        spec,
        subagents={"x": _FakeExec(fail_ids={"a"})},
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
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hello"},
                {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        }
    )
    result = await run_dag(
        spec,
        subagents={"x": _FakeExec()},
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        session_key="web:sess1",
    )

    assert result.summary == {"total": 2, "completed": 2, "failed": 0, "skipped": 0, "cancelled": 0}
    assert any(status == "running" for _, status in seen)
    final_status = {r["nodeId"]: r["status"] for r in reg.list_instances("web:sess1")}
    assert final_status == {"a": "completed", "b": "completed"}


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
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hi-a"},
                {"id": "b", "subagent": "x", "prompt_template": "hi-b"},
                {"id": "c", "subagent": "x", "prompt_template": "{{ b.output }}", "depends_on": ["b"]},
            ]
        }
    )
    b_running = asyncio.Event()
    reaped: list[str] = []

    class _BlockingExec:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None) -> str:
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
                subagents={"x": _BlockingExec()},
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
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None) -> str:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1
            return f"OUT[{task_id}]"

    def _independent_nodes(prefix: str) -> Any:
        return parse_dag_spec(
            {"nodes": [{"id": f"{prefix}{i}", "subagent": "x", "prompt_template": "hi"} for i in range(3)]}
        )

    gate = asyncio.Semaphore(2)
    backend = _Tracking()
    results = await asyncio.gather(
        *[
            run_dag(
                _independent_nodes(prefix),
                subagents={"x": backend},
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
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hi"},
                {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        }
    )
    result = await run_dag(
        spec,
        subagents={"x": _FakeExec(fail_ids={"a"})},
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

    spec = parse_dag_spec({"nodes": [{"id": "a", "subagent": "x", "prompt_template": "hi"}]})
    result = await run_dag(
        spec, subagents={"x": _FakeExec()}, backend=_InMemBackend(), workdir="/w", run_root="/hist/mas_dag"
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


async def test_run_subagent_dag_tool_end_to_end(tmp_path: Path) -> None:
    tool = SubAgentDagTool(
        workspace=tmp_path,
        third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    assert tool.name == "run_subagent_dag"
    assert "echo" in tool.description

    out = await tool.execute(
        nodes=[
            {"id": "a", "subagent": "echo", "prompt_template": "hello world"},
            {"id": "b", "subagent": "echo", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
        ],
        background=False,
    )
    assert "2 completed" in out.model_text
    assert "hello world" in out.model_text  # a's output flowed to b (the sink) and back
    assert "(instance:" not in out.model_text  # stateless nodes must not include instance tags


async def test_run_subagent_dag_tool_dispatches_to_a_name_with_a_space(tmp_path: Path) -> None:
    # The whole user-facing path for a config the web UI already allows: the
    # roster advertises "General Audit", so a node naming it must reach it.
    # Previously the node was refused by the name charset before anything ran.
    tool = SubAgentDagTool(
        workspace=tmp_path,
        third_party_subagents=[ThirdPartyCliSubagentConfig(name="General Audit", command="cat")],
    )
    assert "General Audit" in tool.description

    out = await tool.execute(
        nodes=[{"id": "a", "subagent": "General Audit", "prompt_template": "hello world"}], background=False
    )
    assert "1 completed" in out.model_text
    assert "hello world" in out.model_text


class TestBackgroundRun:
    """The default shape: the call returns as soon as the graph is accepted and
    the outcome comes back as an announced turn, the way a spawn's does."""

    @staticmethod
    def _tool(tmp_path: Path, announce: Any = None) -> SubAgentDagTool:
        return SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            announce=announce,
        )

    async def test_the_call_returns_before_the_graph_does(self, tmp_path: Path) -> None:
        announces = _Announces()
        tool = self._tool(tmp_path, announces)
        tool.set_context("web", "default", "web:sess1")

        out = await tool.execute(
            nodes=[
                {"id": "a", "subagent": "echo", "prompt_template": "hello world"},
                {"id": "b", "subagent": "echo", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        )

        assert "started in the background" in out.model_text
        assert "2 nodes" in out.model_text
        # The outcome cannot be in the result -- nothing has run yet.
        assert "completed" not in out.model_text

        run_id, summary, origin = (await announces.wait())[0]
        assert run_id in out.model_text, "the announce must name the run the call reported"
        assert "2 completed" in summary
        assert "hello world" in summary  # a's output flowed to b and into the announce
        assert origin == {"channel": "web", "chat_id": "default", "session_key": "web:sess1"}

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
        first = await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}])
        await asyncio.wait_for(started.wait(), 10)

        started.clear()
        tool.set_context("web", "two", "web:sess2")
        tool.set_tool_call_id("call-2")
        second = await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}])
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

        out = await tool.execute(nodes=[{"id": "a", "subagent": "nope", "prompt_template": "hi"}])

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
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
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
        out = await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}])
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
        out = await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}])
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
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            charge=charge,
        )
        tool.set_context("web", "default", "web:sess1")

        # A fresh id per submission: ids are unique per conversation, so reusing
        # one would refuse the graph before it ever reached the budget, which is
        # what this test is about.
        def node(nid: str) -> dict:
            return {"id": nid, "subagent": "echo", "prompt_template": "hi"}

        first = await tool.execute(nodes=[node("a")])
        second = await tool.execute(nodes=[node("b")], background=False)
        third = await tool.execute(nodes=[node("c")])

        assert "started in the background" in str(first)
        assert "1 completed" in str(second), "a foreground run draws on the same budget"
        assert third == "Error: budget spent."
        assert charged == ["web:sess1"] * 3

        # A graph that never passes validation must not spend budget either.
        await tool.execute(nodes=[{"id": "d", "subagent": "nope", "prompt_template": "hi"}])
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
        import raven.agent.subagent_dag.tool as tool_mod

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
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            progress_publisher=_publish,
            announce=announces,
        )
        tool.set_context("web", "default", "web:sess1")

        out = await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}])
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
        import raven.agent.subagent_dag.tool as tool_mod

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
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            progress_publisher=_publish,
        )
        tool.set_context("web", "default", "web:sess1")

        out = await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}])
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
        import raven.agent.subagent_dag.tool as tool_mod

        async def _boom(*a, **kw):
            raise RuntimeError("backend exploded")

        monkeypatch.setattr(tool_mod, "run_dag", _boom)
        announces = _Announces()
        tool = self._tool(tmp_path, announces)
        tool.set_context("web", "default", "web:sess1")
        node = {"id": "a", "subagent": "echo", "prompt_template": "hi"}

        out = await tool.execute(nodes=[dict(node)])
        run_id = str(out.model_text).split()[2]
        _, summary, _ = (await announces.wait())[0]

        assert run_id in summary
        assert "backend exploded" in summary
        # The announce is a verbatim copy, so the foreground result is the same
        # text -- the id belongs to the summary, not to any announce framing.
        foreground = await tool.execute(nodes=[dict(node)], background=False)
        assert str(foreground).startswith("Error running DAG ")
        assert "backend exploded" in str(foreground)

    async def test_a_run_with_no_announcer_still_completes(self, tmp_path: Path) -> None:
        """Hosts that never wire one (the CLI before its scheduler exists, tests)
        must not turn a finished graph into an unretrievable crash."""
        tool = self._tool(tmp_path, None)
        out = await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}])
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

        out = await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}])
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
            third_party_subagents=[
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
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
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
            third_party_subagents=[
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
        desc = SubAgentDagTool(workspace=tmp_path, third_party_subagents=cfgs).description

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
            third_party_subagents=[_Bad(), ThirdPartyCliSubagentConfig(name="Good", command="cat")],
        )

        assert "Broken" not in tool.description
        assert "Good" in tool.description
        assert sorted(tool._subagents) == ["Good"]

    def test_empty_config_says_none_configured(self, tmp_path: Path) -> None:
        desc = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[]).description
        assert "(none configured)" in desc


class TestSubagentEnum:
    """`run_dag` rejects the whole graph over one unknown name, so a single
    typo costs the entire call. Constrain it in the schema instead."""

    def _subagent_field(self, tool: SubAgentDagTool) -> dict:
        return tool.parameters["properties"]["nodes"]["items"]["properties"]["subagent"]

    def test_enum_lists_the_configured_roster(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[
                ThirdPartyCliSubagentConfig(name="Writer", command="cat"),
                ThirdPartyCliSubagentConfig(name="Coder", command="cat"),
            ],
        )
        assert self._subagent_field(tool)["enum"] == ["Coder", "Writer"]

    def test_empty_roster_omits_the_enum_entirely(self, tmp_path: Path) -> None:
        """`enum: []` matches nothing while `subagent` stays required, which
        some providers reject as an unsatisfiable tool schema. Reachable at
        runtime: a hot-apply can clear the roster of a registered tool."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="Coder", command="cat")],
        )
        tool.set_third_party_subagents([])
        assert "enum" not in self._subagent_field(tool)

    def test_schema_is_rebuilt_and_never_mutates_the_module_constant(self, tmp_path: Path) -> None:
        """The constant is shared by every instance; annotating it in place
        would leak one tool's roster into another's schema."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="Coder", command="cat")],
        )
        tool.parameters  # noqa: B018 - the property is what would mutate

        assert "enum" not in _NODE_SCHEMA["properties"]["subagent"]
        fresh = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[])
        assert "enum" not in self._subagent_field(fresh)

    def test_enum_follows_a_hot_applied_roster(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[])
        tool.set_third_party_subagents([ThirdPartyCliSubagentConfig(name="Late", command="cat")])
        assert self._subagent_field(tool)["enum"] == ["Late"]


class TestGuideSkillPointer:
    """The node-wiring rules live in the skill, not in this description, so the
    description has to send the agent there before it guesses a graph shape."""

    def test_description_requires_reading_the_guide_first(self, tmp_path: Path) -> None:
        desc = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
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
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
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
        from raven.agent.tools.skill_hub import _lookup_on_disk, _split_qualified_id
        from raven.memory_engine.skill_forge import LocalSkillCatalog

        registry = LocalSkillCatalog(tmp_path, start_watcher=False).registry
        source, native = _split_qualified_id(GUIDE_SKILL_ID)
        assert _lookup_on_disk(registry, source, native) is not None


async def test_run_subagent_dag_tool_unknown_subagent(tmp_path: Path) -> None:
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[])
    out = await tool.execute(nodes=[{"id": "a", "subagent": "nope", "prompt_template": "x"}])
    assert out.startswith("Error")


async def test_run_subagent_dag_tool_fans_progress_to_sink(tmp_path: Path) -> None:
    tool = SubAgentDagTool(
        workspace=tmp_path,
        third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    # The loop delivers the turn's conversation via set_context.
    tool.set_context("web", "default", "web:sess1")
    events: list[tuple[str, str, dict]] = []

    async def sink(conversation, name, payload):
        events.append((conversation, name, payload))

    tool.set_progress_sink(sink)
    await tool.execute(
        nodes=[
            {"id": "a", "subagent": "echo", "prompt_template": "hi"},
            {"id": "b", "subagent": "echo", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
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
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
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
            nodes=[
                {"id": "a", "subagent": "echo", "prompt_template": "hi"},
                {"id": "b", "subagent": "echo", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
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
            third_party_subagents=[
                ThirdPartyCliSubagentConfig(name="echo", command="cat"),
                ThirdPartyCliSubagentConfig(name="broken", command="false"),
            ],
        )
        out = await tool.execute(
            nodes=[
                {"id": "boom", "subagent": "broken", "prompt_template": "x"},
                {"id": "after", "subagent": "echo", "prompt_template": "{{ boom.output }}", "depends_on": ["boom"]},
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
        third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    tool.set_context("tui", "default", "tui:sess1")
    tool.set_tool_call_id("call-42")
    events: list[tuple[str, dict]] = []

    async def sink(conversation, name, payload):
        events.append((name, payload))

    tool.set_progress_sink(sink)
    await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}], background=False)

    assert events, "expected at least one progress event"
    assert all(p.get("tool_call_id") == "call-42" for _, p in events)


async def test_run_subagent_dag_tool_omits_tool_call_id_when_host_sets_none(tmp_path: Path) -> None:
    """Hosts that never call set_tool_call_id (IM channels, tests) must not get
    a null key smuggled into the payload -- the web consumer folds the payload
    verbatim into its projection."""
    tool = SubAgentDagTool(
        workspace=tmp_path,
        third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    tool.set_context("im", "default", "im:sess1")
    events: list[dict] = []

    async def sink(conversation, name, payload):
        events.append(payload)

    tool.set_progress_sink(sink)
    await tool.execute(nodes=[{"id": "a", "subagent": "echo", "prompt_template": "hi"}], background=False)

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
            third_party_subagents=[
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
            nodes=[
                {"id": "draft", "subagent": "stateless_local", "prompt_template": "write", "instance": "author"},
                {"id": "revise", "subagent": "stateless_local", "prompt_template": "revise", "instance": "author"},
            ]
        )

        assert out.startswith("Error: invalid DAG")
        assert "No sub-agent was run." in out
        assert f'read_skill("{GUIDE_SKILL_ID}")' in out
        # Nothing was dispatched, so no run directory exists to read back.
        assert not (tmp_path / "sessions").exists()

    async def test_path_placeholder_to_a_boxed_agent_runs_no_node(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute(
            nodes=[
                {"id": "a", "subagent": "stateless_local", "prompt_template": "upstream"},
                {
                    "id": "b",
                    "subagent": "boxed",
                    "prompt_template": "read {{ a.output_path }}",
                    "depends_on": ["a"],
                },
            ]
        )

        assert out.startswith("Error: invalid DAG")
        assert "{{ a.output }}" in out  # the fix, not just the complaint
        assert "No sub-agent was run." in out
        assert not (tmp_path / "sessions").exists()

    async def test_a_graph_that_respects_the_tags_still_runs(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute(
            nodes=[
                {"id": "a", "subagent": "stateless_local", "prompt_template": "hello world"},
                {"id": "b", "subagent": "boxed", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
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
            nodes=[
                {"id": "draft", "subagent": "resumable", "prompt_template": "write", "instance": "author"},
                {
                    "id": "revise",
                    "subagent": "resumable",
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
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="agent", command="cat")],
        )
        tool.set_third_party_subagents(
            [ThirdPartyCliSubagentConfig(name="agent", command="cat", reads_local_files=False)]
        )

        assert "agent [stateless, no-local-files, no-progress]" in tool.description
        out = await tool.execute(
            nodes=[
                {"id": "a", "subagent": "agent", "prompt_template": "upstream"},
                {"id": "b", "subagent": "agent", "prompt_template": "{{ a.output_path }}", "depends_on": ["a"]},
            ]
        )
        assert out.startswith("Error: invalid DAG")


class TestValidationErrorGuidesRetry:
    """A rejected graph is the one moment we know the model got the shape wrong,
    and the description it read once is long past. The result has to carry the
    way back itself."""

    async def test_structural_failure_points_at_the_guide(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        )
        out = await tool.execute(
            nodes=[
                {"id": "a", "subagent": "echo", "prompt_template": "{{ b.output }}", "depends_on": ["b"]},
                {"id": "b", "subagent": "echo", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        )

        assert "cycle" in out
        assert f'read_skill("{GUIDE_SKILL_ID}")' in out
        assert "retry" in out
        assert not (tmp_path / "sessions").exists()

    async def test_no_guide_installed_means_no_dead_end_advice(self, tmp_path: Path) -> None:
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
            guide_skill_id=None,
        )
        out = await tool.execute(nodes=[{"id": "a", "subagent": "nope", "prompt_template": "hi"}])

        assert out.startswith("Error: invalid DAG")
        assert "read_skill" not in out

    async def test_one_sentence_boundary_between_detail_and_advice(self, tmp_path: Path) -> None:
        """Validation messages are written without a trailing period, so the
        renderer supplies one — else the detail runs into the next sentence."""
        tool = SubAgentDagTool(
            workspace=tmp_path,
            third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        )
        out = await tool.execute(
            nodes=[
                {"id": "a", "subagent": "echo", "prompt_template": "hi"},
                {"id": "a", "subagent": "echo", "prompt_template": "hi"},
            ]
        )

        assert "node ids must be unique. No sub-agent was run." in out


# --- references across runs in one conversation ---------------------------


async def test_a_later_run_reads_an_earlier_runs_output() -> None:
    # The whole point of the @runs/ prefix: run dirs live under the session's
    # metadata directory, which no relative path from the workdir reaches, so
    # without it a chain of graphs in one conversation can pass nothing along.
    backend = _InMemBackend()
    first = await run_dag(
        parse_dag_spec({"nodes": [{"id": "plan", "subagent": "x", "prompt_template": "draft it"}]}),
        subagents={"x": _FakeExec()},
        backend=backend,
        workdir="/w",
        run_root="/hist/mas_dag",
    )

    second = await run_dag(
        parse_dag_spec(
            {
                "nodes": [
                    {
                        "id": "build",
                        "subagent": "x",
                        "prompt_template": f"earlier: {{{{ ref:@runs/{first.run_id}/plan.out.md }}}}",
                    }
                ]
            }
        ),
        subagents={"x": _FakeExec()},
        backend=backend,
        workdir="/w",
        run_root="/hist/mas_dag",
    )

    assert second.summary["completed"] == 1
    prompt = backend.files[f"/hist/mas_dag/{second.run_id}/build.prompt.md"].decode()
    assert "earlier: OUT[plan]:draft it" in prompt


async def test_a_reference_under_the_history_root_needs_the_grant() -> None:
    spec = parse_dag_spec(
        {
            "nodes": [
                {
                    "id": "n",
                    "subagent": "x",
                    "prompt_template": "{{ ref:/hist/spawn/call1/result.md }}",
                }
            ]
        }
    )
    backend = _InMemBackend()
    backend.files["/hist/spawn/call1/result.md"] = b"A SPAWN RECORD"

    with pytest.raises(DagValidationError, match="outside"):
        await run_dag(spec, subagents={"x": _FakeExec()}, backend=backend, workdir="/w", run_root="/hist/mas_dag")

    result = await run_dag(
        spec,
        subagents={"x": _FakeExec()},
        backend=backend,
        workdir="/w",
        run_root="/hist/mas_dag",
        subagents_root="/hist",
    )
    assert result.summary["completed"] == 1
    assert "A SPAWN RECORD" in backend.files[f"/hist/mas_dag/{result.run_id}/n.prompt.md"].decode()


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
    spec = parse_dag_spec({"nodes": [{"id": "n", "subagent": "x", "prompt_template": f"{{{{ ref:{path} }}}}"}]})
    backend = _InMemBackend()
    backend.files[path] = b"SHOULD NOT BE READABLE"
    with pytest.raises(DagValidationError, match="outside"):
        await run_dag(
            spec,
            subagents={"x": _FakeExec()},
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
        third_party_subagents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
    )
    tool.set_context("web", "victim", "web:victim")

    with workdir.bind(ws):
        # Foreground, so the outcome is this call's own result: what is asserted
        # is that the node ran and read the file, not that it was accepted.
        ok = await tool.execute(
            nodes=[{"id": "reads_workdir", "subagent": "echo", "prompt_template": f"{{{{ ref:{ws / 'brief.md'} }}}}"}],
            background=False,
        )
        assert "1 completed" in ok.model_text

        # Its own run history is the second root, reached by absolute path.
        run_dir = Path(str(ok.model_text).split("Run dir: ")[1].splitlines()[0])
        history = await tool.execute(
            nodes=[
                {
                    "id": "reads_history",
                    "subagent": "echo",
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
                nodes=[{"id": nid, "subagent": "echo", "prompt_template": f"{{{{ ref:{target} }}}}"}]
            )
            assert "invalid DAG" in refused, nid
            assert "outside the session workdir and its sub-agent history" in refused, nid


# --- node ids are unique per session, and addressable across runs ---------


async def _run(spec_nodes: list[dict], backend, root: str, history: str = "/hist"):
    return await run_dag(
        parse_dag_spec({"nodes": spec_nodes}),
        subagents={"x": _FakeExec()},
        backend=backend,
        workdir="/w",
        run_root=root,
        subagents_root=history,
    )


async def test_a_bare_node_id_reaches_an_earlier_run_with_no_depends_on() -> None:
    backend = _InMemBackend()
    first = await _run([{"id": "plan", "subagent": "x", "prompt_template": "draft it"}], backend, "/hist/mas_dag")

    second = await _run(
        [
            {
                "id": "build",
                "subagent": "x",
                "prompt_template": "text={{ plan.output }} path={{ plan.output_path }}",
            }
        ],
        backend,
        "/hist/mas_dag",
    )

    assert second.summary["completed"] == 1
    prompt = backend.files[f"/hist/mas_dag/{second.run_id}/build.prompt.md"].decode()
    assert "text=OUT[plan]:draft it" in prompt
    assert f"path=/hist/mas_dag/{first.run_id}/plan.out.md" in prompt


async def test_depends_on_may_name_a_node_an_earlier_run_completed() -> None:
    # Stating the dependency is legal even though it orders nothing: the graph
    # reads as a whole either way, and a model that writes the edge defensively
    # should not have its whole graph refused for it.
    backend = _InMemBackend()
    first = await _run([{"id": "plan", "subagent": "x", "prompt_template": "draft it"}], backend, "/hist/mas_dag")

    second = await _run(
        [
            {
                "id": "build",
                "subagent": "x",
                "depends_on": ["plan"],
                "prompt_template": "text={{ plan.output }} path={{ plan.output_path }}",
            }
        ],
        backend,
        "/hist/mas_dag",
    )

    assert second.summary["completed"] == 1
    prompt = backend.files[f"/hist/mas_dag/{second.run_id}/build.prompt.md"].decode()
    assert "text=OUT[plan]:draft it" in prompt
    assert f"path=/hist/mas_dag/{first.run_id}/plan.out.md" in prompt


async def test_a_cross_run_dependency_neither_blocks_nor_unterminals_a_node() -> None:
    # The scheduler waits on statuses of *this* run. An edge out of the graph
    # has none, so it must not be waited on (the node would never become
    # ready) nor counted as a dependent (the node would stop being terminal).
    backend = _InMemBackend()
    await _run([{"id": "plan", "subagent": "x", "prompt_template": "draft it"}], backend, "/hist/mas_dag")

    second = await _run(
        [{"id": "build", "subagent": "x", "depends_on": ["plan"], "prompt_template": "no placeholder"}],
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
    await _run([{"id": "plan", "subagent": "x", "prompt_template": "draft it"}], backend, "/hist/mas_dag")

    second = await _run(
        [
            {"id": "a", "subagent": "x", "depends_on": ["plan"], "prompt_template": "{{ plan.output }}"},
            {"id": "b", "subagent": "x", "depends_on": ["a", "plan"], "prompt_template": "{{ a.output }}"},
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
    await _run([{"id": "plan", "subagent": "x", "prompt_template": "draft it"}], backend, "/hist/mas_dag")

    result = await run_dag(
        parse_dag_spec(
            {
                "nodes": [
                    {"id": "a", "subagent": "x", "prompt_template": "hi"},
                    {
                        "id": "b",
                        "subagent": "x",
                        "depends_on": ["a", "plan"],
                        "prompt_template": "{{ a.output }} {{ plan.output }}",
                    },
                ]
            }
        ),
        subagents={"x": _FakeExec(fail_ids={"a"})},
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
            [{"id": "build", "subagent": "x", "depends_on": ["plan"], "prompt_template": "hi"}],
            backend,
            "/hist/mas_dag",
        )


async def test_depends_on_an_id_no_run_has_produced_is_still_refused() -> None:
    with pytest.raises(DagValidationError, match="depends on unknown 'ghost'"):
        await _run(
            [{"id": "build", "subagent": "x", "depends_on": ["ghost"], "prompt_template": "hi"}],
            _InMemBackend(),
            "/hist/mas_dag",
        )


async def test_there_is_no_run_qualifier_on_a_node_reference() -> None:
    # A node id already names one node per conversation, so a run qualifier
    # would only ever restate what the id says -- and could contradict it.
    # Pinning a run is the ``ref:@runs/`` file form's job.
    with pytest.raises(DagValidationError, match="unrecognized placeholder"):
        await _run(
            [{"id": "build", "subagent": "x", "prompt_template": "{{ @runs/some-run/plan.output }}"}],
            _InMemBackend(),
            "/hist/mas_dag",
        )


async def test_a_node_input_rejects_a_run_key() -> None:
    with pytest.raises(DagValidationError, match="nothing more to qualify"):
        await _run(
            [
                {
                    "id": "build",
                    "subagent": "x",
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

    bare = await _run([{"id": "n1", "subagent": "x", "prompt_template": "{{ plan.output }}"}], backend, "/hist/mas_dag")
    assert "FROM-RUNB" in backend.files[f"/hist/mas_dag/{bare.run_id}/n1.prompt.md"].decode()

    pinned = await _run(
        [{"id": "n2", "subagent": "x", "prompt_template": "{{ ref:@runs/runA/plan.out.md }}"}],
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
        await _run([{"id": "n1", "subagent": "x", "prompt_template": "{{ plan.output }}"}], backend, "/hist/mas_dag")
    with pytest.raises(DagValidationError, match="already used by run"):
        await _run([{"id": "plan", "subagent": "x", "prompt_template": "retry"}], backend, "/hist/mas_dag")

    ok = await _run(
        [{"id": "n2", "subagent": "x", "prompt_template": "{{ ref:@runs/runOld/plan.out.md }}"}],
        backend,
        "/hist/mas_dag",
    )
    assert "FROM-RUNOLD" in backend.files[f"/hist/mas_dag/{ok.run_id}/n2.prompt.md"].decode()


async def test_a_node_input_takes_another_runs_output() -> None:
    backend = _InMemBackend()
    first = await _run([{"id": "plan", "subagent": "x", "prompt_template": "draft it"}], backend, "/hist/mas_dag")

    result = await _run(
        [
            {
                "id": "build",
                "subagent": "x",
                "prompt_template": "text={{ inputs.prev }} path={{ inputs.prev.path }}",
                "inputs": {"prev": {"node": "plan"}},
            }
        ],
        backend,
        "/hist/mas_dag",
    )
    prompt = backend.files[f"/hist/mas_dag/{result.run_id}/build.prompt.md"].decode()
    assert "text=OUT[plan]:draft it" in prompt
    assert f"path=/hist/mas_dag/{first.run_id}/plan.out.md" in prompt


async def test_a_node_input_may_also_name_a_dependency_of_this_graph() -> None:
    backend = _InMemBackend()
    result = await _run(
        [
            {"id": "up", "subagent": "x", "prompt_template": "hello"},
            {
                "id": "down",
                "subagent": "x",
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
    await _run([{"id": "plan", "subagent": "x", "prompt_template": "draft it"}], backend, "/hist/mas_dag")

    with pytest.raises(DagValidationError, match="already used by run"):
        await _run([{"id": "plan", "subagent": "x", "prompt_template": "draft it again"}], backend, "/hist/mas_dag")


async def test_an_id_is_claimed_at_run_start_so_a_concurrent_graph_cannot_take_it() -> None:
    # Both runs are validated before either finishes; without claiming ids at
    # init the session would end up with two nodes answering to 'plan'.
    backend = _InMemBackend()
    started = asyncio.Event()
    release = asyncio.Event()

    class _Slow:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None) -> str:
            started.set()
            await release.wait()
            return "slow"

    first = asyncio.create_task(
        run_dag(
            parse_dag_spec({"nodes": [{"id": "plan", "subagent": "s", "prompt_template": "hi"}]}),
            subagents={"s": _Slow()},
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
        )
    )
    await started.wait()
    try:
        with pytest.raises(DagValidationError, match="already used by run"):
            await _run([{"id": "plan", "subagent": "x", "prompt_template": "mine now"}], backend, "/hist/mas_dag")
    finally:
        release.set()
        await first


async def test_a_node_that_no_run_produced_is_still_refused() -> None:
    with pytest.raises(DagValidationError, match="undeclared dependency"):
        await _run(
            [{"id": "build", "subagent": "x", "prompt_template": "{{ ghost.output }}"}],
            _InMemBackend(),
            "/hist/mas_dag",
        )


# --- an id being taken is not the same as its output being readable ---------


async def _seeded_outcomes(backend: _InMemBackend) -> None:
    """One finished run holding a completed, a failed and a skipped node."""
    await run_dag(
        parse_dag_spec(
            {
                "nodes": [
                    {"id": "ok_node", "subagent": "x", "prompt_template": "fine"},
                    {"id": "bad_node", "subagent": "x", "prompt_template": "explode"},
                    {
                        "id": "downstream",
                        "subagent": "x",
                        "prompt_template": "{{ bad_node.output }}",
                        "depends_on": ["bad_node"],
                    },
                ]
            }
        ),
        subagents={"x": _FakeExec(fail_ids={"bad_node"})},
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
            [{"id": "later", "subagent": "x", "prompt_template": template, "inputs": inputs}],
            backend,
            "/hist/mas_dag",
        )
    # Refused, so not one node of the new graph was dispatched.
    assert {p for p in backend.files if p.endswith(".prompt.md")} == runs_before


async def test_a_failed_nodes_id_stays_taken() -> None:
    backend = _InMemBackend()
    await _seeded_outcomes(backend)
    with pytest.raises(DagValidationError, match="already used by run"):
        await _run([{"id": "bad_node", "subagent": "x", "prompt_template": "retry"}], backend, "/hist/mas_dag")


async def test_a_completed_sibling_of_a_failed_node_is_still_readable() -> None:
    backend = _InMemBackend()
    await _seeded_outcomes(backend)
    result = await _run(
        [{"id": "later", "subagent": "x", "prompt_template": "{{ ok_node.output }}"}], backend, "/hist/mas_dag"
    )
    assert "OUT[ok_node]:fine" in backend.files[f"/hist/mas_dag/{result.run_id}/later.prompt.md"].decode()


async def test_referencing_an_in_flight_node_does_not_suggest_re_creating_it() -> None:
    backend = _InMemBackend()
    started, release = asyncio.Event(), asyncio.Event()

    class _Slow:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None) -> str:
            started.set()
            await release.wait()
            return "eventually"

    first = asyncio.create_task(
        run_dag(
            parse_dag_spec({"nodes": [{"id": "slow_node", "subagent": "s", "prompt_template": "hi"}]}),
            subagents={"s": _Slow()},
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
        )
    )
    await started.wait()
    try:
        with pytest.raises(DagValidationError, match="has not finished writing") as caught:
            await _run(
                [{"id": "later", "subagent": "x", "prompt_template": "{{ slow_node.output }}"}],
                backend,
                "/hist/mas_dag",
            )
        # Its id is taken, so "put both nodes in one graph" would be advice the
        # uniqueness check refuses; the message must not offer it.
        assert "depends_on" not in str(caught.value)
        with pytest.raises(DagValidationError, match="already used by run"):
            await _run([{"id": "slow_node", "subagent": "x", "prompt_template": "mine"}], backend, "/hist/mas_dag")
    finally:
        release.set()
        await first

    after = await _run(
        [{"id": "later2", "subagent": "x", "prompt_template": "{{ slow_node.output }}"}], backend, "/hist/mas_dag"
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
        [{"id": node_id, "subagent": "x", "prompt_template": template}],
        _InMemBackend(),
        "/hist/mas_dag",
    )
    entry = result.files[0]
    assert entry["status"] == "failed"
    assert entry["error"].startswith(f"{template} points at '/w/gone.md', which does not exist"), entry["error"]


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
            parse_dag_spec({"nodes": [{"id": "plan", "subagent": "x", "prompt_template": "hi"}]}),
            subagents={"x": _FakeExec()},
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
            parse_dag_spec({"nodes": [{"id": "plan", "subagent": "x", "prompt_template": "hi"}]}),
            subagents={"x": _Blocking()},
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
        await _run([{"id": "plan", "subagent": "x", "prompt_template": "again"}], backend, "/hist/mas_dag")
    with pytest.raises(DagValidationError, match="was stopped mid-run"):
        await _run([{"id": "other", "subagent": "x", "prompt_template": "{{ plan.output }}"}], backend, "/hist/mas_dag")


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
    async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None) -> str:
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
            parse_dag_spec({"nodes": [{"id": "plan", "subagent": "y", "prompt_template": "go"}]}),
            subagents={"y": _Yielding()},
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
                parse_dag_spec({"nodes": [{"id": node_id, "subagent": "y", "prompt_template": "go"}]}),
                subagents={"y": _Yielding()},
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
        "subagents": {"x": _FakeExec()},
        "backend": LocalFileBackend(),
        "workdir": str(tmp_path / "wd"),
        "run_root": str(root),
        "subagents_root": str(tmp_path / "hist"),
    }
    (tmp_path / "wd").mkdir()

    first = await run_dag(
        parse_dag_spec({"nodes": [{"id": "seed", "subagent": "x", "prompt_template": "make it"}]}), **common
    )
    assert (root / first.run_id / "seed.out.md").is_file()

    second = await run_dag(
        parse_dag_spec(
            {
                "nodes": [
                    {
                        "id": "consumer",
                        "subagent": "x",
                        # The out-of-graph edge belongs on the real backend too:
                        # it is the scheduler that has to treat it as satisfied,
                        # and a run that never becomes ready writes no prompt.
                        "depends_on": ["seed"],
                        "prompt_template": "text={{ seed.output }}\npath={{ seed.output_path }}\ninput={{ inputs.p }}",
                        "inputs": {"p": {"node": "seed"}},
                    }
                ]
            }
        ),
        **common,
    )
    prompt = (root / second.run_id / "consumer.prompt.md").read_text(encoding="utf-8")
    assert "text=OUT[seed]:make it" in prompt
    assert f"path={root / first.run_id / 'seed.out.md'}" in prompt
    assert "input=OUT[seed]:make it" in prompt

    # The index on disk carries both runs, with the outcome that makes 'seed'
    # readable at all.
    entries = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert [e["run_id"] for e in entries] == [first.run_id, second.run_id]
    assert entries[0]["status"] == {"seed": "completed"}

    # And the id stays taken on the real backend too.
    with pytest.raises(DagValidationError, match="already used by run"):
        await run_dag(
            parse_dag_spec({"nodes": [{"id": "seed", "subagent": "x", "prompt_template": "again"}]}), **common
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
        from raven.agent.subagent_dag._store import node_live_key

        self.seen_live.append(activity.live(node_live_key(self.run_id, kw["task_id"])))
        activity.note_transcript([{"role": "tool", "name": "read", "content": "file body"}])
        return await super().run(task, **kw)


async def test_a_node_writes_down_its_own_transcript() -> None:
    spec = parse_dag_spec({"nodes": [{"id": "a", "subagent": "x", "prompt_template": "hello"}]})
    exec_ = _PublishingExec()
    exec_.run_id = ""
    backend = _InMemBackend()

    result = await run_dag(
        spec,
        subagents={"x": exec_},
        backend=backend,
        workdir="/w",
        run_root="/hist/mas_dag",
    )

    written = backend.files[f"/hist/mas_dag/{result.run_id}/a.transcript.jsonl"].decode()
    assert '"name": "read"' in written


async def test_a_node_in_flight_is_findable_in_the_live_index() -> None:
    """The transcript file is written when the node ends, and a reader watching
    a node that is still going needs an answer before then."""
    spec = parse_dag_spec({"nodes": [{"id": "a", "subagent": "x", "prompt_template": "hello"}]})
    exec_ = _PublishingExec()
    backend = _InMemBackend()

    # The run id is minted inside run_dag, so the backend cannot know it in
    # advance -- it is read back out of the store the runner writes to.
    import raven.agent.subagent_dag.runner as runner_mod

    mint = runner_mod.make_run_id
    minted: list[str] = []

    def _mint() -> str:
        minted.append(mint())
        exec_.run_id = minted[-1]
        return minted[-1]

    runner_mod.make_run_id = _mint
    try:
        await run_dag(spec, subagents={"x": exec_}, backend=backend, workdir="/w", run_root="/hist/mas_dag")
    finally:
        runner_mod.make_run_id = mint

    assert exec_.seen_live and exec_.seen_live[0] is not None


async def test_the_live_index_does_not_outlive_the_node() -> None:
    from raven.agent.subagent import activity
    from raven.agent.subagent_dag._store import node_live_key

    spec = parse_dag_spec({"nodes": [{"id": "a", "subagent": "x", "prompt_template": "hello"}]})
    result = await run_dag(
        spec,
        subagents={"x": _FakeExec()},
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
        "nodes": [
            {"id": "a", "subagent": "x", "prompt_template": "hi"},
            {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
        ]
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
            subagents={"x": agent},
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
            subagents={"x": agent},
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
            subagents={"x": agent},
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
            subagents={"x": agent},
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
            parse_dag_spec({"nodes": [{"id": "a", "subagent": "x", "prompt_template": "hi"}]}),
            subagents={"x": agent},
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
            [{"id": "next", "subagent": "x", "prompt_template": "{{ a.output }}"}],
            backend,
            "/hist/mas_dag",
        )


def test_dag_mints_an_instance_for_a_stateful_node(tmp_path: Path) -> None:
    cfg = ThirdPartyCliSubagentConfig(name="worker", command="cat {agent_id}", resume_command="cat --resume {agent_id}")
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[cfg])
    spec = parse_dag_spec({"nodes": [{"id": "research", "subagent": "worker", "prompt_template": "go"}]})
    minted_spec, auto = tool._mint_missing_instances(spec, tool._capabilities)
    assert auto == frozenset({"research"})
    assert re.fullmatch(r"research-[0-9a-f]{6}", minted_spec.nodes[0].instance)


def test_dag_leaves_a_stateless_node_without_an_instance(tmp_path: Path) -> None:
    cfg = ThirdPartyCliSubagentConfig(name="worker", command="cat")
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[cfg])
    spec = parse_dag_spec({"nodes": [{"id": "research", "subagent": "worker", "prompt_template": "go"}]})
    minted_spec, auto = tool._mint_missing_instances(spec, tool._capabilities)
    assert auto == frozenset()
    assert minted_spec.nodes[0].instance is None


def test_dag_never_overwrites_an_instance_the_model_chose(tmp_path: Path) -> None:
    cfg = ThirdPartyCliSubagentConfig(name="worker", command="cat {agent_id}", resume_command="cat --resume {agent_id}")
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[cfg])
    spec = parse_dag_spec(
        {"nodes": [{"id": "research", "subagent": "worker", "prompt_template": "go", "instance": "author"}]}
    )
    minted_spec, auto = tool._mint_missing_instances(spec, tool._capabilities)
    assert auto == frozenset()
    assert minted_spec.nodes[0].instance == "author"


def test_dag_minting_does_not_repeat_across_two_submissions(tmp_path: Path) -> None:
    """The regression lock on cross-run collision: two graphs whose nodes share
    an id must not share a session."""
    cfg = ThirdPartyCliSubagentConfig(name="worker", command="cat {agent_id}", resume_command="cat --resume {agent_id}")
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[cfg])
    raw = {"nodes": [{"id": "research", "subagent": "worker", "prompt_template": "go"}]}
    first, _ = tool._mint_missing_instances(parse_dag_spec(raw), tool._capabilities)
    second, _ = tool._mint_missing_instances(parse_dag_spec(raw), tool._capabilities)
    assert first.nodes[0].instance != second.nodes[0].instance


async def test_run_with_roles_a_role_declared_stateless_only_via_extra_capabilities_mints_nothing(
    tmp_path: Path,
) -> None:
    """``run_with_roles`` is the playbook executor's entry: its roles exist only
    in ``extra_capabilities``, never in the tool's own roster. Minting must
    honour that role's own declared ``stateful=False`` rather than falling
    back to the permissive default a name absent from the base roster gets."""
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[])
    out = await tool.run_with_roles(
        [{"id": "step", "subagent": "pb-step", "prompt_template": "go"}],
        roles={"pb-step": _FakeExec()},
        role_capabilities={"pb-step": AgentCapabilities(stateful=False)},
        background=False,
    )

    assert isinstance(out, ToolResult), f"gate rejected the graph: {out}"
    assert "instance:" not in out.model_text


async def test_run_dag_records_whether_each_instance_was_minted() -> None:
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "go", "instance": "chosen"},
                {"id": "b", "subagent": "x", "prompt_template": "go", "instance": "b-abc123"},
            ]
        }
    )

    result = await run_dag(
        spec,
        subagents={"x": _FakeExec()},
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
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[cfg])
    result = await tool.execute(
        nodes=[{"id": "research", "subagent": "worker", "prompt_template": "go"}],
        background=False,
    )
    assert re.search(r"- research \[\w+\] \(instance: research-[0-9a-f]{6}\):", str(result))
