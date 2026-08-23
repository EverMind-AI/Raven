"""``subagents.instance*`` handlers (raven/rpc/methods/instances.py).

Covers the four reads/actions the direct-chat surface makes, plus the two rules
that are easy to get wrong: which rows count as live, and that a session with no
live agent loop degrades to empty rather than raising at the client.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from raven.agent.subagent import instances as instances_mod
from raven.rpc.methods.instances import (
    instances_create,
    instances_forget,
    instances_history,
    instances_list,
)


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> instances_mod.InstanceRegistry:
    """Never the developer's real ``~/.raven/subagent_instances.json``."""
    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    monkeypatch.setattr(instances_mod, "_registry", reg)
    return reg


class _FakeManager:
    def __init__(self, live: set[tuple[str, str]] | None = None, stateful: set[str] | None = None) -> None:
        self._live = live or set()
        self._stateful = stateful or set()
        self.session_dirs: dict[str, Path] = {}

    def live_handles(self, session_key: str) -> set[tuple[str, str]]:
        return self._live

    def declared_stateful(self, agent: str | None) -> bool:
        return agent in self._stateful

    def _session_dir(self, session_key: str) -> Path:
        return self.session_dirs[session_key]


class _FakeHandoff:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def pending_count(self, session_key: str) -> int:
        return self._counts.get(session_key, 0)


class _FakeLoop:
    def __init__(self, manager: Any = None, handoff: Any = None) -> None:
        self.subagents = manager
        self._direct_handoff = handoff
        self.tools = None


def _factory(loop: Any) -> Any:
    return lambda: loop


async def test_instances_lists_only_this_session(_isolated_registry: Any) -> None:
    await _isolated_registry.upsert_spawn("s1", "Raven-Code", "a", "completed")
    await _isolated_registry.upsert_spawn("s2", "Raven-Code", "b", "completed")

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(_FakeManager())))

    assert [(r["agent"], r["handle"]) for r in out["instances"]] == [("Raven-Code", "a")]
    assert out["pending_handoff_count"] == 0


async def test_instances_reports_a_dead_running_row_as_interrupted(_isolated_registry: Any) -> None:
    """A killed gateway leaves 'running' behind: the registry is durable, the
    task index is not."""
    await _isolated_registry.upsert_spawn("s1", "Raven-Code", "a", "running")

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(_FakeManager())))

    assert out["instances"][0]["status"] == "interrupted"


async def test_instances_leaves_a_genuinely_live_row_alone(_isolated_registry: Any) -> None:
    """The negative half: without this, "always interrupted" would also pass."""
    await _isolated_registry.upsert_spawn("s1", "Raven-Code", "a", "running")
    manager = _FakeManager(live={("Raven-Code", "a")})

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(manager)))

    assert out["instances"][0]["status"] == "running"


async def test_instances_does_not_rewrite_the_registrys_own_record(_isolated_registry: Any) -> None:
    """Reconciliation returns a copy. A rewrite in place would be read back on
    the next call as if it had come from disk, making the state permanent."""
    await _isolated_registry.upsert_spawn("s1", "Raven-Code", "a", "running")

    await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(_FakeManager())))
    stored = _isolated_registry.list_instances("s1")

    assert stored[0]["status"] == "running"


async def test_instances_reports_the_pending_handoff_count(_isolated_registry: Any) -> None:
    await _isolated_registry.upsert_spawn("s1", "Raven-Code", "a", "completed")
    loop = _FakeLoop(_FakeManager(), handoff=_FakeHandoff({"s1": 3}))

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(loop))

    assert out["pending_handoff_count"] == 3


async def test_instances_without_a_live_loop_is_empty_not_an_error(_isolated_registry: Any) -> None:
    """No provider configured is a normal state, and the client is drawing a
    status band -- it must not be handed an exception for it."""
    await _isolated_registry.upsert_spawn("s1", "Raven-Code", "a", "running")

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=None)

    # The rows still list; only liveness is unknown, and an unknown one reads
    # interrupted rather than pretending to run.
    assert out["instances"][0]["status"] == "interrupted"
    assert out["pending_handoff_count"] == 0


async def test_instances_survives_a_factory_that_raises(_isolated_registry: Any) -> None:
    """``_agent_loop_factory`` re-raises the build error when the loop failed to
    construct. That must not turn the chip strip into an RPC error."""

    def _boom() -> Any:
        raise RuntimeError("no provider")

    await _isolated_registry.upsert_spawn("s1", "Raven-Code", "a", "completed")

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_boom)

    assert [r["handle"] for r in out["instances"]] == ["a"]


def _record(session_dir: Path, agent: str, handle: str, *, task_id: str, task: str, output: str | None) -> Any:
    from raven.agent.subagent.direct_chat import DirectChatRecord

    rec = DirectChatRecord.open(session_dir, agent=agent, handle=handle, task_id=task_id, task=task)
    rec.finish(status="completed" if output is not None else "failed", output=output)
    return rec


async def test_history_reads_the_record_directories_in_order(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "s1"
    first = _record(session_dir, "A", "h", task_id="t1", task="first", output="a1")
    second = _record(session_dir, "A", "h", task_id="t2", task="second", output="a2")
    # Same-second call ids sort arbitrarily by name, so the order has to come
    # from the recorded start. Force the ambiguity rather than hope for it.
    _stamp(first, 1000)
    _stamp(second, 2000)
    _as_a_pre_log_conversation(session_dir)

    turns = await _history(session_dir, "A", "h")

    assert [t["content"] for t in turns] == ["first", "a1", "second", "a2"]
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]
    # The record-only fields, which only this reading has: the log is keyed by
    # instance and cannot name one call's files.
    assert turns[0]["prompt_path"].endswith("prompt.md")
    assert turns[1]["out_path"].endswith("out.md")


def _as_a_pre_log_conversation(session_dir: Path) -> None:
    """Drop the instance log, leaving only the record directories.

    That is what a conversation recorded before the instance log existed looks
    like on disk, and it is the state the stitching path is still there for. The
    tests below are about *its* contract -- ordering by the recorded start rather
    than by directory name -- which the log path cannot be asked about: the log
    is appended as turns finish, so its order is chronological by construction
    and back-dating a meta.json cannot reorder it.
    """
    import shutil

    shutil.rmtree(session_dir / "subagents" / "instances", ignore_errors=True)


def _stamp(record: Any, started_at_ms: int) -> None:
    meta = json.loads((record.dir / "meta.json").read_text(encoding="utf-8"))
    meta["started_at_ms"] = started_at_ms
    meta["ended_at_ms"] = started_at_ms + 1
    (record.dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


async def test_history_orders_by_start_not_by_directory_name(tmp_path: Path) -> None:
    """The directory name and the recorded start can disagree, and the start wins.

    Ids mint monotonically, so the name follows creation order: the record made
    first sorts first however its suffix compares. Stamping them the other way
    round is what makes this discriminating -- sorted by name these come back
    "second", "first".
    """
    session_dir = tmp_path / "sessions" / "s1"
    later = _record(session_dir, "A", "h", task_id="zzz", task="second", output=None)
    earlier = _record(session_dir, "A", "h", task_id="aaa", task="first", output=None)
    _stamp(later, 2000)
    _stamp(earlier, 1000)
    assert later.dir.name < earlier.dir.name
    _as_a_pre_log_conversation(session_dir)

    turns = await _history(session_dir, "A", "h")

    assert [t["content"] for t in turns] == ["first", "second"]


async def test_history_of_a_turn_with_no_reply_yields_only_the_prompt(tmp_path: Path) -> None:
    """A failed or still-running turn wrote prompt.md and no out.md."""
    session_dir = tmp_path / "sessions" / "s1"
    _record(session_dir, "A", "h", task_id="t1", task="ask", output=None)

    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir
    out = await instances_history(
        {"session_key": "s1", "agent": "A", "handle": "h"},
        agent_loop_factory=_factory(_FakeLoop(manager)),
    )

    assert [t["role"] for t in out["turns"]] == ["user"]


async def test_history_of_an_unknown_instance_is_empty_not_an_error(tmp_path: Path) -> None:
    manager = _FakeManager()
    manager.session_dirs["s1"] = tmp_path / "nothing-here"

    out = await instances_history(
        {"session_key": "s1", "agent": "A", "handle": "h"},
        agent_loop_factory=_factory(_FakeLoop(manager)),
    )

    assert out == {"turns": []}


async def test_history_without_a_manager_is_empty(tmp_path: Path) -> None:
    assert await instances_history({"session_key": "s1", "agent": "A", "handle": "h"}) == {"turns": []}


async def test_forget_drops_one_row_and_reports_it(_isolated_registry: Any) -> None:
    await _isolated_registry.upsert_spawn("s1", "A", "h", "completed")

    assert await instances_forget({"session_key": "s1", "agent": "A", "handle": "h"}) == {"removed": True}
    assert await instances_forget({"session_key": "s1", "agent": "A", "handle": "h"}) == {"removed": False}


async def test_forget_keeps_the_record_directories(tmp_path: Path, _isolated_registry: Any) -> None:
    """They are the audit trail, and the handoff already named paths inside them."""
    session_dir = tmp_path / "sessions" / "s1"
    record = _record(session_dir, "A", "h", task_id="t1", task="ask", output="ok")
    await _isolated_registry.upsert_spawn("s1", "A", "h", "completed")

    await instances_forget({"session_key": "s1", "agent": "A", "handle": "h"})

    assert (record.dir / "prompt.md").is_file()


# ---------------------------------------------------------------------------
# History merges what the main agent already asked this instance
# ---------------------------------------------------------------------------


def _spawn(session_dir: Path, *, agent: str, handle: str, task: str, output: str, at_ms: int) -> None:
    from raven.agent.subagent_history import SpawnRecord

    rec = SpawnRecord.open(session_dir, task_id=f"t{at_ms}", task=task, meta={"agent": agent, "handle": handle})
    rec.finish(status="completed", output=output)
    meta = json.loads((rec.dir / "meta.json").read_text(encoding="utf-8"))
    meta.update(started_at_ms=at_ms, ended_at_ms=at_ms + 1)
    (rec.dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _dag_run(session_dir: Path, run_id: str, nodes: dict[str, dict[str, Any]]) -> None:
    from raven.agent.subagent_history import dag_root

    run = dag_root(session_dir) / run_id
    run.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {}
    for node_id, spec in nodes.items():
        (run / f"{node_id}.prompt.md").write_text(spec["prompt"], encoding="utf-8")
        (run / f"{node_id}.out.md").write_text(spec["out"], encoding="utf-8")
        manifest[node_id] = {
            "subagent": spec["agent"],
            "instance": spec.get("instance"),
            "started_at": spec["at_ms"],
            "ended_at": spec["at_ms"] + 1,
            "prompt_file": str(run / f"{node_id}.prompt.md"),
            "output_file": str(run / f"{node_id}.out.md"),
            "status": "completed",
        }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


async def _history(session_dir: Path, agent: str, handle: str) -> list[dict[str, Any]]:
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir
    out = await instances_history(
        {"session_key": "s1", "agent": agent, "handle": handle},
        agent_loop_factory=_factory(_FakeLoop(manager)),
    )
    return out["turns"]


async def test_history_includes_the_spawn_that_created_the_instance(tmp_path: Path) -> None:
    """An instance is nearly always spawned before anyone switches into it; a
    view that showed only direct turns would open empty on the exchange the
    user came to read."""
    session_dir = tmp_path / "sessions" / "s1"
    _spawn(session_dir, agent="Coder", handle="notes", task="summarise it", output="done", at_ms=1000)

    turns = await _history(session_dir, "Coder", "notes")

    assert [t["content"] for t in turns] == ["summarise it", "done"]


async def test_history_ignores_a_spawn_of_another_instance(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "s1"
    _spawn(session_dir, agent="Coder", handle="other", task="not mine", output="nope", at_ms=1000)

    assert await _history(session_dir, "Coder", "notes") == []


async def test_history_includes_a_dag_node_bound_to_this_handle(tmp_path: Path) -> None:
    """A node with no `instance` binds under its own id, which is what the
    registry row then carries -- so both spellings have to match."""
    session_dir = tmp_path / "sessions" / "s1"
    _dag_run(
        session_dir,
        "run-1",
        {
            "greet_coder": {"agent": "Coder", "prompt": "say hi", "out": "hi", "at_ms": 2000},
            "greet_writer": {"agent": "Writer", "prompt": "other", "out": "other", "at_ms": 2000},
        },
    )

    turns = await _history(session_dir, "Coder", "greet_coder")

    assert [t["content"] for t in turns] == ["say hi", "hi"]


async def test_history_matches_a_dag_node_by_its_declared_instance(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "s1"
    _dag_run(
        session_dir,
        "run-1",
        {"n1": {"agent": "Coder", "instance": "notes", "prompt": "ask", "out": "answer", "at_ms": 2000}},
    )

    assert [t["content"] for t in await _history(session_dir, "Coder", "notes")] == ["ask", "answer"]


async def test_history_orders_every_source_by_when_it_started(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "s1"
    _spawn(session_dir, agent="Coder", handle="notes", task="spawned", output="s-out", at_ms=1000)
    _dag_run(
        session_dir,
        "run-1",
        {"n1": {"agent": "Coder", "instance": "notes", "prompt": "dagged", "out": "d-out", "at_ms": 2000}},
    )
    rec = _record(session_dir, "Coder", "notes", task_id="t3", task="typed", output="t-out")
    _stamp(rec, 3000)
    _as_a_pre_log_conversation(session_dir)

    turns = await _history(session_dir, "Coder", "notes")

    assert [t["content"] for t in turns] == ["spawned", "s-out", "dagged", "d-out", "typed", "t-out"]


async def test_a_playbook_dispatched_node_is_not_reported_dead(_isolated_registry: Any) -> None:
    """Liveness comes from every graph tool, not only the registered one.

    A ``mode: dag`` playbook is dispatched by the engine's private graph tool, so
    asking the registered one alone found nothing in flight and every running
    node of that run was rewritten to ``interrupted`` -- the panel showed a node
    as failed while its elapsed time went on climbing, which is the shape of the
    report that found this.
    """
    await _isolated_registry.upsert_dag_node("s1", "run-7", "brief", "content-raven", "running")

    class _LoopWithBothTools(_FakeLoop):
        def active_dag_run_ids(self) -> set[str]:
            return {"run-7"}

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_LoopWithBothTools(_FakeManager())))

    assert out["instances"][0]["status"] == "running"


async def test_a_node_whose_run_really_ended_is_still_reported_interrupted(_isolated_registry: Any) -> None:
    """The negative half: "always live" would pass the test above on its own."""
    await _isolated_registry.upsert_dag_node("s1", "run-7", "brief", "content-raven", "running")

    class _LoopWithNothingLive(_FakeLoop):
        def active_dag_run_ids(self) -> set[str]:
            return set()

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_LoopWithNothingLive(_FakeManager())))

    assert out["instances"][0]["status"] == "interrupted"


async def test_a_loop_that_cannot_answer_liveness_degrades_to_not_live(_isolated_registry: Any) -> None:
    """The callable runs lazily -- only for a dag-node row that still reads
    running -- so a loop without the method would raise from a branch most
    callers never reach. Answering "nothing live" is the same thing a missing
    tool already meant.
    """
    await _isolated_registry.upsert_dag_node("s1", "run-7", "brief", "content-raven", "running")

    class _Exploding(_FakeLoop):
        def active_dag_run_ids(self) -> set[str]:
            raise RuntimeError("no tool")

    plain = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(_FakeManager())))
    boom = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_Exploding(_FakeManager())))

    assert plain["instances"][0]["status"] == "interrupted"
    assert boom["instances"][0]["status"] == "interrupted"


# ---------------------------------------------------------------------------
# The instance log is the preferred source: a direct chat shows the whole turn
# ---------------------------------------------------------------------------


async def test_history_shows_what_the_run_did_not_only_what_it_answered(tmp_path: Path) -> None:
    """The half of a turn the stitched reading could never show.

    A prompt and a final output are what a record directory holds; the thought,
    the tool call and the tool's answer are in the instance log, which is written
    turn by turn. A direct chat that showed only the two ends of a turn was the
    whole reason for reading this file instead.
    """
    from raven.agent.subagent.instance_log import append_turn

    session_dir = tmp_path / "sessions" / "s1"
    append_turn(
        session_dir,
        agent="Coder",
        handle="notes",
        session_key="web:abc",
        kind="spawn",
        prompt="read the file",
        messages=[
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "the file is small, one read will do",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.py"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ZORKMID-4417"},
        ],
        answer="ZORKMID-4417",
    )

    turns = await _history(session_dir, "Coder", "notes")

    assert [t["role"] for t in turns] == ["user", "assistant", "tool", "assistant"]
    assert turns[1]["reasoning_content"] == "the file is small, one read will do"
    assert turns[1]["tool_calls"] == [{"id": "c1", "name": "read_file", "arguments": '{"path": "a.py"}'}]
    assert turns[2]["tool_call_id"] == "c1"
    assert turns[2]["content"] == "ZORKMID-4417"
    assert turns[3]["content"] == "ZORKMID-4417"


async def test_instance_history_names_a_stored_call_in_ravens_vocabulary(tmp_path: Path) -> None:
    """The record keeps the transport's name; the wire keeps raven's.

    This is what lets three front ends -- the TUI verb table, webui's
    per-name renderer dispatch, and the served page -- go unchanged while the
    file underneath gains provenance.
    """
    from raven.agent.subagent.instance_log import append_turn

    session_dir = tmp_path / "sessions" / "s1"
    append_turn(
        session_dir,
        agent="Coder",
        handle="h1",
        session_key="web:abc",
        prompt="go",
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "Bash", "arguments": '{"command": "ls"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "a.py"},
        ],
        answer="done",
    )

    turns = await _history(session_dir, "Coder", "h1")

    call = next(t for t in turns if t.get("tool_calls"))
    assert call["tool_calls"][0]["name"] == "exec"
    assert call["tool_calls"][0]["arguments"] == '{"command": "ls"}'


async def test_history_reads_every_lane_from_one_file(tmp_path: Path) -> None:
    """One instance, three lanes, one ordered conversation -- with no stitching.

    The log is appended by whichever lane ran the turn, so what used to need
    three directory walks and a sort is now the file's own order.
    """
    from raven.agent.subagent.instance_log import append_turn

    session_dir = tmp_path / "sessions" / "s1"
    for lane, text in (("spawn", "spawned"), ("dag", "dagged"), ("direct", "typed")):
        append_turn(
            session_dir,
            agent="Coder",
            handle="notes",
            session_key="web:abc",
            kind=lane,
            prompt=text,
            answer=f"{text}-out",
        )

    turns = await _history(session_dir, "Coder", "notes")

    assert [t["content"] for t in turns] == ["spawned", "spawned-out", "dagged", "dagged-out", "typed", "typed-out"]


async def test_history_falls_back_when_an_instance_has_no_log(tmp_path: Path) -> None:
    """A conversation from before the log existed still opens on its records."""
    session_dir = tmp_path / "sessions" / "s1"
    _spawn(session_dir, agent="Coder", handle="notes", task="asked", output="answered", at_ms=1000)
    _as_a_pre_log_conversation(session_dir)

    turns = await _history(session_dir, "Coder", "notes")

    assert [t["content"] for t in turns] == ["asked", "answered"]


async def test_history_skips_the_logs_header_and_a_corrupt_line(tmp_path: Path) -> None:
    """The header is the file's one tagged record, and one bad line must not
    empty a conversation -- the same tolerance the session loader has."""
    from raven.agent.subagent.instance_log import append_turn, transcript_path

    session_dir = tmp_path / "sessions" / "s1"
    append_turn(session_dir, agent="Coder", handle="notes", session_key="web:abc", prompt="asked", answer="answered")
    path = transcript_path(session_dir, "Coder", "notes")
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")

    turns = await _history(session_dir, "Coder", "notes")

    assert [t["content"] for t in turns] == ["asked", "answered"]


async def test_history_carries_the_steps_of_the_turn_running_now(tmp_path: Path) -> None:
    """A turn in flight has no record, so its only account is the live activity.

    This is what lets a direct chat show the work as it happens: the event
    stream tags an instance on the reply text alone, so nothing else can bring
    the steps to the view before the turn ends.
    """
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h")) as did:
        activity.note_transcript(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "output"},
            ]
        )
        assert did.transcript
        out = await instances_history(
            {"session_key": "s1", "agent": "A", "handle": "h"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )

    assert [t["role"] for t in out["turns"]] == ["assistant", "tool"]
    assert all(t["live"] is True for t in out["turns"]), "the client has to tell a snapshot from the record"
    assert out["turns"][0]["tool_calls"][0]["name"] == "exec"


async def test_the_live_read_carries_the_answer_so_far(tmp_path: Path) -> None:
    """For two of the three lanes this read is the only thing that carries it.

    The wire tags an instance on the four events of a *direct* turn, so a spawn
    or a DAG node reaches the conversation view through nothing else. Withholding
    the text here -- on the grounds that ``token.delta`` delivers it -- left a
    spawned turn showing its tool calls and never a word the agent said.
    """
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h")):
        activity.note_transcript(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "output"},
                {"role": "assistant", "content": "half an answ"},
            ]
        )
        out = await instances_history(
            {"session_key": "s1", "agent": "A", "handle": "h"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )

    assert [t["role"] for t in out["turns"]] == ["assistant", "tool", "assistant"]
    assert out["turns"][-1]["content"] == "half an answ"
    assert all(t["live"] is True for t in out["turns"])


async def test_a_trailing_thought_is_not_mistaken_for_the_streaming_reply(tmp_path: Path) -> None:
    """Both are assistant rows with no tool call; only one carries text."""
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h")):
        activity.note_transcript([{"role": "assistant", "content": "", "reasoning_content": "still deciding"}])
        out = await instances_history(
            {"session_key": "s1", "agent": "A", "handle": "h"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )

    assert [t["reasoning_content"] for t in out["turns"]] == ["still deciding"]


async def test_live_steps_follow_the_turns_already_on_record(tmp_path: Path) -> None:
    """An in-flight turn is the last one by definition, so it appends."""
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    _record(session_dir, "A", "h", task_id="t1", task="first", output="a1")
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h")):
        activity.note_transcript([{"role": "assistant", "content": "", "reasoning_content": "thinking"}])
        out = await instances_history(
            {"session_key": "s1", "agent": "A", "handle": "h"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )

    assert [t.get("live") for t in out["turns"]] == [None, None, True]
    assert [t["content"] for t in out["turns"][:2]] == ["first", "a1"]


async def test_a_conversation_with_nothing_in_flight_reads_the_log_only(tmp_path: Path) -> None:
    """The live index is empty once the block closes, and the record stands alone."""
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    _record(session_dir, "A", "h", task_id="t1", task="first", output="a1")
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h")):
        activity.note_transcript([{"role": "assistant", "content": "", "reasoning_content": "thinking"}])

    assert activity.live_instance("s1", "A", "h") is None
    out = await instances_history(
        {"session_key": "s1", "agent": "A", "handle": "h"},
        agent_loop_factory=_factory(_FakeLoop(manager)),
    )

    assert [t.get("live") for t in out["turns"]] == [None, None]


async def test_one_instances_live_steps_never_reach_another(tmp_path: Path) -> None:
    """The index is keyed by the whole address, not by the handle."""
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h")):
        activity.note_transcript([{"role": "assistant", "content": "", "reasoning_content": "mine"}])
        other = await instances_history(
            {"session_key": "s1", "agent": "B", "handle": "h"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )
        same_agent_other_handle = await instances_history(
            {"session_key": "s1", "agent": "A", "handle": "h2"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )

    assert other["turns"] == []
    assert same_agent_other_handle["turns"] == []


async def test_the_live_read_carries_the_question_the_turn_was_asked(tmp_path: Path) -> None:
    """A client rebuilds the whole in-flight turn from this read.

    Without the prompt a spawn or a DAG node -- whose task the client never
    typed and so has no row of its own for -- would show steps with no question
    above them.
    """
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h"), prompt="refactor the parser"):
        activity.note_transcript([{"role": "assistant", "content": "", "reasoning_content": "reading it"}])
        out = await instances_history(
            {"session_key": "s1", "agent": "A", "handle": "h"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )

    assert [(t["role"], t["content"]) for t in out["turns"]] == [
        ("user", "refactor the parser"),
        ("assistant", ""),
    ]
    assert all(t["live"] is True for t in out["turns"])


async def test_a_turn_that_reported_no_prompt_still_reads(tmp_path: Path) -> None:
    """Absent is not empty: a lane that cannot say leaves the row out."""
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h")):
        activity.note_transcript([{"role": "assistant", "content": "", "reasoning_content": "reading it"}])
        out = await instances_history(
            {"session_key": "s1", "agent": "A", "handle": "h"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )

    assert [t["role"] for t in out["turns"]] == ["assistant"]


async def test_the_running_turns_prompt_is_not_reported_twice(tmp_path: Path) -> None:
    """The record's prompt file exists from the moment the turn opens.

    So the fallback reading yields it as a settled row while the live rows carry
    it too, and the view showed the question twice -- reported from the TUI on a
    spawned turn, whose handle had no instance log yet and therefore took that
    fallback.
    """
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    # A record that has opened and not finished: prompt on disk, no output.
    _record(session_dir, "A", "h", task_id="t1", task="do the thing", output=None)
    _as_a_pre_log_conversation(session_dir)
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h"), prompt="do the thing"):
        activity.note_transcript([{"role": "assistant", "content": "", "reasoning_content": "on it"}])
        out = await instances_history(
            {"session_key": "s1", "agent": "A", "handle": "h"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )

    assert [(t["role"], t.get("live")) for t in out["turns"]] == [("user", True), ("assistant", True)]
    assert sum(1 for t in out["turns"] if t["content"] == "do the thing") == 1


async def test_a_finished_turn_keeps_its_prompt_on_record(tmp_path: Path) -> None:
    """Paired with the case above: the drop is for the *running* turn only."""
    session_dir = tmp_path / "sessions" / "s1"
    _record(session_dir, "A", "h", task_id="t1", task="do the thing", output="did it")
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    out = await instances_history(
        {"session_key": "s1", "agent": "A", "handle": "h"},
        agent_loop_factory=_factory(_FakeLoop(manager)),
    )

    assert [t["content"] for t in out["turns"]] == ["do the thing", "did it"]


async def test_an_earlier_crashed_prompt_survives_a_new_turn(tmp_path: Path) -> None:
    """Only the running turn's own prompt is dropped, not every unanswered one.

    A prompt with no reply that is *not* the last one is a run that crashed. It
    is still worth showing, and it is not the turn this read is correcting for.
    """
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    crashed = _record(session_dir, "A", "h", task_id="t1", task="the one that died", output=None)
    running = _record(session_dir, "A", "h", task_id="t2", task="the one running now", output=None)
    _stamp(crashed, 1000)
    _stamp(running, 2000)
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h"), prompt="the one running now"):
        activity.note_transcript([{"role": "assistant", "content": "working"}])
        out = await instances_history(
            {"session_key": "s1", "agent": "A", "handle": "h"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )

    assert [(t.get("live"), t["content"]) for t in out["turns"]] == [
        (None, "the one that died"),
        (True, "the one running now"),
        (True, "working"),
    ]


def _real_manager(tmp_path: Path) -> Any:
    """A real ``SubagentManager``: ``create_instance``'s whole job is the two
    roster checks it makes, and a double would assert against its own answer."""
    from raven.agent.subagent.manager import SubagentManager

    class _StubProvider:
        def get_default_model(self) -> str:
            return "stub"

    return SubagentManager(
        provider=_StubProvider(),
        workspace=tmp_path / "home",
        session_dir=lambda key: tmp_path / "sessions" / key,
    )


def _create_loop(tmp_path: Path) -> Any:
    from raven.agent.subagent.direct_chat import DirectChatHandoff

    return _FakeLoop(_real_manager(tmp_path), DirectChatHandoff())


async def test_instance_create_returns_the_row_the_strip_will_draw(tmp_path: Path) -> None:
    loop = _create_loop(tmp_path)

    out = await instances_create({"session_key": "s1", "agent": "raven"}, agent_loop_factory=_factory(loop))

    row = out["instance"]
    assert row["agent"] == "raven"
    assert row["status"] == "idle"
    assert row["sessionKey"] == "s1"
    assert row["handle"].startswith("raven-")


async def test_a_created_instance_is_listed_for_that_session(tmp_path: Path) -> None:
    """The row has to survive the read path too: an idle row must not be
    reconciled to 'interrupted' the way a dead 'running' one is."""
    loop = _create_loop(tmp_path)

    created = await instances_create({"session_key": "s1", "agent": "raven"}, agent_loop_factory=_factory(loop))
    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(loop))

    assert [(r["handle"], r["status"]) for r in out["instances"]] == [(created["instance"]["handle"], "idle")]


async def test_a_creation_is_announced_to_the_main_agent(tmp_path: Path) -> None:
    """The user creating an instance is what the main agent has to be told; a
    turn it could already see is not the interesting case."""
    loop = _create_loop(tmp_path)

    created = await instances_create({"session_key": "s1", "agent": "raven"}, agent_loop_factory=_factory(loop))
    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(loop))

    assert out["pending_handoff_count"] == 1

    block = loop._direct_handoff.take("s1")
    assert f"raven / {created['instance']['handle']}" in block
    assert "created by the user at" in block


async def test_a_creation_is_announced_only_to_its_own_session(tmp_path: Path) -> None:
    loop = _create_loop(tmp_path)

    await instances_create({"session_key": "s1", "agent": "raven"}, agent_loop_factory=_factory(loop))
    out = await instances_list({"session_key": "s2"}, agent_loop_factory=_factory(loop))

    assert out["pending_handoff_count"] == 0


async def test_instance_create_refuses_an_agent_that_is_not_on_the_roster(tmp_path: Path) -> None:
    loop = _create_loop(tmp_path)

    with pytest.raises(RuntimeError, match="disabled or no longer configured"):
        await instances_create({"session_key": "s1", "agent": "nope"}, agent_loop_factory=_factory(loop))

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(loop))
    assert out["instances"] == []
    assert out["pending_handoff_count"] == 0


async def test_instance_create_without_a_live_loop_fails_rather_than_reporting_success() -> None:
    """Unlike the reads in this module, a create is an action: an empty answer
    would leave the picker announcing an instance that does not exist."""
    with pytest.raises(RuntimeError, match="no agent loop"):
        await instances_create({"session_key": "s1", "agent": "raven"}, agent_loop_factory=None)


# ── one row per invocation ───────────────────────────────────────────────────
# A stateful DAG node owns two records: its own status row, and the handle its
# sub-agent committed under. Reported separately, four fan-out nodes read back
# as eight instances, each node once as a handle nothing can resume.


async def test_a_stateful_node_is_reported_once_on_its_addressable_handle(_isolated_registry: Any) -> None:
    await _isolated_registry.upsert_dag_node("s1", "r1", "research_a2a", "hermes", "completed")
    await _isolated_registry.link_dag_node("s1", "hermes", "research-a2a-726da8", "r1", "research_a2a")

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(_FakeManager())))

    assert [r["handle"] for r in out["instances"]] == ["research-a2a-726da8"]
    row = out["instances"][0]
    # The node's status, which only the dag-node row carries: the DAG never goes
    # through `spawn`, so nothing ever writes a status on the ordinary row.
    assert row["status"] == "completed"
    assert (row["runId"], row["nodeId"]) == ("r1", "research_a2a")


async def test_a_whole_fan_out_is_one_row_per_node(_isolated_registry: Any) -> None:
    nodes = ["research_a2a", "research_mcp", "research_acp", "synthesize"]
    for node in nodes:
        await _isolated_registry.upsert_dag_node("s1", "r1", node, "hermes", "completed")
        await _isolated_registry.link_dag_node("s1", "hermes", f"{node}-h", "r1", node)

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(_FakeManager())))

    assert len(out["instances"]) == len(nodes)
    assert {r["nodeId"] for r in out["instances"]} == set(nodes)


async def test_a_stateless_node_keeps_the_only_row_it_has(_isolated_registry: Any) -> None:
    """It runs on no handle, so there is no ordinary row to fall back to --
    filtering ``dag-node`` wholesale would take the node off the list."""
    await _isolated_registry.upsert_dag_node("s1", "r1", "shape", "stateless-agent", "completed")

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(_FakeManager())))

    assert [(r["kind"], r["handle"]) for r in out["instances"]] == [("dag-node", "r1/shape")]


async def test_a_node_that_committed_under_its_own_id_is_paired_too(_isolated_registry: Any) -> None:
    """Rows written before the link existed carry no ``nodeId``. A node that
    declared no handle commits under its task id, which is the node id, so that
    equality still pairs them -- otherwise every session that already ran keeps
    reporting its graph twice for good."""
    await _isolated_registry.upsert_dag_node("s1", "r1", "synthesize", "claude_code", "completed")
    await _isolated_registry.commit("s1", "claude_code", "synthesize", "sess-a")

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(_FakeManager())))

    assert [(r["handle"], r["status"], r["nodeId"]) for r in out["instances"]] == [
        ("synthesize", "completed", "synthesize")
    ]


async def test_two_runs_of_one_node_name_are_left_alone(_isolated_registry: Any) -> None:
    """The name pairing is a fallback and must not guess: with two runs holding a
    node of the same name, a handle cannot say which of them it ran."""
    await _isolated_registry.upsert_dag_node("s1", "r1", "synthesize", "claude_code", "completed")
    await _isolated_registry.upsert_dag_node("s1", "r2", "synthesize", "claude_code", "failed")
    await _isolated_registry.commit("s1", "claude_code", "synthesize", "sess-a")

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(_FakeManager())))

    assert sorted(r["handle"] for r in out["instances"]) == ["r1/synthesize", "r2/synthesize", "synthesize"]


async def test_a_spawn_that_belongs_to_no_graph_is_untouched(_isolated_registry: Any) -> None:
    await _isolated_registry.upsert_spawn("s1", "hermes", "lone", "completed")

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(_FakeManager())))

    assert [r["handle"] for r in out["instances"]] == ["lone"]
    assert out["instances"][0].get("runId") is None


async def test_the_node_a_handle_belongs_to_survives_a_later_status_write(_isolated_registry: Any) -> None:
    """Every writer rebuilds its record whole, and the link is the only thing
    that can pair a minted handle back to its node."""
    await _isolated_registry.link_dag_node("s1", "hermes", "h", "r1", "shape")
    await _isolated_registry.commit("s1", "hermes", "h", "cli-session-9")
    await _isolated_registry.upsert_spawn("s1", "hermes", "h", "running")

    rows = _isolated_registry.list_instances("s1")

    assert [(r["runId"], r["nodeId"], r["agentId"]) for r in rows] == [("r1", "shape", "cli-session-9")]


async def test_a_link_written_before_the_binding_does_not_retype_the_row(_isolated_registry: Any) -> None:
    """The link lands when the node dispatches, which is before an acp backend
    has a session id to commit -- stamping ``cli`` there would make ``lookup``
    refuse the acp id that arrives afterwards."""
    await _isolated_registry.link_dag_node("s1", "raven-acp", "h", "r1", "shape")
    await _isolated_registry.commit("s1", "raven-acp", "h", "acp-session-1", kind="acp")

    assert await _isolated_registry.lookup("s1", "raven-acp", "h", kind="acp") == "acp-session-1"


# ── which rows can be talked to ──────────────────────────────────────────────


async def test_instances_say_which_rows_can_be_talked_to(_isolated_registry: Any) -> None:
    """The predicate is the manager's, not the row's: statefulness belongs to the
    agent's configured backend, and every front end guessing at it separately is
    how one of them calls an acp instance unresumable."""
    await _isolated_registry.upsert_spawn("s1", "hermes", "chatty", "completed")
    await _isolated_registry.upsert_spawn("s1", "one-shot", "quiet", "completed")
    await _isolated_registry.upsert_dag_node("s1", "r1", "shape", "hermes", "completed")
    manager = _FakeManager(stateful={"hermes"})

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(manager)))

    assert {r["handle"]: r["resumable"] for r in out["instances"]} == {
        "chatty": True,
        "quiet": False,
        # A handle that names a node is not a conversation, whatever its agent can do.
        "r1/shape": False,
    }


async def test_a_manager_that_cannot_answer_statefulness_reports_not_resumable(_isolated_registry: Any) -> None:
    class _Broken(_FakeManager):
        def declared_stateful(self, agent: str | None) -> bool:
            raise RuntimeError("roster unreadable")

    await _isolated_registry.upsert_spawn("s1", "hermes", "h", "completed")

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=_factory(_FakeLoop(_Broken())))

    assert [r["resumable"] for r in out["instances"]] == [False]


# ── forgetting a row that is really two records ──────────────────────────────
#
# The list reports a stateful DAG node as one row, so it has to be forgettable as
# one. Dropping only the addressable handle left the node's status record behind,
# and the next list reported *that* on its own -- the row the reader dismissed
# came back, as something nothing can be said to.


async def test_forgetting_a_collapsed_dag_row_does_not_bring_it_back(_isolated_registry: Any) -> None:
    await _isolated_registry.upsert_dag_node("s1", "r1", "research_a2a", "hermes", "completed")
    await _isolated_registry.link_dag_node("s1", "hermes", "research-a2a-726da8", "r1", "research_a2a")
    factory = _factory(_FakeLoop(_FakeManager()))

    before = await instances_list({"session_key": "s1"}, agent_loop_factory=factory)
    assert [r["handle"] for r in before["instances"]] == ["research-a2a-726da8"]

    forgotten = await instances_forget({"session_key": "s1", "agent": "hermes", "handle": "research-a2a-726da8"})

    assert forgotten == {"removed": True}
    after = await instances_list({"session_key": "s1"}, agent_loop_factory=factory)
    assert after["instances"] == []


async def test_forgetting_a_name_paired_row_takes_the_node_record_too(_isolated_registry: Any) -> None:
    """Paired by name rather than by link, which is every session that ran before
    the link existed -- and exactly the case reading ``runId`` off the named
    record would miss, since that record has none."""
    await _isolated_registry.upsert_dag_node("s1", "r1", "synthesize", "claude_code", "completed")
    await _isolated_registry.commit("s1", "claude_code", "synthesize", "sess-a")
    factory = _factory(_FakeLoop(_FakeManager()))

    await instances_forget({"session_key": "s1", "agent": "claude_code", "handle": "synthesize"})

    out = await instances_list({"session_key": "s1"}, agent_loop_factory=factory)
    assert out["instances"] == []


async def test_forgetting_a_stateless_nodes_own_row_removes_it(_isolated_registry: Any) -> None:
    """Its handle already *is* ``<run>/<node>``; there is no second record to
    chase, and chasing one would delete the row twice."""
    await _isolated_registry.upsert_dag_node("s1", "r1", "shape", "oneshot", "completed")

    forgotten = await instances_forget({"session_key": "s1", "agent": "oneshot", "handle": "r1/shape"})

    assert forgotten == {"removed": True}
    assert _isolated_registry.list_instances("s1") == []


async def test_forgetting_an_unpaired_handle_leaves_both_runs_nodes_alone(_isolated_registry: Any) -> None:
    """Two runs hold a node of that name, so the handle says nothing about which
    of them it ran -- and a row the list refuses to pair must not be forgotten as
    a pair either, or dismissing it silently takes a node of the other run."""
    await _isolated_registry.upsert_dag_node("s1", "r1", "synthesize", "claude_code", "completed")
    await _isolated_registry.upsert_dag_node("s1", "r2", "synthesize", "claude_code", "failed")
    await _isolated_registry.commit("s1", "claude_code", "synthesize", "sess-a")

    await instances_forget({"session_key": "s1", "agent": "claude_code", "handle": "synthesize"})

    assert sorted(r["handle"] for r in _isolated_registry.list_instances("s1")) == [
        "r1/synthesize",
        "r2/synthesize",
    ]


async def test_forgetting_a_lone_spawn_is_unchanged(_isolated_registry: Any) -> None:
    """No graph, nothing to pair, and the answer is still the plain one."""
    await _isolated_registry.upsert_spawn("s1", "hermes", "lone", "completed")

    assert await instances_forget({"session_key": "s1", "agent": "hermes", "handle": "lone"}) == {"removed": True}
    assert _isolated_registry.list_instances("s1") == []


async def test_the_question_of_a_running_turn_is_dated(tmp_path: Path) -> None:
    """A reader watching a turn sees the question before the turn lands, and it
    has to carry a clock: unstamped, ``_ms_of`` reports 0 -- a valid instant --
    and the page dated a question just asked to 1970-01-01. The run's own start
    is the one instant that is true and does not move between polls."""
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h"), prompt="在吗") as run:
        out = await instances_history(
            {"session_key": "s1", "agent": "A", "handle": "h"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )

    assert [t["content"] for t in out["turns"]] == ["在吗"]
    assert out["turns"][0]["at_ms"] == run.started_at_ms


async def test_a_running_turns_steps_are_left_unstamped(tmp_path: Path) -> None:
    """The start is the turn's, not each step's. A transport publishes its
    transcript with no clock on the rows, and inventing one per row would move
    on every poll -- so they carry none and the client draws none."""
    from raven.agent.subagent import activity

    session_dir = tmp_path / "sessions" / "s1"
    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir

    with activity.collecting(instance=("s1", "A", "h"), prompt="在吗"):
        activity.note_transcript([{"role": "assistant", "content": "", "reasoning_content": "想一下"}])
        out = await instances_history(
            {"session_key": "s1", "agent": "A", "handle": "h"},
            agent_loop_factory=_factory(_FakeLoop(manager)),
        )

    assert [t["at_ms"] > 0 for t in out["turns"]] == [True, False]


async def test_a_log_rows_clock_survives_the_read(tmp_path: Path) -> None:
    """A row's timestamp comes back as the millisecond it was written with.

    Truncating instead of rounding loses it: a fractional second has no exact
    binary form, so ``1.001 * 1000`` is ``1000.9999...`` and ``int()`` takes the
    tick below. Every turn read from a log went through that, and a reader
    sorting or keying on ``at_ms`` was one millisecond out.
    """
    from raven.agent.subagent.instance_log import append_turn

    session_dir = tmp_path / "sessions" / "s1"
    stamped = datetime.fromtimestamp(1.001).isoformat()
    append_turn(
        session_dir,
        agent="A",
        handle="h",
        session_key="s1",
        messages=[{"role": "user", "content": "asked", "timestamp": stamped}],
    )

    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir
    answered = await instances_history(
        {"session_key": "s1", "agent": "A", "handle": "h"},
        agent_loop_factory=_factory(_FakeLoop(manager)),
    )

    assert [t["at_ms"] for t in answered["turns"]] == [1001]
