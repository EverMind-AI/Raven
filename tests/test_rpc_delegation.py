"""Tests for ``delegation.status`` / ``delegation.pause`` / ``subagent.interrupt``.

These three back the TUI's spawn HUD and the agents overlay's pause and kill
controls. ``delegation.status`` in particular is fetched on every
``subagent.spawn_requested`` event, so a rejection here is not a corner case --
it lands in the chat transcript once per spawn.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from raven.rpc.errors import InternalError
from raven.rpc.methods.delegation import (
    MAX_SPAWN_DEPTH,
    delegation_pause,
    delegation_status,
    subagent_cancel_instance,
    subagent_cancel_session,
    subagent_interrupt,
)


class _FakeManager:
    def __init__(self, max_concurrent: int = 4) -> None:
        self.max_concurrent = max_concurrent
        self._paused = False
        self.cancelled: list[str] = []
        self.live: set[str] = set()

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> bool:
        self._paused = bool(paused)
        return self._paused

    async def cancel_by_id(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        return task_id in self.live

    async def cancel_by_session(self, session_key: str) -> int:
        self.cancelled.append(f"session:{session_key}")
        return 2 if session_key in self.live else 0

    async def cancel_by_instance(self, session_key: str, agent: str, handle: str) -> bool:
        key = f"{session_key}:{agent}:{handle}"
        self.cancelled.append(key)
        return key in self.live


def _factory(manager: object | None):
    loop = type("_Loop", (), {"subagents": manager})()
    return lambda: loop


async def test_status_reports_the_live_cap_not_a_config_default() -> None:
    manager = _FakeManager(max_concurrent=9)
    result = await delegation_status({}, agent_loop_factory=_factory(manager))
    assert result == {
        "max_concurrent_children": 9,
        "max_spawn_depth": MAX_SPAWN_DEPTH,
        "paused": False,
    }


async def test_status_reflects_a_pause_set_through_the_other_method() -> None:
    manager = _FakeManager()
    factory = _factory(manager)
    await delegation_pause({"paused": True}, agent_loop_factory=factory)
    assert (await delegation_status({}, agent_loop_factory=factory))["paused"] is True


async def test_pause_echoes_the_value_that_took_effect() -> None:
    manager = _FakeManager()
    factory = _factory(manager)
    assert await delegation_pause({"paused": True}, agent_loop_factory=factory) == {"paused": True}
    assert await delegation_pause({"paused": False}, agent_loop_factory=factory) == {"paused": False}


async def test_pause_treats_a_missing_flag_as_resume() -> None:
    manager = _FakeManager()
    manager.set_paused(True)
    assert await delegation_pause({}, agent_loop_factory=_factory(manager)) == {"paused": False}


async def test_interrupt_reports_found_for_a_live_spawn() -> None:
    manager = _FakeManager()
    manager.live.add("abc123")
    result = await subagent_interrupt({"subagent_id": "abc123"}, agent_loop_factory=_factory(manager))
    assert result == {"found": True, "subagent_id": "abc123"}
    assert manager.cancelled == ["abc123"]


async def test_interrupt_of_an_already_finished_spawn_is_not_an_error() -> None:
    manager = _FakeManager()
    result = await subagent_interrupt({"subagent_id": "gone"}, agent_loop_factory=_factory(manager))
    assert result == {"found": False, "subagent_id": "gone"}


async def test_interrupt_without_an_id_never_reaches_the_manager() -> None:
    manager = _FakeManager()
    result = await subagent_interrupt({}, agent_loop_factory=_factory(manager))
    assert result == {"found": False, "subagent_id": ""}
    assert manager.cancelled == []


async def test_no_live_loop_is_a_typed_error_not_a_traceback() -> None:
    with pytest.raises(InternalError):
        await delegation_status({}, agent_loop_factory=None)
    with pytest.raises(InternalError):
        await delegation_status({}, agent_loop_factory=_factory(None))


# ---------------------------------------------------------------------------
# The manager side of the pause flag
# ---------------------------------------------------------------------------


def _real_manager():
    from raven.agent.subagent.manager import SubagentManager

    provider = type("_P", (), {"get_default_model": lambda self: "m"})()
    return SubagentManager(provider=provider, workspace=Path("."), max_concurrent=3)


def test_manager_exposes_the_concurrency_cap_it_was_built_with() -> None:
    assert _real_manager().max_concurrent == 3


async def test_a_paused_manager_refuses_to_spawn() -> None:
    manager = _real_manager()
    manager.set_paused(True)
    reply = await manager.spawn("do a thing", session_key="s1")
    assert "paused" in reply.lower()
    assert manager.get_running_count() == 0


async def test_resuming_lets_spawns_through_again() -> None:
    manager = _real_manager()
    manager.set_paused(True)
    await manager.spawn("first", session_key="s1")
    manager.set_paused(False)
    reply = await manager.spawn("second", session_key="s1")
    assert "started" in reply
    await manager.cancel_all()


async def test_cancel_by_id_reports_whether_the_task_was_live() -> None:
    manager = _real_manager()
    assert await manager.cancel_by_id("no-such-id") is False

    started = asyncio.Event()

    async def _never_ends() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(_never_ends())
    manager._running_tasks["tid"] = task
    await started.wait()
    assert await manager.cancel_by_id("tid") is True
    assert task.cancelled()


async def test_pausing_also_stops_a_dag_run_not_just_a_single_spawn() -> None:
    # The DAG tool dispatches to its own backends and never calls spawn(), so a
    # pause that only guarded spawn() would stop single spawns while a graph
    # kept fanning out behind a HUD reading "paused".
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    manager = _real_manager()
    tool = SubAgentDagTool(workspace=Path("."), is_paused=lambda: manager.paused)

    manager.set_paused(True)
    refused = await tool.execute(nodes=[{"id": "a", "agent": "x", "prompt": "hi"}])
    assert "paused" in refused.lower()

    # And resuming lets a graph past the pause gate again: whatever it fails on
    # next, it is no longer the pause.
    manager.set_paused(False)
    after = await tool.execute(nodes=[{"id": "a", "agent": "x", "prompt": "hi"}])
    assert "delegation is paused" not in after


async def test_cancel_session_sweeps_and_reports_the_count() -> None:
    """[N4-F3] The terminal dialect gains the sweep IM /stop always had."""
    manager = _FakeManager()
    manager.live.add("web:s1")
    result = await subagent_cancel_session({"session_key": "web:s1"}, agent_loop_factory=_factory(manager))
    assert result == {"cancelled": 2, "session_key": "web:s1"}
    assert manager.cancelled == ["session:web:s1"]


async def test_cancel_session_without_a_key_never_reaches_the_manager() -> None:
    manager = _FakeManager()
    result = await subagent_cancel_session({}, agent_loop_factory=_factory(manager))
    assert result == {"cancelled": 0, "session_key": ""}
    assert manager.cancelled == []


async def test_cancel_instance_mirrors_interrupts_found_semantics() -> None:
    """[N4-F4] The tested-but-unreachable primitive is a wire verb now; a row
    that finished between render and keypress is not an error."""
    manager = _FakeManager()
    manager.live.add("web:s1:hang:h1")
    hit = await subagent_cancel_instance(
        {"session_key": "web:s1", "agent": "hang", "handle": "h1"}, agent_loop_factory=_factory(manager)
    )
    assert hit == {"found": True, "session_key": "web:s1", "agent": "hang", "handle": "h1"}
    miss = await subagent_cancel_instance(
        {"session_key": "web:s1", "agent": "gone", "handle": "h9"}, agent_loop_factory=_factory(manager)
    )
    assert miss["found"] is False


async def test_cancel_instance_without_the_pair_never_reaches_the_manager() -> None:
    manager = _FakeManager()
    result = await subagent_cancel_instance({"session_key": "s"}, agent_loop_factory=_factory(manager))
    assert result == {"found": False, "session_key": "s", "agent": "", "handle": ""}
    assert manager.cancelled == []
