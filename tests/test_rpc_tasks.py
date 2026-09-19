"""``tasks.list`` -- a run-level row per task, built off the three stores.

Spawn rows are driven through a real spawn where the full pipeline matters
(``SubagentManager.spawn`` writing the record and its meta) and through
``SpawnRecord`` directly for the status ladder's edge cases, which need meta
shapes ``SubagentManager`` itself never produces (a record with no ``status``
key, an interrupted run). The file-recording hook itself is covered where it
lives, in ``tests/test_subagent_manager.py``; here a stand-in backend records
the entry by hand and the test checks it comes through. Dag rows are driven
through ``DagRunStore`` -- the graph and manifest are written by hand (a plain
dict, the way ``docs/specs/2026-09-18-desk-tasks-list-design.md`` describes
them), but the node registry is written through the store's own
``record_nodes`` / ``record_outcome``, so a hard-stopped run's ``nodes.json``
looks exactly like the one ``_mark_stopped`` leaves behind.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import raven.home as raven_home_module
from raven.agent.subagent.dag_store import DagRunStore, index_guard
from raven.agent.subagent.history import SpawnRecord, dag_root, nodes_root, session_history_root
from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.rpc.methods import tasks as tasks_mod
from raven.rpc.methods.tasks import tasks_list
from raven.rpc.models import TasksListResult

SESSION = "tui:live"


@pytest.fixture
def workspace(tmp_path: Path):
    """A workspace of our own, reached the way the handler reaches it."""
    import raven.config.loader as loader

    previous = raven_home_module._current_config_path
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agents": {"defaults": {"workspace": str(ws)}}}))
    loader.set_config_path(cfg)
    yield ws
    raven_home_module._current_config_path = previous


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real ``~/.raven/subagent_instances.json``."""
    from raven.agent.subagent import instances as instances_mod

    monkeypatch.setattr(instances_mod, "_registry", instances_mod.InstanceRegistry(path=tmp_path / "inst.json"))


def _session_dir(workspace: Path) -> Path:
    from raven.session.manager import SessionManager

    return SessionManager(workspace).session_dir(SESSION)


def _loop_stub(*, live_spawn_handles: frozenset = frozenset(), live_run_ids: frozenset = frozenset()):
    """A duck-typed agent loop: just enough for ``live_handles`` / liveness."""

    class _Manager:
        def live_handles(self, _session_key: str) -> set[tuple[str, str]]:
            return set(live_spawn_handles)

    class _Loop:
        subagents = _Manager()

        def active_dag_run_ids(self) -> set[str]:
            return set(live_run_ids)

    return _Loop()


def _factory(loop: Any):
    return lambda: loop


# ---------------------------------------------------------------------------
# spawn rows -- one through the real pipeline, the rest through the writer
# ---------------------------------------------------------------------------


async def _real_spawn(workspace: Path, task: str = "count the files", label: str = "counting") -> str:
    """One real spawn through ``SubagentManager``, awaited to completion.

    The stand-in backend writes a file and records the entry the way the
    in-process lane's hook would, so the test is about the record reaching
    the row, not about the hook (``tests/test_subagent_manager.py`` has that).
    """
    import asyncio

    from raven.agent.subagent import activity as activity_mod
    from raven.agent.subagent.manager import SubagentManager

    class _Provider:
        def get_default_model(self) -> str:
            return "m"

    class _WritesAFile:
        async def run(self, task: str, *, workspace: Path, **_kw: Any) -> str:
            # `raven_loop.py`'s own G1 hook (the tool-call site inside its model
            # loop) is exercised directly in `tests/test_subagent_manager.py`;
            # this stand-in calls the same `RunActivity` writer it calls, so
            # this file can test what `tasks.list` does with the result without
            # re-driving a whole fake model conversation. `note.md` is touched
            # twice so the row proves the wire folds a path to one entry, not
            # just that it passes a single one through.
            (workspace / "note.md").write_text("hello\n", encoding="utf-8")
            activity_mod.note_file_change("note.md", "write", 1, 0, len(b"hello\n"))
            (workspace / "note.md").write_text("hello\nworld\n", encoding="utf-8")
            activity_mod.note_file_change("note.md", "write", 1, 0, len(b"hello\nworld\n"))
            (workspace / "other.md").write_text("hi\n", encoding="utf-8")
            activity_mod.note_file_change("other.md", "write", 1, 0, len(b"hi\n"))
            return f"answer to {task}"

    mgr = SubagentManager(provider=_Provider(), workspace=workspace)
    mgr.set_submit(lambda _req: None)
    mgr.registry.set_builtin_builder(lambda _row, _build, _b=_WritesAFile(): _b)
    await mgr.spawn(task, task_summary=label, session_key=SESSION)
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)
    return task


async def test_a_real_spawn_is_one_completed_task(workspace: Path) -> None:
    await _real_spawn(workspace)

    result = await tasks_list({"session_key": SESSION})
    TasksListResult.model_validate(result)

    tasks = result["tasks"]
    assert len(tasks) == 1
    row = tasks[0]
    assert row["kind"] == "spawn"
    assert row["status"] == "completed"
    assert row["task_summary"] == "counting"
    assert row["counts"] == {
        "total": 1,
        "pending": 0,
        "running": 0,
        "completed": 1,
        "failed": 0,
        "skipped": 0,
        "cancelled": 0,
        "interrupted": 0,
        "exception": 0,
    }
    node = row["nodes"][0]
    assert node["agent"]
    assert node["has_output"] is True
    assert node["files"] == [
        {"path": "note.md", "op": "write", "add": 2, "del": 0, "size": len(b"hello\nworld\n")},
        {"path": "other.md", "op": "write", "add": 1, "del": 0, "size": len(b"hi\n")},
    ]


async def test_no_session_and_an_unknown_session_are_both_empty(workspace: Path) -> None:
    assert await tasks_list({}) == {"tasks": []}
    assert await tasks_list({"session_key": SESSION}) == {"tasks": []}


def _spawn_meta(*, agent: str, handle: str | None, task_summary: str | None) -> dict[str, Any]:
    return {
        "session_key": SESSION,
        "agent": agent,
        "instance": handle,
        "instance_auto": False,
        "handle": handle,
        "task_summary": task_summary,
        "working_directory": "/tmp",
    }


async def _claim_spawn_node(session_dir: Path, node_id: str) -> None:
    """Claim ``node_id`` in the session's node registry the way the real
    dispatch path claims it before ``SpawnRecord.open`` -- without this,
    ``_spawn_node_ids`` (which enumerates ``kind=spawn`` registry entries) never
    sees a hand-built record at all."""
    import time

    from raven.agent.subagent.dag_store import ensure_node_claimed, index_guard
    from raven.agent.subagent.prompt_backend import LocalFileBackend

    history_root = str(session_history_root(session_dir))
    async with index_guard(history_root):
        await ensure_node_claimed(
            LocalFileBackend(), history_root, node_id, kind="spawn", started_at_ms=int(time.time() * 1000)
        )


def _drop_meta_key(session_dir: Path, node_id: str, key: str) -> None:
    """Simulate a record written before ``key`` existed."""
    path = nodes_root(session_dir) / f"{node_id}.meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.pop(key, None)
    path.write_text(json.dumps(meta), encoding="utf-8")


class TestSpawnStatusLadder:
    async def test_aborted_reads_as_failed_with_its_error_from_out_md(self, workspace: Path) -> None:
        session_dir = _session_dir(workspace)
        await _claim_spawn_node(session_dir, "do_x")
        record = SpawnRecord.open(
            session_dir,
            task_id="do_x",
            task="do x",
            meta=_spawn_meta(agent="Raven", handle="do_x", task_summary="Do X"),
            node_id="do_x",
        )
        record.finish(status="aborted", output="Aborted: refused to do X.")

        row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
        assert row["status"] == "failed"
        assert row["nodes"][0]["error"] == "Aborted: refused to do X."

    async def test_a_failed_call_carries_its_error_head(self, workspace: Path) -> None:
        session_dir = _session_dir(workspace)
        await _claim_spawn_node(session_dir, "do_y")
        record = SpawnRecord.open(
            session_dir,
            task_id="do_y",
            task="do y",
            meta=_spawn_meta(agent="Raven", handle="do_y", task_summary="Do Y"),
            node_id="do_y",
        )
        record.finish(status="failed", error="Error: " + "x" * 600)

        row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
        assert row["status"] == "failed"
        assert len(row["nodes"][0]["error"]) == 500

    async def test_no_status_key_falls_back_to_out_md_presence(self, workspace: Path) -> None:
        session_dir = _session_dir(workspace)
        await _claim_spawn_node(session_dir, "legacy_done")
        record = SpawnRecord.open(
            session_dir,
            task_id="legacy_done",
            task="an old run",
            meta=_spawn_meta(agent="Raven", handle="legacy_done", task_summary="Legacy"),
            node_id="legacy_done",
        )
        record.finish(status="completed", output="done")
        _drop_meta_key(session_dir, "legacy_done", "status")

        row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
        assert row["status"] == "completed", "its answer is on disk, so it ended"

    async def test_no_status_key_and_nothing_written_yet_reads_as_running(self, workspace: Path) -> None:
        session_dir = _session_dir(workspace)
        await _claim_spawn_node(session_dir, "legacy_open")
        SpawnRecord.open(
            session_dir,
            task_id="legacy_open",
            task="an old run",
            meta=_spawn_meta(agent="Raven", handle="legacy_open", task_summary="Legacy"),
            node_id="legacy_open",
        )
        _drop_meta_key(session_dir, "legacy_open", "status")
        # Live, or the not-live overlay converts this `running` to
        # `interrupted` before the fallback ladder's own answer is visible.
        loop = _loop_stub(live_spawn_handles=frozenset({("Raven", "legacy_open")}))

        row = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]
        assert row["status"] == "running"

    async def test_a_running_spawn_with_no_live_handle_reads_interrupted(self, workspace: Path) -> None:
        session_dir = _session_dir(workspace)
        await _claim_spawn_node(session_dir, "hung")
        SpawnRecord.open(
            session_dir,
            task_id="hung",
            task="a run the gateway died under",
            meta=_spawn_meta(agent="Raven", handle="hung", task_summary="Hung"),
            node_id="hung",
        )

        row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
        assert row["status"] == "interrupted"
        assert row["nodes"][0]["status"] == "interrupted"

    async def test_a_running_spawn_with_a_live_handle_stays_running(self, workspace: Path) -> None:
        session_dir = _session_dir(workspace)
        await _claim_spawn_node(session_dir, "live")
        SpawnRecord.open(
            session_dir,
            task_id="live",
            task="still going",
            meta=_spawn_meta(agent="Raven", handle="live", task_summary="Live"),
            node_id="live",
        )
        loop = _loop_stub(live_spawn_handles=frozenset({("Raven", "live")}))

        row = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]
        assert row["status"] == "running"


async def test_kind_and_id_narrow_to_one_spawn(workspace: Path) -> None:
    session_dir = _session_dir(workspace)
    for nid in ("a", "b"):
        await _claim_spawn_node(session_dir, nid)
        SpawnRecord.open(
            session_dir,
            task_id=nid,
            task=nid,
            meta=_spawn_meta(agent="Raven", handle=nid, task_summary=nid),
            node_id=nid,
        ).finish(status="completed", output="ok")

    result = await tasks_list({"session_key": SESSION, "kind": "spawn", "id": "a"})
    assert [t["id"] for t in result["tasks"]] == ["a"]


async def test_an_id_without_kind_is_ignored(workspace: Path) -> None:
    session_dir = _session_dir(workspace)
    await _claim_spawn_node(session_dir, "a")
    SpawnRecord.open(
        session_dir,
        task_id="a",
        task="a",
        meta=_spawn_meta(agent="Raven", handle="a", task_summary="a"),
        node_id="a",
    ).finish(status="completed", output="ok")

    result = await tasks_list({"session_key": SESSION, "id": "a"})
    assert len(result["tasks"]) == 1, "id alone (no kind) narrows nothing"


# ---------------------------------------------------------------------------
# dag rows -- graph.json / manifest.json written by hand, nodes.json through
# DagRunStore's own record_nodes / record_outcome
# ---------------------------------------------------------------------------

RUN_ID = "20260918T000000000000Z-aaaaaaaa"

_GRAPH = {
    "task_summary": "a two-step graph",
    "nodes": [
        {
            "id": "n1",
            "subagent": "Raven",
            "node_summary": "first step",
            "prompt_template": "do 1",
            "depends_on": [],
            "skills": None,
            "mcps": None,
            "inputs": {},
            "instance": None,
        },
        {
            "id": "n2",
            "subagent": "Raven",
            "node_summary": "second step",
            "prompt_template": "do 2",
            "depends_on": ["n1"],
            "skills": None,
            "mcps": None,
            "inputs": {},
            "instance": None,
        },
    ],
}


async def _make_run(
    session_dir: Path, run_id: str, graph: dict[str, Any], node_ids: list[str], *, registry_root: Path | None = None
) -> DagRunStore:
    nodes_root(session_dir).mkdir(parents=True, exist_ok=True)
    store = DagRunStore(
        LocalFileBackend(),
        str(dag_root(session_dir)),
        run_id,
        nodes_root=str(nodes_root(session_dir)),
        registry_root=str(registry_root or session_history_root(session_dir)),
    )
    async with index_guard(store.registry_root):
        await store.init(json.dumps(graph), node_ids=node_ids)
    return store


def _manifest_entry(
    *,
    status: str,
    subagent: str = "Raven",
    started_at: int = 1_000,
    ended_at: int | None = 2_000,
    error: str | None = None,
    output_file: str | None = None,
    depends_on: list[str] | None = None,
    instance: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "subagent": subagent,
        "depends_on": depends_on or [],
        "instance": instance,
        "instance_auto": False,
        "started_at": started_at,
        "ended_at": ended_at,
        "prompt_file": None,
        "output_file": output_file,
        "error": error,
        **extra,
    }


async def test_a_finalized_two_node_run_is_one_completed_task(workspace: Path) -> None:
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    out1 = nodes_root(session_dir) / "n1.out.md"
    out1.write_text("done", encoding="utf-8")
    manifest = {
        "n1": _manifest_entry(
            status="completed", output_file=str(out1), tool_calls=["read_file"], tokens_in=10, tokens_out=5
        ),
        "n2": _manifest_entry(status="completed", depends_on=["n1"]),
    }
    await store.write_manifest(manifest)
    async with index_guard(store.registry_root):
        await store.record_outcome({"n1": "completed", "n2": "completed"}, summary="ok")

    result = await tasks_list({"session_key": SESSION})
    TasksListResult.model_validate(result)

    row = result["tasks"][0]
    assert row["kind"] == "dag"
    assert row["id"] == RUN_ID
    assert row["task_summary"] == "a two-step graph"
    assert row["status"] == "completed"
    assert row["started_at"] == 1_000
    assert row["ended_at"] == 2_000
    assert row["counts"]["total"] == 2
    assert row["counts"]["completed"] == 2

    n1 = row["nodes"][0]
    assert n1["node_id"] == "n1"
    assert n1["has_output"] is True
    assert n1["tool_call_count"] == 1
    assert n1["tool_failure_count"] == 0
    assert n1["tokens_in"] == 10 and n1["tokens_out"] == 5
    n2 = row["nodes"][1]
    assert n2["depends_on"] == ["n1"]
    assert n2["tool_call_count"] is None and n2["tool_failure_count"] is None


async def test_a_hard_stop_reads_cancelled_from_the_node_registry_not_all(workspace: Path) -> None:
    """The v1 bug this replaces: `_mark_stopped` leaves a completed node alone,
    so a run stopped after its first node finishes ends as a *mix* of
    completed/cancelled/skipped, and the task must read `cancelled` on ANY of
    those -- not only when every node shares the same terminal state."""
    graph = {
        "task_summary": "three steps, stopped after the first",
        "nodes": [
            {"id": "n1", "subagent": "Raven", "node_summary": "s1", "depends_on": [], "instance": None},
            {"id": "n2", "subagent": "Raven", "node_summary": "s2", "depends_on": ["n1"], "instance": None},
            {"id": "n3", "subagent": "Raven", "node_summary": "s3", "depends_on": ["n2"], "instance": None},
        ],
    }
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, graph, ["n1", "n2", "n3"])
    # No manifest.json at all: a hard stop enters `run_dag`'s CancelledError
    # branch, and `_finalize` never runs.
    async with index_guard(store.registry_root):
        await store.record_outcome({"n1": "completed", "n2": "cancelled", "n3": "skipped"}, summary="stopped")

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    assert row["status"] == "cancelled"
    assert [n["status"] for n in row["nodes"]] == ["completed", "cancelled", "skipped"]
    # The registry stamps a skipped node with the finalize moment; a node that
    # never ran has no clock of its own to show.
    assert row["nodes"][2]["started_at"] is None and row["nodes"][2]["ended_at"] is None
    assert row["counts"] == {
        "total": 3,
        "pending": 0,
        "running": 0,
        "completed": 1,
        "failed": 0,
        "skipped": 1,
        "cancelled": 1,
        "interrupted": 0,
        "exception": 0,
    }


async def test_a_dead_runs_claimed_node_reads_interrupted(workspace: Path) -> None:
    """No manifest and no terminal outcome in the node registry either -- the
    run declared these nodes and nothing more, which is `record_nodes`'s own
    `running` claim -- and nothing this gateway is executing turns that into
    `interrupted`."""
    session_dir = _session_dir(workspace)
    await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    assert [n["status"] for n in row["nodes"]] == ["interrupted", "interrupted"]
    assert row["status"] == "interrupted"


async def test_a_live_runs_claimed_but_undispatched_node_reads_pending_with_no_clock(workspace: Path) -> None:
    """`claim_node` writes `running` and the claim time for EVERY node of a run
    at start, so the registry cannot tell an executing node from one still
    waiting on its dependencies. Only the node the runner dispatched has an
    instance row; the other reads `pending`, with no clock of its own."""
    from raven.agent.subagent.instances import get_registry

    session_dir = _session_dir(workspace)
    await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    await get_registry().upsert_dag_node(SESSION, RUN_ID, "n1", "Raven", "running")
    loop = _loop_stub(live_run_ids=frozenset({RUN_ID}))

    row = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]
    n1, n2 = row["nodes"]
    assert n1["status"] == "running" and isinstance(n1["started_at"], int) and n1["ended_at"] is None
    assert n2["status"] == "pending" and n2["started_at"] is None and n2["ended_at"] is None
    assert row["status"] == "running"
    assert row["counts"]["running"] == 1 and row["counts"]["pending"] == 1


async def test_a_live_run_reads_each_node_from_its_own_instance_row(workspace: Path) -> None:
    """The registry claims every node `running` at start; the instance rows are
    where a live run's per-node truth is (`_write_node_status`): one done, one
    executing, one not yet dispatched must read as three different statuses."""
    from raven.agent.subagent.instances import get_registry

    graph = {
        "task_summary": "three steps in flight",
        "nodes": [
            {"id": "n1", "subagent": "Raven", "node_summary": "s1", "depends_on": [], "instance": None},
            {"id": "n2", "subagent": "Raven", "node_summary": "s2", "depends_on": ["n1"], "instance": None},
            {"id": "n3", "subagent": "Raven", "node_summary": "s3", "depends_on": ["n2"], "instance": None},
        ],
    }
    session_dir = _session_dir(workspace)
    await _make_run(session_dir, RUN_ID, graph, ["n1", "n2", "n3"])
    await get_registry().upsert_dag_node(SESSION, RUN_ID, "n1", "Raven", "completed")
    await get_registry().upsert_dag_node(SESSION, RUN_ID, "n2", "Raven", "running")
    loop = _loop_stub(live_run_ids=frozenset({RUN_ID}))

    row = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]
    assert [n["status"] for n in row["nodes"]] == ["completed", "running", "pending"]
    n1, n2, n3 = row["nodes"]
    assert isinstance(n1["ended_at"], int) and n2["ended_at"] is None and n3["started_at"] is None
    assert row["status"] == "running" and row["ended_at"] is None
    assert row["counts"]["completed"] == 1 and row["counts"]["running"] == 1 and row["counts"]["pending"] == 1


async def test_a_just_dispatched_run_sorts_above_older_finished_work(workspace: Path) -> None:
    """A run no node has started yet has no `started_at`; its id's UTC stamp
    says when it was minted, and that is what puts it at the top."""
    session_dir = _session_dir(workspace)
    old_id = "20260101T000000000000Z-0a0a0a0a"
    new_id = "20260918T235959000000Z-b1b1b1b1"
    old_store = await _make_run(session_dir, old_id, _GRAPH, ["n1", "n2"])
    await old_store.write_manifest(
        {"n1": _manifest_entry(status="completed"), "n2": _manifest_entry(status="completed")}
    )
    async with index_guard(old_store.registry_root):
        await old_store.record_outcome({"n1": "completed", "n2": "completed"}, summary="ok")
    await _make_run(session_dir, new_id, _GRAPH, ["n1", "n2"], registry_root=session_dir / "other")
    loop = _loop_stub(live_run_ids=frozenset({new_id}))

    rows = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"]
    assert [r["id"] for r in rows] == [new_id, old_id]
    assert rows[0]["started_at"] is None and rows[0]["status"] == "running"


async def test_a_run_dir_with_no_readable_graph_is_not_a_task(workspace: Path) -> None:
    """A graph mid-write or corrupt has no node list; reporting it as a
    completed run of zero steps is the one answer the record cannot support."""
    session_dir = _session_dir(workspace)
    await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    (dag_root(session_dir) / RUN_ID / "graph.json").write_text('{"task_summary": "half', encoding="utf-8")

    assert (await tasks_list({"session_key": SESSION}))["tasks"] == []


async def test_a_node_no_layer_has_ever_heard_of_reads_pending(workspace: Path) -> None:
    """`n2` is declared in `graph.json` but was never claimed in the node
    registry at all (a run that claims nodes progressively rather than all at
    once) -- the bottom of the three-layer ladder, and a live run must not
    turn a genuine `pending` into `interrupted`."""
    session_dir = _session_dir(workspace)
    await _make_run(session_dir, RUN_ID, _GRAPH, ["n1"])
    loop = _loop_stub(live_run_ids=frozenset({RUN_ID}))

    row = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]
    # `n1` is claimed but has no instance row either: claimed is not dispatched.
    assert [n["status"] for n in row["nodes"]] == ["pending", "pending"]
    assert row["status"] == "running"


async def test_a_suspended_node_of_a_dead_run_reads_interrupted(workspace: Path) -> None:
    """`exception` (suspended on `resolve_dag_node`) is a non-terminal status
    too, and reads `interrupted` once nothing is executing the run."""
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    async with index_guard(store.registry_root):
        await store.record_outcome({"n1": "completed"}, summary="one done, one suspended")
    # `n2` is left claimed at `running` by `record_nodes`; promote it to
    # `exception` the way `resolve_dag_node`'s caller would, without a manifest.
    from raven.agent.subagent.dag_store import read_registry, write_registry

    async with index_guard(store.registry_root):
        registry = await read_registry(LocalFileBackend(), store.registry_root)
        registry["nodes"]["n2"]["status"] = "exception"
        await write_registry(LocalFileBackend(), store.registry_root, registry)

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    by_id = {n["node_id"]: n["status"] for n in row["nodes"]}
    assert by_id == {"n1": "completed", "n2": "interrupted"}
    assert row["status"] == "interrupted"


class TestReplan:
    async def test_a_started_replan_reads_cancelled_with_the_link(self, workspace: Path) -> None:
        session_dir = _session_dir(workspace)
        store = await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
        await store.record_replan(
            {"run_id": "successor-run", "from_node": "n2", "reason": "steer", "decided_at": 1, "started": True}
        )
        await store.write_manifest(
            {"n1": _manifest_entry(status="completed"), "n2": _manifest_entry(status="failed", error="superseded")}
        )

        row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
        assert row["status"] == "cancelled"
        assert row["replan"] == {
            "run_id": "successor-run",
            "from_node": "n2",
            "reason": "steer",
            "started": True,
            "error": None,
        }

    async def test_a_replan_that_never_started_reads_failed_with_why(self, workspace: Path) -> None:
        session_dir = _session_dir(workspace)
        store = await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
        await store.record_replan(
            {
                "run_id": "successor-run",
                "from_node": "n2",
                "reason": "steer",
                "decided_at": 1,
                "started": False,
                "error": "the successor graph failed validation",
            }
        )
        await store.write_manifest(
            {"n1": _manifest_entry(status="completed"), "n2": _manifest_entry(status="failed", error="superseded")}
        )

        row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
        assert row["status"] == "failed"
        assert row["replan"]["started"] is False
        assert row["replan"]["error"] == "the successor graph failed validation"


async def test_dag_error_is_read_from_disk_and_capped(workspace: Path) -> None:
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    await store.write_manifest(
        {
            "n1": _manifest_entry(status="failed", error="boom: " + "x" * 600),
            "n2": _manifest_entry(status="skipped", started_at=None, ended_at=None),
        }
    )

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    assert row["status"] == "failed"
    n1 = next(n for n in row["nodes"] if n["node_id"] == "n1")
    assert len(n1["error"]) == 500


async def test_a_nodes_files_pass_through_from_the_manifest(workspace: Path) -> None:
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    files = [{"path": "a.py", "op": "edit", "add": 2, "del": 1, "size": 30}]
    await store.write_manifest(
        {
            "n1": _manifest_entry(status="completed", files=files),
            "n2": _manifest_entry(status="completed", depends_on=["n1"]),
        }
    )

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    assert row["nodes"][0]["files"] == files


async def test_a_legacy_records_duplicate_file_entries_fold_on_read(workspace: Path) -> None:
    """A record written before ``merge_file_change`` landed in the recorder
    still has one entry per call on disk; ``tasks.list`` folds it exactly as
    a freshly-recorded run would."""
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    legacy_files = [
        {"path": "a.py", "op": "write", "add": 3, "del": 0, "size": 42},
        {"path": "a.py", "op": "write", "add": 1, "del": 2, "size": 20},
    ]
    await store.write_manifest(
        {
            "n1": _manifest_entry(status="completed", files=legacy_files),
            "n2": _manifest_entry(status="completed", depends_on=["n1"]),
        }
    )

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    assert row["nodes"][0]["files"] == [{"path": "a.py", "op": "write", "add": 4, "del": 2, "size": 20}]


async def test_kind_and_id_narrow_to_one_dag_run(workspace: Path) -> None:
    session_dir = _session_dir(workspace)
    other_run = "20260918T000000000000Z-bbbbbbbb"
    for run_id in (RUN_ID, other_run):
        store = await _make_run(session_dir, run_id, _GRAPH, ["n1", "n2"])
        await store.write_manifest(
            {"n1": _manifest_entry(status="completed"), "n2": _manifest_entry(status="completed", depends_on=["n1"])}
        )

    result = await tasks_list({"session_key": SESSION, "kind": "dag", "id": RUN_ID})
    assert [t["id"] for t in result["tasks"]] == [RUN_ID]


async def test_playbook_is_derived_from_the_node_id_prefix(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks_mod, "_playbook_names", lambda: ["daily-scan"])
    graph = {
        "task_summary": "a playbook run",
        "nodes": [
            {"id": "daily-scan-a1b2c3-step1", "subagent": "Raven", "depends_on": [], "instance": None},
        ],
    }
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, graph, ["daily-scan-a1b2c3-step1"])
    await store.write_manifest({"daily-scan-a1b2c3-step1": _manifest_entry(status="completed")})

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    assert row["playbook"] == "daily-scan"


async def test_an_ordinary_dag_run_has_no_playbook(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks_mod, "_playbook_names", lambda: ["daily-scan"])
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    await store.write_manifest(
        {"n1": _manifest_entry(status="completed"), "n2": _manifest_entry(status="completed", depends_on=["n1"])}
    )

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    assert row["playbook"] is None


# ---------------------------------------------------------------------------
# both kinds together
# ---------------------------------------------------------------------------


async def test_newest_first_across_both_kinds(workspace: Path) -> None:
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    await store.write_manifest(
        {
            "n1": _manifest_entry(status="completed", started_at=1_000, ended_at=2_000),
            "n2": _manifest_entry(status="completed", depends_on=["n1"], started_at=1_000, ended_at=2_000),
        }
    )
    await _claim_spawn_node(session_dir, "later")
    SpawnRecord.open(
        session_dir,
        task_id="later",
        task="a later spawn",
        meta=_spawn_meta(agent="Raven", handle="later", task_summary="Later"),
        node_id="later",
    ).finish(status="completed", output="ok")
    # `SpawnRecord.open` stamps `started_at_ms` from the wall clock, which is
    # necessarily after the dag row's hand-written `1_000`.

    result = await tasks_list({"session_key": SESSION})
    assert [t["kind"] for t in result["tasks"]] == ["spawn", "dag"]


# ---------------------------------------------------------------------------
# the small readers and their refusals
# ---------------------------------------------------------------------------


def test_head_is_none_for_a_missing_file_and_for_one_that_cannot_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tasks_mod._head(tmp_path / "absent.md", 10) is None
    present = tmp_path / "present.md"
    present.write_text("0123456789abcdef", encoding="utf-8")
    assert tasks_mod._head(present, 10) == "0123456789"

    def _refuse(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise OSError("locked")

    monkeypatch.setattr(Path, "open", _refuse)
    assert tasks_mod._head(present, 10) is None


def test_tool_counts_three_ways() -> None:
    assert tasks_mod._tool_counts({}) == (None, None)
    assert tasks_mod._tool_counts({"tool_calls": ["a", "b"]}) == (2, 0)
    assert tasks_mod._tool_counts({"tool_calls": ["a", "b"], "tool_failures": ["b"]}) == (2, 1)


def test_clip_only_clips_strings() -> None:
    assert tasks_mod._clip(None) is None
    assert tasks_mod._clip(12) is None
    assert tasks_mod._clip("x" * 600) == "x" * 500


def test_run_id_epoch_ms_reads_the_utc_prefix_and_refuses_the_rest() -> None:
    assert tasks_mod._run_id_epoch_ms("20260918T000000000000Z-aaaaaaaa") == 1789689600000
    assert tasks_mod._run_id_epoch_ms("20260918T000000500000Z-aaaaaaaa") == 1789689600500
    assert tasks_mod._run_id_epoch_ms("e2e_spawn_ok") is None
    assert tasks_mod._run_id_epoch_ms("20261399T000000000000Z-aaaaaaaa") is None


def test_derive_playbook_needs_a_first_node_and_the_six_hex_tag() -> None:
    names = ["daily-scan", "daily-scan-report"]
    assert tasks_mod._derive_playbook(None, names) is None
    assert tasks_mod._derive_playbook("daily-scan-notahex-collect", names) is None
    assert tasks_mod._derive_playbook("daily-scan-a1b2c3-collect", names) == "daily-scan"
    assert tasks_mod._derive_playbook("daily-scan-report-a1b2c3-collect", names) == "daily-scan-report"


def test_playbook_names_is_empty_when_the_library_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.rpc.methods import playbooks as playbooks_mod

    def _broken() -> Any:
        raise RuntimeError("no library")

    monkeypatch.setattr(playbooks_mod, "_store", _broken)
    assert tasks_mod._playbook_names() == []


def test_liveness_is_advisory_when_the_manager_refuses(workspace: Path) -> None:
    class _Manager:
        def live_handles(self, _session_key: str) -> set[tuple[str, str]]:
            raise RuntimeError("no such session")

    class _Loop:
        subagents = _Manager()

    assert tasks_mod._live_spawn_handles(_factory(_Loop()), SESSION) == set()
    assert tasks_mod._manager_of(None) is None


async def test_a_graph_node_without_an_id_is_skipped_and_skills_pass_through(workspace: Path) -> None:
    graph = {
        "task_summary": "one odd node",
        "nodes": [
            {"subagent": "Raven"},
            {
                "id": "n1",
                "subagent": "Raven",
                "node_summary": "s1",
                "depends_on": [],
                "instance": None,
                "skills": ["quote"],
                "mcps": ["fs"],
                "inputs": {"topic": "gold"},
            },
        ],
    }
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, graph, ["n1"])
    await store.write_manifest({"n1": _manifest_entry(status="completed")})
    async with index_guard(store.registry_root):
        await store.record_outcome({"n1": "completed"}, summary="ok")

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    TasksListResult.model_validate({"tasks": [row]})
    assert [n["node_id"] for n in row["nodes"]] == ["n1"]
    assert row["nodes"][0]["skills"] == ["quote"]
    assert row["nodes"][0]["mcps"] == ["fs"]
    assert row["nodes"][0]["inputs"] == {"topic": "gold"}


async def test_a_session_with_no_run_dir_has_no_dag_rows(workspace: Path) -> None:
    session_dir = _session_dir(workspace)
    nodes_root(session_dir).mkdir(parents=True, exist_ok=True)
    assert (await tasks_list({"session_key": SESSION}))["tasks"] == []


async def test_an_unreadable_instance_registry_is_an_empty_overlay(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent import instances as instances_mod

    session_dir = _session_dir(workspace)
    await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])

    def _broken(self: Any, _session_key: str) -> Any:
        raise RuntimeError("registry offline")

    monkeypatch.setattr(instances_mod.InstanceRegistry, "list_instances", _broken)
    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    assert [n["status"] for n in row["nodes"]] == ["interrupted", "interrupted"]


async def test_a_session_dir_that_cannot_be_resolved_is_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(_key: str, _factory: Any) -> Path:
        raise OSError("no home")

    monkeypatch.setattr(tasks_mod, "_session_dir", _broken)
    assert await tasks_list({"session_key": SESSION}) == {"tasks": []}


async def test_register_binds_the_method_name_to_the_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: dict[str, Any] = {}

    class _Dispatcher:
        def register(self, name: str, handler: Any) -> None:
            handlers[name] = handler

    tasks_mod.register_tasks_methods(_Dispatcher(), agent_loop_factory=None)  # type: ignore[arg-type]
    assert set(handlers) == {"tasks.list"}
    assert await handlers["tasks.list"]({"session_key": ""}) == {"tasks": []}
