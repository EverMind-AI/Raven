"""Where a session's sub-agent call history lands, for both delegation paths."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from raven.agent.subagent_history import SpawnRecord, dag_root, session_history_root, spawn_root
from raven.session.manager import SessionManager


def _session_dir(home: Path, key: str, project_slug: str | None = None) -> Path:
    return SessionManager(home, project_slug=project_slug).session_dir(key)


def test_history_sits_beside_the_session_transcript(tmp_path: Path) -> None:
    """The layout mirrors ``sessions/<group>/<chat_id>.jsonl`` one level down.

    A directory named ``<chat_id>`` and a file named ``<chat_id>.jsonl`` are
    different names, so the two coexist and the transcript globs
    (``sessions/*/*.jsonl``) never pick the history up.
    """
    d = _session_dir(tmp_path, "web:abc")
    assert session_history_root(d) == tmp_path / "sessions" / "web" / "abc" / "subagents"
    assert spawn_root(d).name == "spawn"
    assert dag_root(d).name == "mas_dag"
    assert spawn_root(d).parent == dag_root(d).parent


def test_history_follows_a_pre_grouping_transcript_to_its_old_group(tmp_path: Path) -> None:
    """A transcript written before project grouping keeps its channel directory,
    and its history has to follow it there.

    Deriving the group here instead would file the history under the slug this
    process picked while the transcript stayed put, so deleting the session
    would leave the audit records behind -- which is exactly what the
    CHANGELOG's retention note promises does not happen.
    """
    legacy = tmp_path / "sessions" / "cli"
    legacy.mkdir(parents=True)
    (legacy / "abc.jsonl").write_text(
        json.dumps({"_type": "metadata", "key": "cli:abc", "updated_at": "2026-08-10T00:00:00"}) + "\n",
        encoding="utf-8",
    )

    manager = SessionManager(tmp_path, project_slug="-srv-project-a")
    transcript = manager._get_session_path("cli:abc")
    root = session_history_root(manager.session_dir("cli:abc"))

    assert transcript.parent.name == "cli"
    assert root.parent == transcript.with_suffix("")
    assert "-srv-project-a" not in root.parts


@pytest.mark.parametrize(
    ("session_key", "tail"),
    [
        ("web:abc", ("web", "abc")),
        ("heartbeat", ("heartbeat", "_")),
        ("cron:nightly-report", ("cron", "nightly-report")),
        ("", ("_", "_")),
    ],
)
def test_session_key_shapes_map_to_stable_directories(tmp_path: Path, session_key: str, tail: tuple) -> None:
    root = session_history_root(_session_dir(tmp_path, session_key))
    assert root.parts[-len(tail) - 1 : -1] == tail


@pytest.mark.parametrize("session_key", ["web:..", "..:abc", "web:.", "..:.."])
def test_dot_only_key_segments_cannot_escape_the_sessions_tree(tmp_path: Path, session_key: str) -> None:
    """``safe_filename`` leaves ``..`` intact, which is fine for a name with a
    ``.jsonl`` suffix but not for the bare directory names used here."""
    root = session_history_root(_session_dir(tmp_path, session_key))
    assert (tmp_path / "sessions") in root.parents
    assert ".." not in root.parts


def test_spawn_record_writes_the_prompt_before_the_result(tmp_path: Path) -> None:
    """The prompt is on disk from the moment the record opens, so a call that
    never returns still shows what was asked."""
    d = _session_dir(tmp_path, "web:abc")
    record = SpawnRecord.open(d, task_id="t1", task="do the thing", meta={"agent": "Coder"})

    assert (record.dir / "prompt.md").read_text(encoding="utf-8") == "do the thing"
    assert not (record.dir / "out.md").exists()
    assert json.loads((record.dir / "meta.json").read_text(encoding="utf-8"))["status"] == "running"
    assert record.dir.parent == spawn_root(d)
    assert record.dir.name.endswith("-t1")


def test_spawn_record_finish_records_the_output(tmp_path: Path) -> None:
    record = SpawnRecord.open(_session_dir(tmp_path, "web:abc"), task_id="t1", task="ask", meta={"agent": "Coder"})
    record.finish(status="completed", output="the answer")

    assert (record.dir / "out.md").read_text(encoding="utf-8") == "the answer"
    meta = json.loads((record.dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["agent"] == "Coder"
    assert meta["ended_at_ms"] >= meta["started_at_ms"]


def test_spawn_record_keeps_failures(tmp_path: Path) -> None:
    """The failure case is the one worth keeping: without it, a sub-agent that
    died leaves nothing to debug from."""
    record = SpawnRecord.open(_session_dir(tmp_path, "web:abc"), task_id="t1", task="ask", meta={})
    record.finish(status="failed", error="Error: CLI agent 'Coder' exited 1: boom")

    assert "boom" in (record.dir / "error.md").read_text(encoding="utf-8")
    assert not (record.dir / "out.md").exists()
    assert json.loads((record.dir / "meta.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_spawn_record_survives_an_unwritable_root(tmp_path: Path) -> None:
    """History is an audit trail; losing it must never take down the run.

    A file where the history root should be is the cheap way to make every
    write fail without depending on permissions (tests may run as root, where
    a read-only mode bit is not enforced). Built without a ``SessionManager``
    for the same reason -- constructing one would create the directory this
    test needs to be absent.
    """
    blocker = tmp_path / "sessions"
    blocker.write_text("not a directory", encoding="utf-8")

    record = SpawnRecord.open(blocker / "web" / "abc", task_id="t1", task="ask", meta={})
    record.finish(status="completed", output="ignored")

    assert not record.dir.exists()


# ---------------------------------------------------------------------------
# ids carry the ordering, because nothing else does
# ---------------------------------------------------------------------------


def test_ids_minted_in_the_same_instant_still_sort_in_mint_order() -> None:
    """Readers sort `<stamp>-<suffix>` lexicographically and the suffix is a
    task id or random hex, so the stamp is the whole ordering.

    The suffixes here are deliberately reverse-ordered: with a stamp accurate
    only to the second -- or to the microsecond, which two spawns tie on often
    enough -- the older call sorts first, the panel reshuffles on every poll,
    and the listing test that asserts newest-first fails intermittently.
    """
    from raven.agent.subagent_history import make_call_id

    first = make_call_id("efd70c2c")
    second = make_call_id("a8f2a07a")

    assert first < second, f"{first} was minted first and must sort first"
    assert sorted([first, second], reverse=True) == [second, first]


def test_a_run_id_orders_against_a_call_id() -> None:
    """The panel sorts spawns and graph runs into one list, so the two id
    shapes have to be comparable -- a second-accurate stamp on either side
    reshuffles ties across both."""
    from raven.agent.subagent_dag._store import make_run_id
    from raven.agent.subagent_history import make_call_id

    call = make_call_id("aaaa1111")
    run = make_run_id()

    assert call < run, f"{call} was minted before {run} and must sort before it"


def test_direct_record_lands_under_the_instance(tmp_path: Path) -> None:
    from raven.agent.subagent.direct_chat import DirectChatRecord, direct_root

    record = DirectChatRecord.open(tmp_path, agent="Raven-Code", handle="refactor-auth", task_id="t1", task="do it")

    root = direct_root(tmp_path, "Raven-Code", "refactor-auth")
    assert record.dir.parent == root
    assert (record.dir / "prompt.md").read_text(encoding="utf-8") == "do it"
    assert not (record.dir / "out.md").exists()


def test_direct_record_shares_the_state_directory(tmp_path: Path) -> None:
    from raven.agent.subagent.direct_chat import direct_root
    from raven.agent.subagent.instance_state import instance_state_path

    root = direct_root(tmp_path, "A", "h")
    assert instance_state_path(tmp_path, "A", "h").parent == root


def test_direct_record_finish_writes_the_output(tmp_path: Path) -> None:
    from raven.agent.subagent.direct_chat import DirectChatRecord

    record = DirectChatRecord.open(tmp_path, agent="A", handle="h", task_id="t1", task="q")
    record.finish(status="completed", output="a")

    meta = json.loads((record.dir / "meta.json").read_text(encoding="utf-8"))
    assert (record.dir / "out.md").read_text(encoding="utf-8") == "a"
    assert meta["status"] == "completed"
    assert meta["agent"] == "A"
    assert meta["handle"] == "h"
    assert meta["ended_at_ms"] >= meta["started_at_ms"]


def test_direct_record_survives_an_unwritable_root(tmp_path: Path, monkeypatch) -> None:
    """History is an audit trail; losing it must never take down the turn."""
    from raven.agent.subagent.direct_chat import DirectChatRecord

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr("pathlib.Path.mkdir", boom)
    record = DirectChatRecord.open(tmp_path, agent="A", handle="h", task_id="t1", task="q")
    record.finish(status="completed", output="a")


@pytest.mark.asyncio
async def test_a_declared_identity_becomes_a_memory_record(tmp_path, monkeypatch) -> None:
    from raven.agent.subagent import manager as manager_mod

    calls: list[dict] = []

    async def _fake_record(**kwargs) -> None:
        calls.append(kwargs)
        await kwargs["write"]('{"agent": "Raven-Code", "status": "settled", "memories": []}')

    monkeypatch.setattr(manager_mod, "record_memories", _fake_record)

    d = _session_dir(tmp_path, "web:abc")
    record = SpawnRecord.open(d, task_id="t1", task="ask", meta={"agent": "Raven-Code"})
    record.finish(status="completed", output="done")

    await manager_mod.write_memory_record_for(
        directory=record.dir,
        filename="memory.json",
        agent="Raven-Code",
        identity=manager_mod.EverosIdentity(
            user_id="raven-code", agent_id=None, base_url="http://everos.test", session_prefix="cli:"
        ),
        resolve_session_id=_noop_key,
        budget_s=0.0,
    )

    assert json.loads((record.dir / "memory.json").read_text(encoding="utf-8"))["agent"] == "Raven-Code"
    assert calls and calls[0]["agent"] == "Raven-Code"


async def _noop_key() -> str:
    return "cli:abc"


# --- the spawn path's own scheduling, through the manager --------------------


class _StubProvider:
    def get_default_model(self) -> str:
        return "stub"


def _spawn_manager(tmp_path, *, agent: str, backend, identity=None):
    """A manager whose ``agent``'s backend is fully under the test's control."""
    from raven.agent.subagent.manager import SubagentManager
    from raven.config.schema import ThirdPartyCliSubagentConfig

    # Declared through config, not injected: the manager reads the identity off
    # the registry row, so a hand-placed map would test nothing.
    manager = SubagentManager(
        provider=_StubProvider(),
        workspace=tmp_path / "home",
        session_dir=lambda key: _session_dir(tmp_path, key),
        agents=[
            ThirdPartyCliSubagentConfig(
                name=agent,
                command="cat {agent_id}",
                resume_command="cat --resume {agent_id}",
                everos={"userId": agent, "agentId": agent} if identity is not None else None,
            )
        ],
    )
    manager.registry._backends[agent] = backend
    manager._submit = lambda *a, **k: None
    return manager


def _spawn_origin(agent: str, tmp_path) -> dict:
    return {
        "channel": "cli",
        "chat_id": "direct",
        "session_key": "s1",
        "agent": agent,
        "instance": None,
        "handle": "h1",
        "workspace": tmp_path,
    }


def _fake_record_calls(monkeypatch):
    from raven.agent.subagent import manager as manager_mod

    calls: list[dict] = []

    async def _fake_record(**kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(manager_mod, "record_memories", _fake_record)
    return calls


def _identity() -> "object":
    from raven.agent.subagent_memory import EverosIdentity

    return EverosIdentity(user_id="raven-code", agent_id=None, base_url="http://everos.test", session_prefix="cli:")


@pytest.mark.asyncio
async def test_a_completed_spawn_schedules_a_memory_record(tmp_path, monkeypatch) -> None:
    """The path a naive `finally` placement (before the `record.finish` branches
    existed) would have gotten right by accident and wrong on every other
    outcome -- this is the one that has to keep working.
    """

    class _Backend:
        async def run(self, task, **kw):
            return "ok"

    calls = _fake_record_calls(monkeypatch)
    manager = _spawn_manager(tmp_path, agent="Coder", backend=_Backend(), identity=_identity())

    await manager._run_subagent_inner(
        task_id="t1",
        task="do it",
        task_summary="l",
        origin=_spawn_origin("Coder", tmp_path),
        executor=None,
        provider=_StubProvider(),
        model="m",
    )
    await asyncio.gather(*list(manager._record_tasks))

    assert len(calls) == 1
    assert calls[0]["agent"] == "Coder"


@pytest.mark.asyncio
async def test_a_failed_spawn_still_schedules_a_memory_record(tmp_path, monkeypatch) -> None:
    class _Backend:
        async def run(self, task, **kw):
            raise RuntimeError("boom")

    calls = _fake_record_calls(monkeypatch)
    manager = _spawn_manager(tmp_path, agent="Coder", backend=_Backend(), identity=_identity())

    await manager._run_subagent_inner(
        task_id="t1",
        task="do it",
        task_summary="l",
        origin=_spawn_origin("Coder", tmp_path),
        executor=None,
        provider=_StubProvider(),
        model="m",
    )
    await asyncio.gather(*list(manager._record_tasks))

    assert len(calls) == 1
    assert calls[0]["agent"] == "Coder"


@pytest.mark.asyncio
async def test_a_cancelled_spawn_schedules_no_memory_record(tmp_path, monkeypatch) -> None:
    """A recorder scheduled after a cancellation sweep's snapshot is
    unreapable (F1) -- the fix is to never schedule one for a cancelled call.
    """

    class _Backend:
        async def run(self, task, **kw):
            raise asyncio.CancelledError()

    calls = _fake_record_calls(monkeypatch)
    manager = _spawn_manager(tmp_path, agent="Coder", backend=_Backend(), identity=_identity())

    with pytest.raises(asyncio.CancelledError):
        await manager._run_subagent_inner(
            task_id="t1",
            task="do it",
            task_summary="l",
            origin=_spawn_origin("Coder", tmp_path),
            executor=None,
            provider=_StubProvider(),
            model="m",
        )

    assert manager._record_tasks == set()
    assert calls == []


@pytest.mark.asyncio
async def test_a_spawn_with_no_declared_identity_schedules_no_memory_record(tmp_path, monkeypatch) -> None:
    class _Backend:
        async def run(self, task, **kw):
            return "ok"

    calls = _fake_record_calls(monkeypatch)
    manager = _spawn_manager(tmp_path, agent="Coder", backend=_Backend(), identity=None)

    await manager._run_subagent_inner(
        task_id="t1",
        task="do it",
        task_summary="l",
        origin=_spawn_origin("Coder", tmp_path),
        executor=None,
        provider=_StubProvider(),
        model="m",
    )

    assert manager._record_tasks == set()
    assert calls == []
