"""raven.agent.subagent.resume_remote: the runs a process before this one left on machines, concluded here."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.acp_client import remote_resume, remote_runs
from raven.acp_client.acp_agent import AcpAgentBackend
from raven.agent.subagent import resume_remote
from raven.agent.subagent.history import SpawnRecord, nodes_root


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(resume_remote, "_SCHEDULED", False)
    return tmp_path / "home"


@pytest.fixture
def logs():
    """What raven logged while the test ran, as loguru's sink hears it."""
    from loguru import logger

    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="INFO")
    try:
        yield records
    finally:
        logger.remove(sink)


class _Registry:
    def __init__(self) -> None:
        self.committed: list[tuple[str, str, str, str, str]] = []
        self.statuses: list[tuple[str, str, str, str]] = []

    async def commit(self, session_key, agent, handle, agent_id, *, kind="cli"):
        self.committed.append((session_key, agent, handle, agent_id, kind))

    async def upsert_spawn(self, session_key, agent, handle, status, agent_id=None):
        self.statuses.append((session_key, agent, handle, status))


class _Manager:
    def __init__(self, backend: Any, session_dir: Path) -> None:
        self.registry = SimpleNamespace(
            backend=lambda name: backend if backend is not None and name == backend.name else None
        )
        self._session_dir = session_dir
        self.announced: list[dict[str, Any]] = []
        self.finalized: list[str] = []
        self.remembered: list[str] = []

    def session_dir_for(self, session_key: str) -> Path:
        return self._session_dir

    async def _finalize_node_claim(self, session_key, record) -> None:
        self.finalized.append(record.node_id)

    def remember_session(self, session_key: str) -> None:
        self.remembered.append(session_key)

    async def _announce_result(self, task_id, summary, task, result, origin, status, record_path=None, activity=None):
        self.announced.append(
            {
                "task_id": task_id,
                "summary": summary,
                "task": task,
                "result": result,
                "origin": origin,
                "status": status,
                "record": record_path,
            }
        )


def _backend(name: str = "stub@box", machine: str = "box") -> AcpAgentBackend:
    return AcpAgentBackend(name=name, command="stub", machine=machine)


def _run(**kw) -> remote_runs.RemoteRun:
    fields = dict(
        machine="box",
        task="stub@box-1a2b3c4d",
        agent="stub@box",
        session_key="tui:s1",
        handle="t1",
        task_id="t1",
        session_id="sess-1",
        prompt_id=3,
        next_id=4,
        initialize={},
        launch_key="k",
        config_key="c",
        session_cwd="/root/raven-work/t1",
        stateful=False,
    )
    fields.update(kw)
    run = remote_runs.RemoteRun(**fields)
    remote_runs.record(run)
    return run


def _open_record(session_dir: Path, *, agent: str = "stub@box", handle: str = "t1", node_id: str = "n1") -> SpawnRecord:
    meta = {"call_id": node_id, "session_key": "tui:s1", "agent": agent, "handle": handle, "task_summary": "say hi"}
    return SpawnRecord.open(session_dir, task_id="t1", task="say hi to the machine", meta=meta, node_id=node_id)


async def test_a_picked_up_reply_finishes_the_dispatch_s_record_and_is_announced_to_its_conversation(
    tmp_path, monkeypatch
):
    backend = _backend()
    manager = _Manager(backend, tmp_path / "s1")
    registry = _Registry()
    monkeypatch.setattr(resume_remote, "get_registry", lambda: registry)
    _open_record(tmp_path / "s1")
    run = _run(stateful=True)

    async def picked(got_run, got_backend, *, pool=None):
        assert got_run == run and got_backend is backend
        return remote_resume.PickedUp(
            "ok", "pong\n\n[raven] Ran on machine 'Lab box' (box), picked up after this computer restarted."
        )

    monkeypatch.setattr(remote_resume, "pick_up", picked)

    assert await resume_remote.resume_remote_runs(manager) == 1

    meta = json.loads((nodes_root(tmp_path / "s1") / "n1.meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed" and meta["agent"] == "stub@box"
    assert (nodes_root(tmp_path / "s1") / "n1.out.md").read_text(encoding="utf-8").startswith("pong")
    assert manager.finalized == ["n1"] and manager.remembered == ["tui:s1"]
    assert registry.committed == [("tui:s1", "stub@box", "t1", "sess-1", "acp")]
    (said,) = manager.announced
    assert said["status"] == "ok" and said["result"].startswith("pong") and said["task"] == "say hi to the machine"
    assert (
        said["origin"] == {"session_key": "tui:s1", "channel": "tui", "chat_id": "s1"} and said["summary"] == "say hi"
    )
    assert said["record"].endswith("n1.out.md")
    assert remote_runs.pending() == []


async def test_a_run_that_could_not_be_picked_up_is_announced_as_an_error_and_its_record_closed(tmp_path, monkeypatch):
    backend = _backend()
    manager = _Manager(backend, tmp_path / "s1")
    registry = _Registry()
    monkeypatch.setattr(resume_remote, "get_registry", lambda: registry)
    _open_record(tmp_path / "s1")
    _run()

    async def picked(run, backend, *, pool=None):
        return remote_resume.PickedUp("error", "machine 'Lab box' (box) no longer has the run")

    monkeypatch.setattr(remote_resume, "pick_up", picked)

    await resume_remote.resume_remote_runs(manager)

    meta = json.loads((nodes_root(tmp_path / "s1") / "n1.meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed"
    assert (nodes_root(tmp_path / "s1") / "n1.error.md").read_text(encoding="utf-8").startswith("Error: machine")
    assert registry.statuses == [("tui:s1", "stub@box", "t1", "failed")] and registry.committed == []
    (said,) = manager.announced
    assert said["status"] == "error" and said["result"].startswith("Error: machine")
    assert remote_runs.pending() == []


async def test_a_machine_that_cannot_be_asked_keeps_the_record_for_a_later_start(tmp_path, monkeypatch, logs):
    manager = _Manager(_backend(), tmp_path / "s1")
    _run()

    async def picked(run, backend, *, pool=None):
        raise remote_resume.UnreachableError("the Raven node on machine 'Lab box' (box) did not answer")

    monkeypatch.setattr(remote_resume, "pick_up", picked)

    assert await resume_remote.resume_remote_runs(manager) == 0

    assert [run.task for run in remote_runs.pending()] == ["stub@box-1a2b3c4d"]
    assert manager.announced == [] and "waits for a later start" in "\n".join(logs)


async def test_a_run_whose_agent_left_the_table_is_concluded_as_an_error_without_the_machine(tmp_path, monkeypatch):
    registry = _Registry()
    monkeypatch.setattr(resume_remote, "get_registry", lambda: registry)
    asked: list[Any] = []

    async def picked(run, backend, *, pool=None):
        asked.append(run)
        return remote_resume.PickedUp("ok", "never")

    monkeypatch.setattr(remote_resume, "pick_up", picked)
    _run()
    for manager in (_Manager(None, tmp_path / "s1"), _Manager(_backend(machine="other"), tmp_path / "s1")):
        remote_runs.record(_run())
        await resume_remote.resume_remote_runs(manager)
        (said,) = manager.announced
        assert (
            said["status"] == "error"
            and "no longer on the agent table as an acp agent on that machine" in said["result"]
        )
    assert asked == [] and remote_runs.pending() == []


async def test_a_failure_inside_the_pick_up_is_the_run_s_own_and_the_next_run_is_tried(tmp_path, monkeypatch):
    backend = _backend()
    manager = _Manager(backend, tmp_path / "s1")
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())
    _run(task="first", started=1.0)
    _run(task="second", started=2.0)

    async def picked(run, backend, *, pool=None):
        if run.task == "first":
            raise RuntimeError("boom")
        return remote_resume.PickedUp("ok", "pong")

    monkeypatch.setattr(remote_resume, "pick_up", picked)

    assert await resume_remote.resume_remote_runs(manager) == 2

    assert [said["status"] for said in manager.announced] == ["error", "ok"]
    assert "could not be picked up: boom" in manager.announced[0]["result"]


async def test_a_run_with_no_conversation_to_report_to_is_closed_quietly(tmp_path, monkeypatch, logs):
    backend = _backend()
    manager = _Manager(backend, tmp_path / "s1")
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())
    _run(session_key="default")

    async def picked(run, backend, *, pool=None):
        return remote_resume.PickedUp("ok", "pong")

    monkeypatch.setattr(remote_resume, "pick_up", picked)

    await resume_remote.resume_remote_runs(manager)

    assert manager.announced == [] and "has no conversation to report to" in "\n".join(logs)


async def test_the_pick_up_is_scheduled_once_per_process_after_a_delay_and_not_for_a_stand_in_manager(
    tmp_path, monkeypatch
):
    ran: list[Any] = []

    async def fake(manager):
        ran.append(manager)
        return 0

    monkeypatch.setattr(resume_remote, "resume_remote_runs", fake)
    manager = _Manager(_backend(), tmp_path / "s1")

    assert resume_remote.schedule_remote_resume(SimpleNamespace()) is None, (
        "a stand-in without sessions schedules nothing"
    )
    task = resume_remote.schedule_remote_resume(manager, delay_s=0.01)
    assert task is not None and resume_remote.schedule_remote_resume(manager, delay_s=0.01) is None
    await asyncio.wait_for(task, timeout=5)

    assert ran == [manager]


async def test_a_pick_up_that_raises_in_the_background_is_logged_not_raised(tmp_path, monkeypatch, logs):
    async def fail(manager):
        raise RuntimeError("no machines today")

    monkeypatch.setattr(resume_remote, "resume_remote_runs", fail)
    task = resume_remote.schedule_remote_resume(_Manager(_backend(), tmp_path / "s1"), delay_s=0.01)

    await asyncio.wait_for(task, timeout=5)

    assert "picking up the runs left on machines failed" in "\n".join(logs)
