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
    def __init__(self, bound: str | None = None) -> None:
        self.bound = bound
        self.committed: list[tuple[str, str, str, str, str]] = []
        self.statuses: list[tuple[str, str, str, str]] = []

    async def bind_if_absent(self, session_key, agent, handle, agent_id, *, kind="cli"):
        if self.bound is not None:
            return self.bound
        self.committed.append((session_key, agent, handle, agent_id, kind))
        self.bound = agent_id
        return agent_id

    async def commit(self, session_key, agent, handle, agent_id, *, kind="cli"):
        raise AssertionError("a pick-up binds where nothing is bound; it never commits over a binding")

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
    # The summary stands for the task: nothing authored was recorded, and the prompt file is never quoted.
    assert said["status"] == "ok" and said["result"].startswith("pong") and said["task"] == "say hi"
    assert said["origin"] == {
        "session_key": "tui:s1",
        "channel": "tui",
        "chat_id": "s1",
        "agent": "stub@box",
        "handle": "t1",
        "instance": None,
        "node_id": "n1",
    }
    assert said["summary"] == "say hi"
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


async def test_two_processes_starting_together_pick_a_run_up_once(tmp_path, monkeypatch, logs):
    """A gateway and a page stack of one home start at once: the run is picked up by one and reported once."""
    backend = _backend()
    managers = [_Manager(backend, tmp_path / "s1"), _Manager(backend, tmp_path / "s1")]
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())
    _open_record(tmp_path / "s1")
    _run()
    picked_by: list[str] = []

    async def picked(run, backend, *, pool=None):
        picked_by.append(run.task)
        await asyncio.sleep(0.1)
        return remote_resume.PickedUp("ok", "pong")

    monkeypatch.setattr(remote_resume, "pick_up", picked)

    counts = await asyncio.gather(*(resume_remote.resume_remote_runs(manager) for manager in managers))

    assert sorted(counts) == [0, 1] and picked_by == ["stub@box-1a2b3c4d"]
    assert sum(len(manager.announced) for manager in managers) == 1
    assert "is being picked up by another process of this computer" in "\n".join(logs)
    assert remote_runs.pending() == [] and not remote_runs.claim_path("stub@box-1a2b3c4d").exists()


async def test_a_stop_during_a_pick_up_keeps_that_record_and_the_ones_after_it(tmp_path, monkeypatch, logs):
    from raven.acp_client import remote

    manager = _Manager(_backend(), tmp_path / "s1")
    _run(task="first", started=1.0)
    _run(task="second", started=2.0)
    asked: list[str] = []

    async def picked(run, backend, *, pool=None):
        asked.append(run.task)
        remote.leave_agents_running()
        raise remote_resume.UnreachableError("this computer stopped while the run was being picked up")

    monkeypatch.setattr(remote_resume, "pick_up", picked)
    try:
        assert await resume_remote.resume_remote_runs(manager) == 0
    finally:
        remote.staying()

    assert asked == ["first"] and manager.announced == []
    assert [run.task for run in remote_runs.pending()] == ["first", "second"]
    assert remote_runs.claim("first") and remote_runs.claim("second"), "nothing is left claimed"
    assert "wait for the next start" in "\n".join(logs)


def _picked(status: str, reply: str):
    async def pick_up(run, backend, *, pool=None):
        return remote_resume.PickedUp(status, reply)

    return pick_up


async def test_a_resumed_announce_carries_the_dispatch_s_origin_and_quotes_the_task_as_authored(tmp_path, monkeypatch):
    """What the announce here would have said: instance, handle, node and the task as the model wrote it, never the rendered prompt."""
    backend = _backend()
    session_dir = tmp_path / "s1"
    record = _open_record(session_dir, handle="r1")
    record.file("prompt.md").write_text(
        "say hi to the machine\n\n<the whole referenced file, rendered>", encoding="utf-8"
    )
    origin = {
        "channel": "tui",
        "chat_id": "s1",
        "session_key": "tui:s1",
        "agent": "stub@box",
        "instance": "r1",
        "instance_auto": False,
        "handle": "r1",
        "workspace": "/w",
        "authored_task": "say hi {{ ref:notes.md }}",
        "tool_call_id": "call-9",
        "node_id": "n1",
    }
    run = _run(handle="r1", authored_task="say hi {{ ref:notes.md }}", task_summary="say hi", origin=origin)
    manager = _Manager(backend, session_dir)
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())
    monkeypatch.setattr(remote_resume, "pick_up", _picked("ok", "pong"))

    assert await resume_remote.resume_remote_runs(manager) == 1

    (said,) = manager.announced
    assert said["task"] == "say hi {{ ref:notes.md }}" and said["summary"] == "say hi"
    assert said["origin"]["instance"] == "r1" and said["origin"]["node_id"] == "n1"
    assert said["origin"]["tool_call_id"] == "call-9" and said["origin"]["authored_task"] == "say hi {{ ref:notes.md }}"
    assert said["origin"]["chat_id"] == "s1" and said["task_id"] == run.task_id


async def test_without_the_manager_s_fields_the_origin_comes_from_the_record_and_the_prompt_file_is_never_quoted(
    tmp_path, monkeypatch
):
    """A host killed outright never annotated the record: the spawn record names the node, the handle is the instance, the summary stands for the task."""
    backend = _backend()
    session_dir = tmp_path / "s1"
    record = _open_record(session_dir, handle="r1", node_id="n7")
    record.file("prompt.md").write_text("say hi\n\n<the whole referenced file, rendered>", encoding="utf-8")
    _run(handle="r1")
    manager = _Manager(backend, session_dir)
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())
    monkeypatch.setattr(remote_resume, "pick_up", _picked("ok", "pong"))

    assert await resume_remote.resume_remote_runs(manager) == 1

    (said,) = manager.announced
    assert said["task"] == "say hi" and "rendered" not in said["task"]
    assert said["origin"]["node_id"] == "n7" and said["origin"]["instance"] == "r1" and said["origin"]["handle"] == "r1"
    assert said["origin"]["agent"] == "stub@box" and said["origin"]["session_key"] == "tui:s1"


async def test_the_record_goes_only_once_the_announce_is_taken_and_stays_when_the_scheduler_refuses_it(
    tmp_path, monkeypatch
):
    backend = _backend()
    session_dir = tmp_path / "s1"
    _open_record(session_dir)
    run = _run()
    manager = _Manager(backend, session_dir)
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())
    monkeypatch.setattr(remote_resume, "pick_up", _picked("ok", "pong"))
    seen: list[list[str]] = []

    async def refused(*args, **kwargs):
        seen.append([kept.task for kept in remote_runs.pending()])
        return False

    manager._announce_result = refused

    assert await resume_remote.resume_remote_runs(manager) == 0

    assert seen == [[run.task]], "the record is still there while the announce runs"
    assert [kept.task for kept in remote_runs.pending()] == [run.task], "and stays for the next start"
    assert remote_runs.claim(run.task), "with its claim released"


async def test_a_conclusion_that_fails_keeps_the_record_and_releases_the_claim(tmp_path, monkeypatch, logs):
    backend = _backend()
    session_dir = tmp_path / "s1"
    _open_record(session_dir)
    run = _run()
    manager = _Manager(backend, session_dir)

    async def boom(session_key, record):
        raise RuntimeError("the index is locked")

    manager._finalize_node_claim = boom
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())
    monkeypatch.setattr(remote_resume, "pick_up", _picked("ok", "pong"))

    assert await resume_remote.resume_remote_runs(manager) == 0

    assert [kept.task for kept in remote_runs.pending()] == [run.task] and manager.announced == []
    assert remote_runs.claim(run.task)
    assert any("its record stays for the next start" in line for line in logs)


async def test_a_cli_run_is_picked_up_by_its_cli_entry_and_an_acp_record_is_not_a_cli_entry_s(tmp_path, monkeypatch):
    from raven.agent.subagent.backends import build_third_party_backend
    from raven.config.schema import ThirdPartyCliSubagentConfig

    cli = build_third_party_backend(
        ThirdPartyCliSubagentConfig(name="sh@box", command="cat", machine="box", remote_cwd="~/raven-work")
    )
    picked: list[str] = []

    async def pick_up(run, *, pool=None):
        picked.append(run.task)
        return remote_resume.PickedUp("ok", "one\ntwo")

    monkeypatch.setattr(cli, "pick_up", pick_up)
    session_dir = tmp_path / "s1"
    _open_record(session_dir, agent="sh@box")
    run = _run(
        kind="cli", agent="sh@box", session_id="", prompt_id=0, next_id=0, initialize=None, launch_key="", config_key=""
    )
    manager = _Manager(cli, session_dir)
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())

    assert await resume_remote.resume_remote_runs(manager) == 1
    assert picked == [run.task] and manager.announced[0]["result"] == "one\ntwo"

    _run(task="stub@box-2", agent="sh@box")
    assert await resume_remote.resume_remote_runs(manager) == 1
    assert "no longer on the agent table as an acp agent" in manager.announced[-1]["result"]


@pytest.mark.parametrize("kind", ["acp", "cli"])
@pytest.mark.parametrize("value", [None, "0"])
async def test_pending_machine_runs_resume_without_a_debug_switch(tmp_path, monkeypatch, kind, value):
    from raven.agent.subagent.registry import AgentRegistry
    from raven.config.schema import ThirdPartyAcpSubagentConfig, ThirdPartyCliSubagentConfig

    if value is None:
        monkeypatch.delenv("RAVEN_DEBUG_MACHINES", raising=False)
    else:
        monkeypatch.setenv("RAVEN_DEBUG_MACHINES", value)
    cls = ThirdPartyAcpSubagentConfig if kind == "acp" else ThirdPartyCliSubagentConfig
    cfg = cls(name="stub@box", command="cat", machine="box")
    registry = AgentRegistry(build_builtin=lambda row, narrowed: ("backend", row.name))
    registry.apply([cfg])
    manager = _Manager(None, tmp_path / "s1")
    manager.registry = registry
    run = _run(kind=kind)
    picked = []

    async def pick_up(saved, backend):
        picked.append(saved.task)
        assert backend.machine == "box"
        return remote_resume.PickedUp("ok", "recovered answer")

    monkeypatch.setattr(resume_remote, "_pick_up", pick_up)
    monkeypatch.setattr(resume_remote, "get_registry", lambda: _Registry())
    assert await resume_remote.resume_remote_runs(manager) == 1
    assert picked == [run.task]
    assert manager.announced[0]["status"] == "ok"
    assert manager.announced[0]["result"] == "recovered answer"
    assert remote_runs.pending() == []


async def test_a_minted_id_is_bound_only_where_nothing_is_bound_so_a_newer_turn_s_binding_is_kept(
    tmp_path, monkeypatch
):
    """The provisioned twin of the cli pick-up's rule: `_conclude` binds the record's session id where the handle is
    unbound, and keeps the binding a dispatch made while the pick-up waited on the machine."""
    kept = _Registry(bound="newer-turn")
    monkeypatch.setattr(resume_remote, "get_registry", lambda: kept)
    manager = _Manager(_backend(), tmp_path / "s1")
    _open_record(tmp_path / "s1")

    assert (
        await resume_remote._conclude(manager, _run(stateful=True, session_id="minted-X"), "ok", "the answer") is True
    )

    assert kept.committed == [] and kept.bound == "newer-turn", "the newer binding stays"
    assert manager.announced[0]["status"] == "ok"

    fresh = _Registry()
    monkeypatch.setattr(resume_remote, "get_registry", lambda: fresh)
    _open_record(tmp_path / "s2")
    assert (
        await resume_remote._conclude(
            _Manager(_backend(), tmp_path / "s2"), _run(stateful=True, session_id="minted-X"), "ok", "x"
        )
        is True
    )
    assert fresh.committed == [("tui:s1", "stub@box", "t1", "minted-X", "acp")], "unbound: the minted id binds"
