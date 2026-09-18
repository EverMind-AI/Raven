"""``tasks.list`` -- a run-level row per task, built off the three stores.

Spawn rows are driven through a real spawn where the full pipeline matters
(``SubagentManager.spawn``, exercising G1's file-recording hook end to end)
and through ``SpawnRecord`` directly for the status ladder's edge cases, which
need meta shapes ``SubagentManager`` itself never produces (a record with no
``status`` key, an interrupted run). Dag rows are driven through
``DagRunStore`` -- the graph and manifest are written by hand (a plain dict,
the way ``docs/specs/2026-09-18-desk-tasks-list-design.md`` and
``my_docs/specs/20260918_tasks_rpc_contract.md`` describe them), but the
node registry is written through the store's own ``record_nodes`` /
``record_outcome``, so a hard-stopped run's ``nodes.json`` looks exactly like
the one ``_mark_stopped`` leaves behind.
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

    Exercises G1 end to end: the stand-in backend calls the registry's real
    ``write_file`` tool, so the recorded ``files`` entry is the one
    ``raven_loop.py`` actually produced, not one this test fabricated.
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
            # re-driving a whole fake model conversation.
            (workspace / "note.md").write_text("hello\n", encoding="utf-8")
            activity_mod.note_file_change("note.md", "write", 1, 0, len(b"hello\n"))
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
    assert node["files"] == [{"path": "note.md", "op": "write", "add": 1, "del": 0, "size": len(b"hello\n")}]


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
    assert n1["tokens_in"] == 10 and n1["tokens_out"] == 5
    n2 = row["nodes"][1]
    assert n2["depends_on"] == ["n1"]


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


async def test_a_node_no_layer_has_ever_heard_of_reads_pending(workspace: Path) -> None:
    """`n2` is declared in `graph.json` but was never claimed in the node
    registry at all (a run that claims nodes progressively rather than all at
    once) -- the bottom of the three-layer ladder, and a live run must not
    turn a genuine `pending` into `interrupted`."""
    session_dir = _session_dir(workspace)
    await _make_run(session_dir, RUN_ID, _GRAPH, ["n1"])
    loop = _loop_stub(live_run_ids=frozenset({RUN_ID}))

    row = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]
    assert [n["status"] for n in row["nodes"]] == ["running", "pending"]
    assert row["status"] == "running"


async def test_a_suspended_node_of_a_dead_run_reads_interrupted(workspace: Path) -> None:
    """`exception` (suspended on `resolve_dag_node`) is a non-terminal status
    too -- `subagent.py::_dag_rows` was missing it from this same rule until
    the sibling fix in this change."""
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
