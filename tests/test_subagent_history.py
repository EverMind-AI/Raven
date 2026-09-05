"""Where a session's sub-agent call history lands, for both delegation paths."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from raven.agent.subagent import activity
from raven.agent.subagent.backends.base import clamp_output
from raven.agent.subagent.history import (
    SpawnRecord,
    add_turn_to_instance_log,
    dag_root,
    session_history_root,
    spawn_root,
)
from raven.agent.subagent.instance_log import instance_title
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
    transcript = manager.session_path("cli:abc")
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


def test_spawn_record_finish_is_idempotent(tmp_path: Path) -> None:
    """A cancel racing a completion runs finish twice; the first outcome wins --
    no duplicate instance-log turn, no clobbered status."""
    d = _session_dir(tmp_path, "web:abc")
    record = SpawnRecord.open(d, task_id="t1", task="do x", meta={"agent": "coder", "handle": "h1"})
    record.finish(status="completed", output="done")
    log = d / "subagents" / "instances" / "coder" / "h1.jsonl"
    lines_after_first = len(log.read_text(encoding="utf-8").splitlines())

    record.finish(status="cancelled")

    meta = json.loads((record.dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert len(log.read_text(encoding="utf-8").splitlines()) == lines_after_first


def test_a_running_spawn_already_says_what_it_is_for(tmp_path: Path) -> None:
    """The title has to be there while the run is still going, not after it.

    A reader opens an instance panel *because* the run is in flight, and the
    title used to be written by the first instance-log turn -- which lands at
    finish. Until then every surface fell back to the handle, an id.
    """
    d = _session_dir(tmp_path, "web:abc")
    meta = {
        "agent": "coder",
        "handle": "h1",
        "session_key": "web:abc",
        "task_summary": "Build the deck from the research",
    }

    SpawnRecord.open(d, task_id="t1", task="ask", meta=meta)

    assert instance_title(d, "coder", "h1") == "Build the deck from the research"


def test_an_instance_with_no_dispatch_is_named_by_its_opening_message(tmp_path: Path) -> None:
    """An instance the reader started themselves has nothing to be named after
    but the message that opened it, and every panel of one used to be headed by
    its handle -- an id. Named by the same rule a conversation with no title is
    named by: the first line, collapsed, capped."""
    from raven.agent.subagent.direct_chat import DirectChatRecord

    d = _session_dir(tmp_path, "web:abc")

    DirectChatRecord.open(
        d, agent="coder", handle="h1", task_id="t1", task="Help me configure serper\nsecond line must not count"
    )

    assert instance_title(d, "coder", "h1") == "Help me configure serper"


def test_the_instance_log_is_named_even_when_the_record_cannot_be_written(tmp_path: Path) -> None:
    """The two subtrees fail independently, so one must not take the other down.

    A file where `subagents/direct` wants a directory kills the record while the
    sibling instance log stays perfectly writable. Reading the identity back out
    of the meta the failed write was supposed to leave made the log's fate depend
    on the record's, and the instance stayed nameless for a reason that had
    nothing to do with it.
    """
    from raven.agent.subagent.direct_chat import DirectChatRecord

    d = _session_dir(tmp_path, "web:abc")
    (d / "subagents").mkdir(parents=True, exist_ok=True)
    (d / "subagents" / "direct").write_text("not a directory", encoding="utf-8")

    record = DirectChatRecord.open(d, agent="coder", handle="h1", task_id="t1", task="Check the weather for me")

    assert not record.dir.exists()
    assert instance_title(d, "coder", "h1") == "Check the weather for me"


def test_a_derivation_that_fails_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one failure in this path that nothing else can report.

    `_derived` runs before the try blocks in `open_instance_log_for` and
    `add_turn_to_instance_log`, so neither of their warnings observes a failure
    inside it -- the instance simply comes out headed by its handle, which is
    also exactly what an instance nobody named looks like. Swallowing is still
    right (a missing name may not fail a run); being silent about it was not.
    """
    import raven.session.manager as manager
    from raven.agent.subagent.direct_chat import DirectChatRecord

    def _fail(_value: str) -> str:
        raise RuntimeError("no rule to derive by")

    monkeypatch.setattr(manager, "derive_title", _fail)
    d = _session_dir(tmp_path, "web:abc")

    from loguru import logger

    said: list[str] = []
    sink_id = logger.add(lambda msg: said.append(str(msg)), level="WARNING", format="{message}")
    try:
        DirectChatRecord.open(d, agent="coder", handle="h1", task_id="t1", task="Help me configure serper")
    finally:
        logger.remove(sink_id)

    assert instance_title(d, "coder", "h1") == ""
    assert any("no rule to derive by" in line for line in said)


def test_a_later_message_does_not_rename_the_instance(tmp_path: Path) -> None:
    """The header is written once, so the first message names the instance --
    the rule `save` follows for a session, and the reason a name derived this
    way is stable enough to head a panel."""
    from raven.agent.subagent.direct_chat import DirectChatRecord

    d = _session_dir(tmp_path, "web:abc")
    first = DirectChatRecord.open(d, agent="coder", handle="h1", task_id="t1", task="the first ask")
    first.finish(status="completed", output="done")

    DirectChatRecord.open(d, agent="coder", handle="h1", task_id="t2", task="the second ask")

    assert instance_title(d, "coder", "h1") == "the first ask"


def test_a_dispatched_summary_still_wins_over_the_message(tmp_path: Path) -> None:
    """Deriving is the fallback, not the rule: a dispatch that said what the
    instance is for is a better name than the prompt's first line."""
    d = _session_dir(tmp_path, "web:abc")
    meta = {"agent": "coder", "handle": "h1", "session_key": "web:abc", "task_summary": "Draft a deck"}

    SpawnRecord.open(
        d, task_id="t1", task="Read these materials, research first, then hand me a deck I can present as-is", meta=meta
    )

    assert instance_title(d, "coder", "h1") == "Draft a deck"


def test_a_finished_spawn_is_still_named_once(tmp_path: Path) -> None:
    """Opening now names the instance, so finishing must not name it again."""
    d = _session_dir(tmp_path, "web:abc")
    meta = {"agent": "coder", "handle": "h1", "session_key": "web:abc", "task_summary": "Draft a deck"}
    record = SpawnRecord.open(d, task_id="t1", task="ask", meta=meta)

    record.finish(status="completed", output="done")

    written = [
        json.loads(line)
        for line in (d / "subagents" / "instances" / "coder" / "h1.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row.get("_type") for row in written].count("metadata") == 1
    assert instance_title(d, "coder", "h1") == "Draft a deck"
    assert [row["role"] for row in written if row.get("_type") != "metadata"] == ["user", "assistant"]


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
    from raven.agent.subagent.history import make_call_id

    first = make_call_id("efd70c2c")
    second = make_call_id("a8f2a07a")

    assert first < second, f"{first} was minted first and must sort first"
    assert sorted([first, second], reverse=True) == [second, first]


def test_a_run_id_orders_against_a_call_id() -> None:
    """The panel sorts spawns and graph runs into one list, so the two id
    shapes have to be comparable -- a second-accurate stamp on either side
    reshuffles ties across both."""
    from raven.agent.subagent.dag_store import make_run_id
    from raven.agent.subagent.history import make_call_id

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


def test_spawn_record_keeps_the_whole_answer_when_the_reply_was_capped(tmp_path: Path) -> None:
    """out.md is the recovery artifact the spawn tool advertises.

    Written from the activity's full output rather than the value the caller was
    handed: the two used to be the same truncated string, so the directory
    offered as the way back to the complete result held a second copy of the
    loss.
    """
    record = SpawnRecord.open(_session_dir(tmp_path, "web:abc"), task_id="t1", task="ask", meta={"agent": "Coder"})
    whole = "x" * 400
    with activity.collecting() as did:
        delivered = asyncio.run(clamp_output(whole, 200, agent="Coder"))
        record.finish(status="completed", output=delivered, activity=did)

    assert (record.dir / "out.md").read_text(encoding="utf-8") == whole
    meta = json.loads((record.dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["output_truncated"] is True
    assert meta["output_chars_total"] == 400
    assert meta["output_chars_returned"] + meta["output_chars_discarded"] == 400
    assert meta["output_truncation_reason"] == "max_output_chars"


def test_spawn_record_writes_what_it_was_given_when_nothing_was_dropped(tmp_path: Path) -> None:
    """An activity with no truncation on it must not redirect the output row:
    the full-output field is "what the caller is not being handed", not "the
    output"."""
    record = SpawnRecord.open(_session_dir(tmp_path, "web:abc"), task_id="t1", task="ask", meta={})
    with activity.collecting() as did:
        record.finish(status="completed", output="short answer", activity=did)

    assert (record.dir / "out.md").read_text(encoding="utf-8") == "short answer"
    assert "output_truncated" not in json.loads((record.dir / "meta.json").read_text(encoding="utf-8"))


def _logged_answer(tmp_path: Path, output: str | None, run: Any) -> str | None:
    """What the instance log recorded as this turn's answer, or None if no row.

    ``build_turn`` omits the assistant row entirely for ``answer=None``, so
    "there is no answer" and "the answer is empty" are distinguishable here the
    same way they are on disk.
    """
    turns = add_turn_to_instance_log(
        _session_dir(tmp_path, "web:abc"),
        meta={"agent": "Coder", "handle": "h1"},
        prompt="ask",
        output=output,
        activity=run,
        kind="spawn",
    )
    return next((t["content"] for t in turns if t["role"] == "assistant"), None)


def test_the_closing_row_wins_over_the_joined_output_when_a_lane_reports_one() -> None:
    """Three states, and the middle one must not fall back.

    `None` is "this transport cannot tell what was said last", so the joined
    output stands in. `""` is "it ended on a step and said nothing after it",
    which is a different fact: falling back there would attribute the whole
    reply to a closing message the run never sent. Neither is exercised anywhere
    else, so the distinction was free to rot.
    """
    assert _closing_answer(output="every burst joined", closing=None) == "every burst joined"
    assert _closing_answer(output="every burst joined", closing="") is None
    assert _closing_answer(output="every burst joined", closing="the last thing said") == "the last thing said"


def _closing_answer(*, output: str, closing: str | None) -> str | None:
    """The precedence in ``add_turn_to_instance_log``, read off a real turn."""
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        with activity.collecting() as run:
            if closing is not None:
                activity.note_closing(closing)
            return _logged_answer(Path(home), output, run)


def test_a_truncation_notice_cannot_invent_a_closing_the_run_never_sent() -> None:
    """`append_closing` is a no-op when nothing set a closing, and has to stay one.

    The truncation notice appends to the closing row so a reader of the record
    finds the sentence saying the answer is partial in the place they go for the
    answer. But a cli run reports no closing at all, and if this appended
    anyway it would turn `closing is None` into a non-empty string -- handing
    the instance log a bare notice as that turn's entire answer, with the answer
    itself dropped. That is a worse loss than the one being reported.
    """
    with activity.collecting() as run:
        activity.append_closing("\n\n[raven] Output truncated: ...")
        assert run.closing is None, "nothing set a closing, so there is nothing to append to"

    with activity.collecting() as run:
        activity.note_closing("")
        activity.append_closing("\n\n[raven] Output truncated: ...")
        assert run.closing == "\n\n[raven] Output truncated: ...", "an empty closing is still a closing"
