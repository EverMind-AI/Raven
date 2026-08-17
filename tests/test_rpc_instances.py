"""``subagents.instance*`` handlers (raven/rpc/methods/instances.py).

Covers the four reads/actions the direct-chat surface makes, plus the two rules
that are easy to get wrong: which rows count as live, and that a session with no
live agent loop degrades to empty rather than raising at the client.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from raven.agent.subagent import instances as instances_mod
from raven.rpc.methods.instances import instances_forget, instances_history, instances_list


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> instances_mod.InstanceRegistry:
    """Never the developer's real ``~/.raven/subagent_instances.json``."""
    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    monkeypatch.setattr(instances_mod, "_registry", reg)
    return reg


class _FakeManager:
    def __init__(self, live: set[tuple[str, str]] | None = None) -> None:
        self._live = live or set()
        self.session_dirs: dict[str, Path] = {}

    def live_handles(self, session_key: str) -> set[tuple[str, str]]:
        return self._live

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

    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir
    out = await instances_history(
        {"session_key": "s1", "agent": "A", "handle": "h"},
        agent_loop_factory=_factory(_FakeLoop(manager)),
    )

    assert [t["content"] for t in out["turns"]] == ["first", "a1", "second", "a2"]
    assert [t["role"] for t in out["turns"]] == ["user", "assistant", "user", "assistant"]
    assert out["turns"][0]["prompt_path"].endswith("prompt.md")
    assert out["turns"][1]["out_path"].endswith("out.md")


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

    manager = _FakeManager()
    manager.session_dirs["s1"] = session_dir
    out = await instances_history(
        {"session_key": "s1", "agent": "A", "handle": "h"},
        agent_loop_factory=_factory(_FakeLoop(manager)),
    )

    assert [t["content"] for t in out["turns"]] == ["first", "second"]


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

    turns = await _history(session_dir, "Coder", "notes")

    assert [t["content"] for t in turns] == ["spawned", "s-out", "dagged", "d-out", "typed", "t-out"]
