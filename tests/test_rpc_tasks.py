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
from raven.agent.subagent.dag_reader import read_run
from raven.agent.subagent.dag_store import DagRunStore, index_guard
from raven.agent.subagent.history import SpawnRecord, dag_root, nodes_root, session_history_root, spawn_live_key
from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.agent.subagent.registry import AgentRegistry
from raven.config.schema import BuiltinAgentConfig
from raven.rpc.methods import tasks as tasks_mod
from raven.rpc.methods.instances import instances_list
from raven.rpc.methods.subagent import subagent_context, subagent_list
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
        "not_executed": 0,
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


@pytest.mark.parametrize(
    "agent, expected",
    [("raven", "Raven-General"), ("Raven", "Raven-General"), ("Raven-General", "Raven-General"), ("Scribe", "Scribe")],
)
@pytest.mark.parametrize("finished", [False, True])
async def test_persisted_spawn_identity_matches_instances_and_roster(
    workspace, tmp_path, agent, expected, finished
) -> None:
    session_dir = _session_dir(workspace)
    await _claim_spawn_node(session_dir, "legacy_task")
    record = SpawnRecord.open(
        session_dir,
        task_id="legacy_task",
        task="inspect a file",
        meta=_spawn_meta(agent=agent, handle="review", task_summary="Inspect file"),
        node_id="legacy_task",
    )
    status = "completed" if finished else "running"
    if finished:
        record.finish(status=status, output="done")
    instance_path = tmp_path / "inst.json"
    instance_path.write_text(
        json.dumps(
            {
                "instances": [
                    {"sessionKey": SESSION, "agent": agent, "handle": "review", "kind": "spawn", "status": status}
                ]
            }
        )
    )
    meta_path = nodes_root(session_dir) / "legacy_task.meta.json"
    before = {path: path.read_bytes() for path in (instance_path, meta_path)}
    loop = _loop_stub(live_spawn_handles=frozenset({(expected, "review")}) if not finished else frozenset())
    registry = AgentRegistry()
    registry.apply([BuiltinAgentConfig(name="Scribe")])
    loop.subagents.registry = registry
    factory = _factory(loop)

    task = (await tasks_list({"session_key": SESSION}, agent_loop_factory=factory))["tasks"][0]
    listed = (await subagent_list({"session_id": SESSION}, agent_loop_factory=factory))["items"][0]
    context = await subagent_context({"session_id": SESSION, "id": "legacy_task"}, agent_loop_factory=factory)
    instances = (await instances_list({"session_key": SESSION}, agent_loop_factory=factory))["instances"]

    assert task["status"] == status
    assert task["nodes"][0]["status"] == status
    assert [task["agent"], task["nodes"][0]["agent"], listed["agent"], context["agent"]] == [expected] * 4
    assert (task["nodes"][0]["agent"], task["nodes"][0]["instance"]) == (instances[0]["agent"], instances[0]["handle"])
    assert registry.get(task["agent"]).name == expected
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize("missing", [False, True])
async def test_persisted_spawn_without_agent_keeps_the_missing_identity(workspace, missing) -> None:
    session_dir = _session_dir(workspace)
    await _claim_spawn_node(session_dir, "anonymous")
    record = SpawnRecord.open(
        session_dir, task_id="anonymous", task="old task", meta={"agent": None}, node_id="anonymous"
    )
    record.finish(status="completed", output="done")
    if missing:
        _drop_meta_key(session_dir, "anonymous", "agent")
    path = nodes_root(session_dir) / "anonymous.meta.json"
    before = path.read_bytes()

    task = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    listed = (await subagent_list({"session_id": SESSION}))["items"][0]
    context = await subagent_context({"session_id": SESSION, "id": "anonymous"})

    assert task["agent"] is listed["agent"] is context["agent"] is None
    assert task["nodes"][0]["agent"] == ""
    assert path.read_bytes() == before


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
        loop = _loop_stub(live_spawn_handles=frozenset({("Raven-General", "legacy_open")}))

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
        loop = _loop_stub(live_spawn_handles=frozenset({("Raven-General", "live")}))

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


@pytest.mark.parametrize(
    "graph_agent, manifest_agent, expected",
    [
        ("raven", None, "Raven-General"),
        ("Raven", None, "Raven-General"),
        ("Raven-General", None, "Raven-General"),
        ("Scribe", None, "Scribe"),
        (None, None, None),
        ("Scribe", "raven", "Raven-General"),
        ("Scribe", "Raven", "Raven-General"),
        ("Raven", "Scribe", "Scribe"),
    ],
)
async def test_persisted_dag_readers_publish_one_agent_identity(
    workspace, graph_agent, manifest_agent, expected
) -> None:
    session_dir = _session_dir(workspace)
    graph = {
        "task_summary": "legacy graph",
        "nodes": [{"id": "legacy_node", "subagent": graph_agent, "instance": "review"}],
    }
    store = await _make_run(session_dir, RUN_ID, graph, ["legacy_node"])
    if manifest_agent is not None:
        await store.write_manifest(
            {"legacy_node": _manifest_entry(status="completed", subagent=manifest_agent, instance="review")}
        )
    paths = list((dag_root(session_dir) / RUN_ID).glob("*.json"))
    before = {path: path.read_bytes() for path in paths}

    task = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    listed = (await subagent_list({"session_id": SESSION}))["items"][0]
    run = await read_run(LocalFileBackend(), str(dag_root(session_dir)), RUN_ID, str(nodes_root(session_dir)))

    assert task["nodes"][0]["agent"] == (expected or "")
    assert listed["agent"] == expected
    assert run["files"][0]["subagent"] == expected
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize("source", ["graph", "manifest"])
@pytest.mark.parametrize("agent", ["raven", "Raven"])
async def test_dag_reader_canonicalizes_the_former_agent_field(workspace, source, agent) -> None:
    session_dir = _session_dir(workspace)
    graph = {"nodes": [{"id": "legacy_node", **({"agent": agent} if source == "graph" else {})}]}
    store = await _make_run(session_dir, RUN_ID, graph, ["legacy_node"])
    if source == "manifest":
        await store.write_manifest({"legacy_node": {"agent": agent, "status": "completed"}})

    run = await read_run(LocalFileBackend(), str(dag_root(session_dir)), RUN_ID, str(nodes_root(session_dir)))

    assert run["files"][0]["subagent"] == "Raven-General"


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
    assert "tool_failure_count" not in n1
    assert n1["tokens_in"] == 10 and n1["tokens_out"] == 5
    n2 = row["nodes"][1]
    assert n2["depends_on"] == ["n1"]
    assert n2["tool_call_count"] is None
    assert "tool_failure_count" not in n2


async def test_a_reassigned_node_reads_back_its_current_agent_and_attempt_history(workspace: Path) -> None:
    """Before the manifest lands the registry row is the only record of who is
    running the node; a reader that fell back to ``graph.json`` would name the
    agent that gave up."""
    from raven.agent.subagent import instances as instances_mod

    session_dir = _session_dir(workspace)
    graph = {
        "task_summary": "moved",
        "nodes": [{"id": "fix", "subagent": "alpha", "node_summary": "do it", "depends_on": [], "instance": None}],
    }
    await _make_run(session_dir, RUN_ID, graph, ["fix"])
    registry = instances_mod.get_registry()
    await registry.upsert_dag_node(SESSION, RUN_ID, "fix", "alpha", "exception")
    await registry.move_dag_node(
        SESSION,
        run_id=RUN_ID,
        node_id="fix",
        old_agent="alpha",
        new_agent="beta",
        status="running",
        instance="fix-minted",
        instance_auto=True,
        attempt_agents=["alpha", "beta"],
    )

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]

    (node,) = row["nodes"]
    assert (node["agent"], node["instance"]) == ("beta", "fix-minted")
    assert node["instance_auto"] is True
    assert node["attempt_agents"] == ["alpha", "beta"]


async def test_a_finalized_reassigned_run_reads_the_same_identity_from_its_manifest(workspace: Path) -> None:
    """After finalization the manifest is the record, and it agrees with what the
    registry said while the run was going: same agent, handle, flag and history."""
    session_dir = _session_dir(workspace)
    graph = {"task_summary": "moved", "nodes": [{"id": "fix", "subagent": "alpha", "depends_on": []}]}
    store = await _make_run(session_dir, RUN_ID, graph, ["fix"])
    await store.write_manifest(
        {
            "fix": _manifest_entry(
                status="completed",
                subagent="beta",
                instance="fix-minted",
                attempt_agents=["alpha", "beta"],
            )
        }
    )

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]

    (node,) = row["nodes"]
    assert (node["agent"], node["instance"]) == ("beta", "fix-minted")
    assert node["attempt_agents"] == ["alpha", "beta"]
    assert node["instance_auto"] is False


async def test_a_node_that_never_moved_keeps_the_node_keys_it_always_had(workspace: Path) -> None:
    session_dir = _session_dir(workspace)
    graph = {"task_summary": "plain", "nodes": [{"id": "fix", "subagent": "Raven", "depends_on": []}]}
    await _make_run(session_dir, RUN_ID, graph, ["fix"])

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]

    (node,) = row["nodes"]
    assert not {"attempt_agents", "instance_auto"} & set(node)


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
        "not_executed": 0,
    }


def test_counts_carry_the_new_status() -> None:
    counts = tasks_mod._counts(["not_executed", "completed"])

    assert counts["not_executed"] == 1
    assert counts["completed"] == 1
    assert counts["total"] == 2
    assert tasks_mod._counts(["completed"])["not_executed"] == 0, "present on every row, zero when nothing branched"


def test_a_run_whose_only_oddity_is_an_unselected_branch_reads_as_completed() -> None:
    assert tasks_mod._task_status(["completed", "not_executed"]) == "completed"
    assert tasks_mod._task_status(["not_executed"]) == "completed"


def test_an_unselected_branch_moves_no_rung_of_the_status_ladder() -> None:
    """The rungs it sits beside answer as they did: it is not a state that
    stopped the run, so it neither adds to a cancelled run nor hides a failure."""
    assert tasks_mod._task_status(["completed", "skipped"]) == "cancelled"
    assert tasks_mod._task_status(["completed", "cancelled", "not_executed"]) == "cancelled"
    assert tasks_mod._task_status(["failed", "not_executed"]) == "failed"
    assert tasks_mod._task_status(["running", "not_executed"]) == "running"
    assert tasks_mod._task_status(["interrupted", "not_executed"]) == "interrupted"


_BRANCHING_GRAPH = {
    "task_summary": "handle an issue",
    "nodes": [
        {
            "id": "triage",
            "subagent": "Raven",
            "node_summary": "classify",
            "depends_on": [],
            "routes": {"bug": "a defect", "docs": "a question"},
        },
        {"id": "fix", "subagent": "Raven", "node_summary": "patch it", "depends_on": ["triage"], "when": "triage.bug"},
        {"id": "reply", "subagent": "Raven", "node_summary": "answer", "depends_on": ["triage"]},
    ],
}


async def test_a_run_with_an_unselected_branch_is_a_completed_task(workspace: Path) -> None:
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _BRANCHING_GRAPH, ["triage", "fix", "reply"])
    await store.write_manifest(
        {
            "triage": _manifest_entry(status="completed", selected_routes=["docs"]),
            "fix": _manifest_entry(
                status="not_executed", depends_on=["triage"], started_at=9_000, ended_at=9_500, reason="when_false"
            ),
            "reply": _manifest_entry(status="completed", depends_on=["triage"]),
        }
    )
    async with index_guard(store.registry_root):
        await store.record_outcome({"triage": "completed", "fix": "not_executed", "reply": "completed"}, summary="ok")

    result = await tasks_list({"session_key": SESSION})
    TasksListResult.model_validate(result)

    row = result["tasks"][0]
    assert row["status"] == "completed"
    assert (row["counts"]["total"], row["counts"]["completed"], row["counts"]["not_executed"]) == (3, 2, 1)
    assert row["counts"]["skipped"] == 0
    fix = row["nodes"][1]
    assert fix["status"] == "not_executed"
    # The runner stamps a node it decided will not run with the moment it decided,
    # which is not a clock the node had: no stamp of its own, and none stretching the task's window.
    assert fix["started_at"] is None and fix["ended_at"] is None
    assert (row["started_at"], row["ended_at"]) == (1_000, 2_000)


_BRANCH_KEYS = {"reason", "selected_routes", "route_evidence", "routes", "select", "when"}


def _branch_part(node: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in node.items() if key in _BRANCH_KEYS}


async def test_a_branching_runs_nodes_carry_what_they_asked_and_what_was_decided(workspace: Path) -> None:
    """A task's node detail shows the reason, the selection, the quote and
    the guard, and `tasks.list` is the only read it reloads from. What happened is
    the manifest's; what each node was asked to branch on is graph.json's. An empty
    selection is a selection, and a plain node carries none of the six."""
    graph = {
        "task_summary": "handle an issue",
        "nodes": [
            {"id": "triage", "subagent": "Raven", "routes": {"bug": "a defect", "docs": "a question"}},
            {
                "id": "scan",
                "subagent": "Raven",
                "depends_on": ["triage"],
                "routes": {"docs": "mentions docs", "api": "mentions the api"},
                "select": "many",
            },
            {"id": "fix", "subagent": "Raven", "depends_on": ["triage"], "when": "triage.bug"},
            {"id": "verify", "subagent": "Raven", "depends_on": ["fix"]},
            {"id": "reply", "subagent": "Raven", "depends_on": ["triage"]},
        ],
    }
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, graph, ["triage", "scan", "fix", "verify", "reply"])
    await store.write_manifest(
        {
            "triage": _manifest_entry(
                status="completed", selected_routes=["docs"], route_evidence="the reporter asks how to configure it"
            ),
            "scan": _manifest_entry(status="completed", depends_on=["triage"], selected_routes=[]),
            "fix": _manifest_entry(status="not_executed", depends_on=["triage"], reason="when_false"),
            "verify": _manifest_entry(status="not_executed", depends_on=["fix"], reason="upstream_not_executed"),
            "reply": _manifest_entry(status="completed", depends_on=["triage"]),
        }
    )
    async with index_guard(store.registry_root):
        await store.record_outcome(
            {
                "triage": "completed",
                "scan": "completed",
                "fix": "not_executed",
                "verify": "not_executed",
                "reply": "completed",
            },
            summary="ok",
        )

    result = await tasks_list({"session_key": SESSION})
    TasksListResult.model_validate(result)

    by_node = {n["node_id"]: n for n in result["tasks"][0]["nodes"]}
    assert _branch_part(by_node["triage"]) == {
        "routes": ["bug", "docs"],
        "select": "one",
        "selected_routes": ["docs"],
        "route_evidence": "the reporter asks how to configure it",
    }
    assert _branch_part(by_node["scan"]) == {"routes": ["docs", "api"], "select": "many", "selected_routes": []}
    assert _branch_part(by_node["fix"]) == {"reason": "when_false", "when": "triage.bug"}
    assert _branch_part(by_node["verify"]) == {"reason": "upstream_not_executed"}
    assert _branch_part(by_node["reply"]) == {}
    assert "it reports a defect" not in json.dumps(result), "a route's condition is the judge's and goes nowhere"


async def test_a_run_that_never_branched_has_exactly_the_node_keys_it_always_had(workspace: Path) -> None:
    """The tasks tab reads this for every run, so one without routes or guards answers with
    none of the six branch keys -- not null, not empty -- and nothing else moves."""
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    await store.write_manifest(
        {
            "n1": _manifest_entry(status="completed"),
            "n2": _manifest_entry(status="failed", depends_on=["n1"], error="boom"),
        }
    )
    async with index_guard(store.registry_root):
        await store.record_outcome({"n1": "completed", "n2": "failed"}, summary="failed")

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]

    always = {
        "node_id",
        "node_summary",
        "agent",
        "instance",
        "status",
        "depends_on",
        "started_at",
        "ended_at",
        "error",
        "tokens_in",
        "tokens_out",
        "tool_call_count",
        "has_output",
        "prompt_template",
        "inputs",
        "files",
    }
    assert [set(node) for node in row["nodes"]] == [always, always]


async def test_tasks_list_builds_the_branch_keys_with_the_helper_dag_get_does(workspace: Path) -> None:
    """One helper answers for both reads, and the shapes a person editing
    graph.json can leave are where two derivations would part: a list for `routes`,
    an empty map, a stray `select`, a blank or non-text guard, an unset `select`."""
    from raven.agent.subagent.dag_reader import branch_fields, read_run

    two = {"a": "the first holds", "b": "the second holds"}
    shapes = {
        "unset": {"routes": two},
        "null_select": {"routes": two, "select": None},
        "many": {"routes": two, "select": "many"},
        "empty_routes": {"routes": {}, "select": "many"},
        "list_routes": {"routes": ["a", "b"]},
        "stray_select": {"select": "many"},
        "blank_when": {"when": "   "},
        "non_text_when": {"when": 5},
        "real_when": {"when": "unset.a"},
        "plain": {},
    }
    graph = {
        "task_summary": "hand edited",
        "nodes": [{"id": nid, "subagent": "Raven", "depends_on": [], **extra} for nid, extra in shapes.items()],
    }
    manifest = {
        nid: _manifest_entry(status="completed", **({"selected_routes": ["a"]} if "routes" in extra else {}))
        for nid, extra in shapes.items()
    }
    manifest["real_when"] = _manifest_entry(status="not_executed", reason="when_unknown")
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, graph, list(shapes))
    await store.write_manifest(manifest)
    async with index_guard(store.registry_root):
        await store.record_outcome({nid: entry["status"] for nid, entry in manifest.items()}, summary="ok")

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]
    run = await read_run(LocalFileBackend(), str(dag_root(session_dir)), RUN_ID, str(nodes_root(session_dir)))

    ours = {node["node_id"]: _branch_part(node) for node in row["nodes"]}
    dag_get = {entry["node"]: _branch_part(entry) for entry in run["files"]}
    assert ours == dag_get
    assert ours == {gnode["id"]: branch_fields(manifest[gnode["id"]], gnode) for gnode in graph["nodes"]}
    assert ours["unset"]["select"] == "one", "the effective value, as the run-started event says it"
    for nid in ("list_routes", "empty_routes", "stray_select", "plain"):
        assert not {"routes", "select"} & set(ours[nid]), f"{nid} is no decision, whatever else the file says"
    for nid in ("blank_when", "non_text_when", "plain"):
        assert "when" not in ours[nid], f"{nid} has no guard"
    assert ours["real_when"] == {"when": "unset.a", "reason": "when_unknown"}


async def test_a_run_not_yet_finalized_names_what_it_asked_and_not_yet_what_it_decided(workspace: Path) -> None:
    """With no manifest and no decision on any registry row there is no outcome to
    read: the declarations are graph.json's and are there from the first poll, and
    the reason and selection arrive with the row that records the decision. Every
    field stays optional on the wire for exactly this."""
    session_dir = _session_dir(workspace)
    await _make_run(session_dir, RUN_ID, _BRANCHING_GRAPH, ["triage", "fix", "reply"])
    loop = _loop_stub(live_run_ids=frozenset({RUN_ID}))

    row = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]

    by_node = {n["node_id"]: n for n in row["nodes"]}
    assert _branch_part(by_node["triage"]) == {"routes": ["bug", "docs"], "select": "one"}
    assert _branch_part(by_node["fix"]) == {"when": "triage.bug"}
    assert _branch_part(by_node["reply"]) == {}


async def test_a_run_not_yet_finalized_reads_what_a_branch_decided_off_its_registry_rows(workspace: Path) -> None:
    """Until the manifest is written, a node's registry row is the only record of what
    a branch decided, and the tasks tab reads it from there after a reload."""
    from raven.agent.subagent.instances import get_registry

    session_dir = _session_dir(workspace)
    await _make_run(session_dir, RUN_ID, _BRANCHING_GRAPH, ["triage", "fix", "reply"])
    registry = get_registry()
    await registry.upsert_dag_node(
        SESSION, RUN_ID, "triage", "Raven", "completed", selected_routes=["docs"], route_evidence="asks how to set it"
    )
    await registry.upsert_dag_node(SESSION, RUN_ID, "fix", "Raven", "not_executed", reason="when_false")
    await registry.upsert_dag_node(SESSION, RUN_ID, "reply", "Raven", "running")
    loop = _loop_stub(live_run_ids=frozenset({RUN_ID}))

    result = await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop))
    TasksListResult.model_validate(result)

    by_node = {n["node_id"]: n for n in result["tasks"][0]["nodes"]}
    assert _branch_part(by_node["triage"]) == {
        "routes": ["bug", "docs"],
        "select": "one",
        "selected_routes": ["docs"],
        "route_evidence": "asks how to set it",
    }
    assert _branch_part(by_node["fix"]) == {"reason": "when_false", "when": "triage.bug"}
    assert _branch_part(by_node["reply"]) == {}


async def test_a_nodes_manifest_entry_outranks_its_registry_row(workspace: Path) -> None:
    """Once the manifest names a node it is that node's whole account, even where its row says more."""
    from raven.agent.subagent.instances import get_registry

    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _BRANCHING_GRAPH, ["triage", "fix", "reply"])
    await store.write_manifest(
        {
            "triage": _manifest_entry(status="completed"),
            "fix": _manifest_entry(status="completed", depends_on=["triage"]),
            "reply": _manifest_entry(status="completed", depends_on=["triage"]),
        }
    )
    await get_registry().upsert_dag_node(SESSION, RUN_ID, "triage", "Raven", "completed", selected_routes=["bug"])
    await get_registry().upsert_dag_node(SESSION, RUN_ID, "fix", "Raven", "not_executed", reason="when_false")

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]

    by_node = {n["node_id"]: n for n in row["nodes"]}
    assert _branch_part(by_node["triage"]) == {"routes": ["bug", "docs"], "select": "one"}
    assert _branch_part(by_node["fix"]) == {"when": "triage.bug"}


async def test_a_real_branching_run_reaches_the_wire_and_the_task_row_alike(workspace: Path) -> None:
    """The tests above feed the spine and the reader dicts typed by hand. This one
    takes what the runner really emits and really writes, so a key the runner
    renamed fails here instead of in a client, and the three surfaces -- the live
    frames, the closing frame and the reloaded row -- have to say the same thing."""
    from pydantic import TypeAdapter

    from raven.agent.subagent.dag_graph import parse_dag_spec
    from raven.agent.subagent.dag_runner import run_dag
    from raven.agent.subagent.dag_verdict import Verdict
    from raven.rpc.models import TurnEvent
    from raven.rpc.spine import _DAG_WIRE_EVENT, _dag_payload

    class _Echo:
        async def run(self, task: str, *, task_id: str, workspace: Any, executor: Any, **kwargs: Any) -> str:
            return f"OUT[{task_id}]"

    async def judge(*, node: Any, store: Any, output: Any, error: Any, crashed: Any, output_limited: bool = False):
        if node.routes:
            return Verdict(accomplished=True, routes=("docs",), route_evidence="the reporter asks how to configure it")
        return Verdict(accomplished=True)

    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(name: str, payload: dict[str, Any]) -> None:
        published.append((name, payload))

    def node(node_id: str, **extra: Any) -> dict[str, Any]:
        return {"id": node_id, "subagent": "x", "node_summary": node_id, "prompt_template": node_id, **extra}

    spec = parse_dag_spec(
        {
            "task_summary": "handle an issue",
            "nodes": [
                node("triage", routes={"bug": "a defect", "docs": "a question"}),
                node("fix", depends_on=["triage"], when="triage.bug"),
                node("verify", depends_on=["fix"]),
                node("reply", depends_on=["triage"], when="triage.docs"),
            ],
        }
    )
    session_dir = _session_dir(workspace)
    result = await run_dag(
        spec,
        resolve=lambda _node: _Echo(),
        backend=LocalFileBackend(),
        workdir=str(workspace),
        run_root=str(dag_root(session_dir)),
        nodes_root=str(nodes_root(session_dir)),
        history_root=str(session_history_root(session_dir)),
        judge_node=judge,
        progress_publisher=publish,
        session_key=SESSION,
    )
    # `run_subagent_dag` publishes this one, from the run's own result.
    published.append(
        (
            "dag_run_completed",
            {
                "run_id": result.run_id,
                "manifest": {"dir": result.dir, "files": result.files, "summary": result.summary},
            },
        )
    )
    frames = [(name, _dag_payload(name, payload)) for name, payload in published]
    for name, frame in frames:
        TypeAdapter(TurnEvent).validate_python({"type": _DAG_WIRE_EVENT[name], "payload": frame})

    started = next(frame for name, frame in frames if name == "dag_run_started")
    declared = {n["id"]: _branch_part(n) for n in started["nodes"]}
    assert declared == {
        "triage": {"routes": ["bug", "docs"], "select": "one"},
        "fix": {"when": "triage.bug"},
        "verify": {},
        "reply": {"when": "triage.docs"},
    }

    updates = {(f["node"], f["status"]): f for name, f in frames if name == "dag_node_updated"}
    assert _branch_part(updates["triage", "completed"]) == {
        "selected_routes": ["docs"],
        "route_evidence": "the reporter asks how to configure it",
    }
    assert _branch_part(updates["fix", "not_executed"]) == {"reason": "when_false"}
    assert _branch_part(updates["verify", "not_executed"]) == {"reason": "upstream_not_executed"}

    closing = next(frame for name, frame in frames if name == "dag_run_completed")
    outcome = ("reason", "selected_routes", "route_evidence")
    closed = {f["node"]: {k: f[k] for k in outcome if k in f} for f in closing["files"]}
    assert closed == {
        "triage": {"selected_routes": ["docs"], "route_evidence": "the reporter asks how to configure it"},
        "fix": {"reason": "when_false"},
        "verify": {"reason": "upstream_not_executed"},
        "reply": {},
    }
    assert closing["summary"]["not_executed"] == 2

    reloaded = await tasks_list({"session_key": SESSION})
    TasksListResult.model_validate(reloaded)
    row = reloaded["tasks"][0]
    assert row["status"] == "completed"
    assert row["counts"]["not_executed"] == 2
    assert {n["node_id"]: {k: n[k] for k in outcome if k in n} for n in row["nodes"]} == closed
    assert {n["node_id"]: {k: n[k] for k in ("routes", "select", "when") if k in n} for n in row["nodes"]} == {
        "triage": {"routes": ["bug", "docs"], "select": "one"},
        "fix": {"when": "triage.bug"},
        "verify": {},
        "reply": {"when": "triage.docs"},
    }


async def test_a_hard_stopped_run_clears_the_stamp_of_a_not_executed_node_too(workspace: Path) -> None:
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _BRANCHING_GRAPH, ["triage", "fix", "reply"])
    async with index_guard(store.registry_root):
        await store.record_outcome(
            {"triage": "completed", "fix": "not_executed", "reply": "cancelled"}, summary="stopped"
        )

    row = (await tasks_list({"session_key": SESSION}))["tasks"][0]

    assert [n["status"] for n in row["nodes"]] == ["completed", "not_executed", "cancelled"]
    assert row["nodes"][1]["started_at"] is None and row["nodes"][1]["ended_at"] is None
    assert row["counts"]["not_executed"] == 1


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


async def test_a_created_and_a_removed_file_pass_through_from_the_manifest(workspace: Path) -> None:
    """The two ops a node can record that name no file the reader can open.

    A removal has no size -- the file is gone, and a zero there would read as a
    file that is present and empty -- so the wire model has to accept a null one
    for these rows to reach a client at all.
    """
    session_dir = _session_dir(workspace)
    store = await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    files = [
        {"path": "new.md", "op": "add", "add": 4, "del": 0, "size": 18},
        {"path": "gone.md", "op": "delete", "add": 0, "del": 6, "size": None},
    ]
    await store.write_manifest(
        {
            "n1": _manifest_entry(status="completed", files=files),
            "n2": _manifest_entry(status="completed", depends_on=["n1"]),
        }
    )

    result = await tasks_list({"session_key": SESSION})
    TasksListResult.model_validate(result)
    assert result["tasks"][0]["nodes"][0]["files"] == files


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


def test_tool_call_count_reads_only_the_listed_calls() -> None:
    """Deliberate flip: this used to pin ``_tool_counts`` and its failure
    tally. Failures are the sub-agent's to report now, so the helper derives
    the call count alone -- a record that still carries ``tool_failures`` is
    evidence the field is ignored, not a second number to publish."""
    assert tasks_mod._tool_call_count({}) is None
    assert tasks_mod._tool_call_count({"tool_calls": "not a list"}) is None
    assert tasks_mod._tool_call_count({"tool_calls": ["a", "b"]}) == 2
    assert tasks_mod._tool_call_count({"tool_calls": ["a", "b"], "tool_failures": ["b"]}) == 2


def test_clip_only_clips_strings() -> None:
    assert tasks_mod._clip(None) is None
    assert tasks_mod._clip(12) is None
    assert tasks_mod._clip("x" * 600) == "x" * 500


def test_run_id_epoch_ms_reads_the_utc_prefix_and_refuses_the_rest() -> None:
    assert tasks_mod._run_id_epoch_ms("20260918T000000000000Z-aaaaaaaa") == 1789689600000
    assert tasks_mod._run_id_epoch_ms("20260918T000000500000Z-aaaaaaaa") == 1789689600500
    assert tasks_mod._run_id_epoch_ms("e2e_spawn_ok") is None
    assert tasks_mod._run_id_epoch_ms("20261399T000000000000Z-aaaaaaaa") is None


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


# ---------------------------------------------------------------------------
# a running node's usage, counts and files, off the live activity
# ---------------------------------------------------------------------------


async def test_a_running_spawn_reads_usage_and_counts_off_the_live_activity(workspace: Path) -> None:
    """The record carries usage only once ``finish`` writes ``as_meta``; while
    the run is in flight the only account is the activity being collected in
    this process, and the row reads that -- and reads nothing again once the
    collecting block has closed without the record having been finished."""
    from raven.agent.subagent import activity as activity_mod

    session_dir = _session_dir(workspace)
    await _claim_spawn_node(session_dir, "counting")
    SpawnRecord.open(
        session_dir,
        task_id="counting",
        task="count things",
        meta=_spawn_meta(agent="Raven", handle="counting", task_summary="Counting"),
        node_id="counting",
    )
    loop = _loop_stub(live_spawn_handles=frozenset({("Raven-General", "counting")}))

    with activity_mod.collecting(live_key=spawn_live_key(nodes_root(session_dir), "counting")):
        activity_mod.note_usage({"prompt_tokens": 40, "completion_tokens": 2})
        activity_mod.note_usage({"prompt_tokens": 10, "completion_tokens": 3})
        activity_mod.note_tool_call("exec")
        activity_mod.note_tool_call("write_file")
        activity_mod.note_tool_failure("exec")
        activity_mod.note_file_change("notes/a.md", "write", 3, 0, 12)
        node = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]["nodes"][0]

    assert node["status"] == "running"
    assert node["tokens_in"] == 50 and node["tokens_out"] == 5
    assert node["tool_call_count"] == 2
    assert "tool_failure_count" not in node
    assert node["files"] == [{"path": "notes/a.md", "op": "write", "add": 3, "del": 0, "size": 12}]

    after = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]["nodes"][0]
    assert after["tokens_in"] is None and after["tool_call_count"] is None and after["files"] == []


async def test_a_running_spawns_created_and_removed_files_reach_the_row(workspace: Path) -> None:
    """The spawn lane carries the same two ops the manifest does, and a removal
    reaches the row with no size at all -- the file is gone, and the reader has
    to be told that rather than shown a zero-byte file."""
    from raven.agent.subagent import activity as activity_mod

    session_dir = _session_dir(workspace)
    await _claim_spawn_node(session_dir, "cleaning")
    SpawnRecord.open(
        session_dir,
        task_id="cleaning",
        task="tidy up",
        meta=_spawn_meta(agent="Raven", handle="cleaning", task_summary="Cleaning"),
        node_id="cleaning",
    )
    loop = _loop_stub(live_spawn_handles=frozenset({("Raven-General", "cleaning")}))

    with activity_mod.collecting(live_key=spawn_live_key(nodes_root(session_dir), "cleaning")):
        activity_mod.note_file_change("notes/new.md", "add", 4, 0, 18)
        activity_mod.note_file_change("notes/old.md", "delete", 0, 6, None)
        result = await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop))

    TasksListResult.model_validate(result)
    assert result["tasks"][0]["nodes"][0]["files"] == [
        {"path": "notes/new.md", "op": "add", "add": 4, "del": 0, "size": 18},
        {"path": "notes/old.md", "op": "delete", "add": 0, "del": 6, "size": None},
    ]


async def test_a_lane_that_has_not_spoken_keeps_its_nulls_while_live(workspace: Path) -> None:
    """A live activity with nothing reported yet is not "zero": the row says
    nothing rather than a count the lane never gave."""
    from raven.agent.subagent import activity as activity_mod

    session_dir = _session_dir(workspace)
    await _claim_spawn_node(session_dir, "quiet")
    SpawnRecord.open(
        session_dir,
        task_id="quiet",
        task="say nothing yet",
        meta=_spawn_meta(agent="Raven", handle="quiet", task_summary="Quiet"),
        node_id="quiet",
    )
    loop = _loop_stub(live_spawn_handles=frozenset({("Raven-General", "quiet")}))

    with activity_mod.collecting(live_key=spawn_live_key(nodes_root(session_dir), "quiet")):
        node = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]["nodes"][0]

    assert node["tokens_in"] is None and node["tokens_out"] is None
    assert node["tool_call_count"] is None
    assert "tool_failure_count" not in node
    assert node["files"] == []


async def test_a_running_dag_node_reads_usage_off_the_live_activity(workspace: Path) -> None:
    """The dag runner collects a node's activity under ``node_live_key``; the
    row reads that key, so a running node's usage grows before the manifest
    (which a finalized run writes) exists at all."""
    from raven.agent.subagent import activity as activity_mod
    from raven.agent.subagent.dag_store import node_live_key
    from raven.agent.subagent.instances import get_registry

    session_dir = _session_dir(workspace)
    await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    await get_registry().upsert_dag_node(SESSION, RUN_ID, "n1", "Raven", "running")
    loop = _loop_stub(live_run_ids=frozenset({RUN_ID}))

    with activity_mod.collecting(live_key=node_live_key(RUN_ID, "n1")):
        activity_mod.note_usage({"input_tokens": 7, "output_tokens": 1})
        activity_mod.note_tool_call("read_file")
        row = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]

    n1, n2 = row["nodes"]
    assert n1["status"] == "running" and n1["tokens_in"] == 7 and n1["tokens_out"] == 1
    assert n1["tool_call_count"] == 1
    assert "tool_failure_count" not in n1
    assert n2["tokens_in"] is None and n2["tool_call_count"] is None


async def test_a_dag_node_that_finished_mid_run_reads_the_account_the_runner_set_aside(workspace: Path) -> None:
    """Between a node's end and the run's manifest, the node's account lives in
    the settled index; the row reads it there, and reads nothing once the run
    has forgotten it (the manifest carries it by then)."""
    from raven.agent.subagent import activity as activity_mod
    from raven.agent.subagent.dag_store import node_live_key
    from raven.agent.subagent.instances import get_registry

    session_dir = _session_dir(workspace)
    await _make_run(session_dir, RUN_ID, _GRAPH, ["n1", "n2"])
    await get_registry().upsert_dag_node(SESSION, RUN_ID, "n1", "Raven", "completed")
    await get_registry().upsert_dag_node(SESSION, RUN_ID, "n2", "Raven", "running")
    loop = _loop_stub(live_run_ids=frozenset({RUN_ID}))
    key = node_live_key(RUN_ID, "n1")
    activity_mod.record_settled(
        key,
        {
            "tokens_in": 7,
            "tokens_out": 1,
            "tool_calls": ["read_file"],
            "tool_failures": ["read_file"],
            "files": [{"path": "a.md", "op": "write", "add": 1, "del": 0, "size": 5}],
        },
    )
    try:
        row = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]
    finally:
        activity_mod.forget_settled([key])

    n1, n2 = row["nodes"]
    assert n1["status"] == "completed"
    assert n1["tokens_in"] == 7 and n1["tokens_out"] == 1
    assert n1["tool_call_count"] == 1
    assert "tool_failure_count" not in n1
    assert n1["files"] == [{"path": "a.md", "op": "write", "add": 1, "del": 0, "size": 5}]
    assert n2["tokens_in"] is None and n2["tool_call_count"] is None

    after = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]["nodes"][0]
    assert after["tokens_in"] is None and after["tool_call_count"] is None and after["files"] == []


async def test_a_settled_spawn_ignores_a_live_activity_under_its_key(workspace: Path) -> None:
    """A settled row's account is the record's, and an entry still in the live
    index under this record's address belongs to a run this row is not
    describing -- so a finished spawn keeps its on-disk nulls."""
    from raven.agent.subagent import activity as activity_mod

    session_dir = _session_dir(workspace)
    await _claim_spawn_node(session_dir, "shared")
    record = SpawnRecord.open(
        session_dir,
        task_id="shared",
        task="done already",
        meta=_spawn_meta(agent="Raven", handle="shared", task_summary="Shared"),
        node_id="shared",
    )
    record.finish(status="completed", output="done")

    with activity_mod.collecting(live_key=spawn_live_key(nodes_root(session_dir), "shared")):
        activity_mod.note_usage({"prompt_tokens": 40, "completion_tokens": 2})
        activity_mod.note_tool_call("exec")
        node = (await tasks_list({"session_key": SESSION}))["tasks"][0]["nodes"][0]

    assert node["status"] == "completed"
    assert node["tokens_in"] is None and node["tool_call_count"] is None and node["files"] == []


async def test_two_conversations_that_named_a_spawn_alike_each_read_their_own_live_run(workspace: Path) -> None:
    """A node id is unique for one conversation only and the live index is one
    per process: keyed by the record's address, two conversations running a
    spawn under the same id read their own account, and the first to finish
    takes nothing of the other's with it."""
    from raven.agent.subagent import activity as activity_mod
    from raven.session.manager import SessionManager

    other = "tui:other"
    dir_a = _session_dir(workspace)
    dir_b = SessionManager(workspace).session_dir(other)
    for session_dir in (dir_a, dir_b):
        await _claim_spawn_node(session_dir, "step1")
        SpawnRecord.open(
            session_dir,
            task_id="step1",
            task="the same first step",
            meta=_spawn_meta(agent="Raven", handle="step1", task_summary="Step one"),
            node_id="step1",
        )
    loop = _loop_stub(live_spawn_handles=frozenset({("Raven-General", "step1")}))

    with activity_mod.collecting(live_key=spawn_live_key(nodes_root(dir_a), "step1")):
        activity_mod.note_usage({"prompt_tokens": 10, "completion_tokens": 1})
        with activity_mod.collecting(live_key=spawn_live_key(nodes_root(dir_b), "step1")):
            activity_mod.note_usage({"prompt_tokens": 200, "completion_tokens": 2})
            a = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]["nodes"][0]
            b = (await tasks_list({"session_key": other}, agent_loop_factory=_factory(loop)))["tasks"][0]["nodes"][0]
            assert (a["tokens_in"], a["tokens_out"]) == (10, 1)
            assert (b["tokens_in"], b["tokens_out"]) == (200, 2)
        # B finished first: A's entry is still its own.
        a_after = (await tasks_list({"session_key": SESSION}, agent_loop_factory=_factory(loop)))["tasks"][0]["nodes"][
            0
        ]
        assert (a_after["tokens_in"], a_after["tokens_out"]) == (10, 1)
