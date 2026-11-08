"""The ACP pool closes an agent's process once it has been idle for the configured budget."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.acp_client import pool as pool_mod
from raven.acp_client.pool import AcpConnectionPool, _Connection, _SessionElicitors, _SessionRouter, close_pool
from raven.utils.commands import command_quote

_STUB = Path(__file__).with_name("acp_stub_server.py")
_LOADS = {"agentCapabilities": {"loadSession": True}}
_RESIDENT = {"agentCapabilities": {"loadSession": True, "_meta": {"raven.residentWork": True}}}
_NO_LOAD = {"agentCapabilities": {}}


@pytest.fixture(autouse=True)
async def _no_pooled_connections():
    yield
    await close_pool()


class _Client:
    def __init__(self, *, idle: float = 0.0, busy: bool = False, alive: bool = True, resident: Any = None) -> None:
        self.name = "agent"
        self._idle_since = time.monotonic() - idle
        self.busy = busy
        self.alive = alive
        self.closed = False
        self.resident = resident
        self.asked: list[str] = []

    async def request(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> Any:
        self.asked.append(method)
        if isinstance(self.resident, BaseException):
            raise self.resident
        return {"resident": self.resident}

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._idle_since

    async def close(self) -> None:
        self.closed = True
        self.alive = False


def _held(client: _Client, initialize: Any, *, leased_ago: float = 3600.0, seen: tuple[str, ...] = ()) -> _Connection:
    router = _SessionRouter("agent")
    router.seen.update(seen)
    connection = _Connection(client, router, _SessionElicitors("agent"), initialize=initialize)  # type: ignore[arg-type]
    connection.leased_at = time.monotonic() - leased_ago
    return connection


def _pool_holding(**connections: _Connection) -> AcpConnectionPool:
    pool = AcpConnectionPool()
    for name, connection in connections.items():
        pool._connections[name] = {"": connection}
    return pool


async def test_a_quiet_agent_that_can_reload_its_sessions_is_closed() -> None:
    client = _Client(idle=120)
    pool = _pool_holding(quiet=_held(client, _LOADS, seen=("s1",)))

    assert await pool.close_idle(60) == ["quiet"]
    assert client.closed
    assert pool.live("quiet") == []


async def test_an_agent_that_says_it_holds_resident_work_is_kept() -> None:
    """An armed wake or a running service is work nobody is asking for yet: closing the agent loses it.

    Asked only of an agent that declared it can answer, and only once closing
    it is otherwise settled. No answer in time, or none declared, closes it as before.
    """
    holding = _Client(idle=3600, resident=True)
    idle = _Client(idle=3600, resident=False)
    deaf = _Client(idle=3600, resident=TimeoutError())
    undeclared = _Client(idle=3600, resident=True)
    busy = _Client(idle=3600, busy=True, resident=True)
    pool = _pool_holding(
        holding=_held(holding, _RESIDENT),
        idle=_held(idle, _RESIDENT),
        deaf=_held(deaf, _RESIDENT),
        undeclared=_held(undeclared, _LOADS),
        busy=_held(busy, _RESIDENT),
    )

    assert sorted(await pool.close_idle(60)) == ["deaf", "idle", "undeclared"]
    assert not holding.closed and not busy.closed
    assert holding.asked == ["_raven/residentWork"]
    assert undeclared.asked == [] and busy.asked == []


@pytest.mark.slow(reason="Starts a real stub ACP process and waits out the wake it arms")
async def test_an_agent_holding_a_wake_is_kept_until_the_wake_has_fired(tmp_path: Path) -> None:
    """A turn arms an in-process wake and ends; the agent is then silent until the wake fires.

    The real process, through the pool: closed early, the wake would never fire.
    """
    mark = tmp_path / "fired"
    pool = AcpConnectionPool()
    command = f"{command_quote(sys.executable)} {command_quote(str(_STUB))}"
    env = {"ACP_STUB_MODE": "resident", "ACP_STUB_WAKE_S": "1.0", "ACP_STUB_WAKE_MARK": str(mark)}
    held = await pool.acquire(name="stub", command=command, cwd=str(tmp_path), env=env)
    opened = await held.client.request("session/new", {"cwd": str(tmp_path), "mcpServers": []})
    held.router.reserve(opened["sessionId"])
    prompt = [{"type": "text", "text": "remind me in a second"}]
    await held.client.request("session/prompt", {"sessionId": opened["sessionId"], "prompt": prompt})
    held.leased_at -= 10

    await asyncio.sleep(0.3)
    assert await pool.close_idle(0.2) == []
    assert held.alive

    for _ in range(100):
        if mark.exists():
            break
        await asyncio.sleep(0.05)
    assert mark.exists(), "the wake fired in the agent it was armed in"
    await asyncio.sleep(0.3)
    assert await pool.close_idle(0.2) == ["stub"]
    assert not held.alive


async def test_a_connection_whose_agent_already_ended_is_dropped() -> None:
    gone = _Client(idle=0, alive=False)
    pool = _pool_holding(gone=_held(gone, _NO_LOAD, leased_ago=0, seen=("s1",)))

    assert await pool.close_idle(60) == ["gone"]
    assert pool._connections == {}


async def test_nothing_is_closed_while_a_request_is_in_flight_either_way() -> None:
    """A held wake is a prompt still waiting for its answer, so it reads as busy too."""
    client = _Client(idle=120, busy=True)
    pool = _pool_holding(working=_held(client, _LOADS))

    assert await pool.close_idle(60) == []
    assert not client.closed


async def test_a_connection_just_handed_out_is_left_for_its_caller() -> None:
    client = _Client(idle=120)
    pool = _pool_holding(leased=_held(client, _LOADS, leased_ago=1.0))

    assert await pool.close_idle(60) == []
    assert not client.closed


async def test_an_agent_heard_from_recently_is_kept() -> None:
    client = _Client(idle=5)
    pool = _pool_holding(chatty=_held(client, _LOADS))

    assert await pool.close_idle(60) == []


async def test_an_agent_that_cannot_reload_keeps_the_sessions_it_opened() -> None:
    """Closing it would hand the next turn of a stateful instance a blank session."""
    keeps = _Client(idle=120)
    unused = _Client(idle=120)
    pool = _pool_holding(
        holds=_held(keeps, _NO_LOAD, seen=("s1",)),
        never_used=_held(unused, _NO_LOAD),
    )

    assert await pool.close_idle(60) == ["never_used"]
    assert not keeps.closed
    assert unused.closed


async def test_a_budget_of_zero_or_none_in_the_config_turns_closing_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from raven.config import live as live_mod

    config = tmp_path / "config.json"
    monkeypatch.setattr(live_mod, "default_live", lambda: live_mod.LiveConfig(config))
    client = _Client(idle=10_000)
    pool = _pool_holding(quiet=_held(client, _LOADS))

    for value in (0, None):
        config.write_text(json.dumps({"subagents": {"runtime": {"idleCloseS": value}}}), encoding="utf-8")
        assert await pool.close_idle() == []
    config.write_text(json.dumps({"subagents": {"runtime": {"idleCloseS": 30}}}), encoding="utf-8")
    assert await pool.close_idle() == ["quiet"]


async def test_the_idle_check_starts_with_a_connection_and_ends_with_the_last(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool_mod, "_IDLE_CHECK_MIN_S", 0.01)
    monkeypatch.setattr(AcpConnectionPool, "_idle_budget", staticmethod(lambda: 0.04))
    client = _Client(idle=0)
    pool = _pool_holding(soon=_held(client, _LOADS, leased_ago=0))
    pool._watch_idle()
    task = pool._idle_tasks.get(asyncio.get_running_loop())
    assert task is not None

    await asyncio.wait_for(task, timeout=5)

    assert client.closed
    assert pool._connections == {}


async def test_an_idle_agent_process_ends_and_the_next_dispatch_starts_a_fresh_one(tmp_path: Path) -> None:
    pool = AcpConnectionPool()
    command = f"{command_quote(sys.executable)} {command_quote(str(_STUB))}"
    first = await pool.acquire(name="stub", command=command, cwd=str(tmp_path), env={"ACP_STUB_MODE": "ok"})
    opened = await first.client.request("session/new", {"cwd": str(tmp_path), "mcpServers": []})
    first.router.reserve(opened["sessionId"])
    first_pid = first.client.endpoint.pid
    first.leased_at -= 10

    await asyncio.sleep(0.3)
    assert await pool.close_idle(0.2) == ["stub"]
    assert not first.alive

    second = await pool.acquire(name="stub", command=command, cwd=str(tmp_path), env={"ACP_STUB_MODE": "ok"})
    assert second.alive
    assert second.client.endpoint.pid != first_pid
    await pool.close_all()


def test_the_idle_budget_is_read_from_the_host_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from raven.config import live as live_mod

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"subagents": {"runtime": {"idleCloseS": 42}}}), encoding="utf-8")
    monkeypatch.setattr(live_mod, "default_live", lambda: live_mod.LiveConfig(config))
    assert AcpConnectionPool._idle_budget() == 42
    config.write_text("{}", encoding="utf-8")
    assert AcpConnectionPool._idle_budget() == 600


def test_a_busy_client_property_reads_requests_both_ways() -> None:
    from raven.acp_client.client import AcpClient

    client = AcpClient(name="x", endpoint=SimpleNamespace(returncode=None))  # type: ignore[arg-type]
    assert not client.busy
    loop = asyncio.new_event_loop()
    try:
        client._pending[1] = loop.create_future()
        assert client.busy
        client._pending.clear()
        client._answer_tasks.add(object())  # type: ignore[arg-type]
        assert client.busy
    finally:
        loop.close()


def test_a_rejected_edit_keeps_the_last_valid_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from raven.config import live as live_mod

    config = tmp_path / "config.json"
    reader = live_mod.LiveConfig(config)
    config.write_text(
        json.dumps({"subagents": {"runtime": {"idleCloseS": 30, "forkTemplate": False}}}), encoding="utf-8"
    )
    assert live_mod.subagent_runtime(reader).idle_close_s == 30
    config.write_text(json.dumps({"subagents": {"runtime": {"idleCloseS": "soon"}}}), encoding="utf-8")
    kept = live_mod.subagent_runtime(reader)
    assert kept.idle_close_s == 30 and kept.fork_template is False


@pytest.mark.parametrize("edit", [{"runtime": False}, {"runtime": "off"}, {"runtime": []}, False])
def test_a_runtime_edit_that_is_not_a_mapping_is_rejected_not_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, edit: Any
) -> None:
    """Only a section that left the file, or a null, brings the defaults back; any other value is a bad edit."""
    from raven.config import live as live_mod

    config = tmp_path / "config.json"
    reader = live_mod.LiveConfig(config)
    config.write_text(
        json.dumps({"subagents": {"runtime": {"idleCloseS": 30, "llmRelay": False, "forkTemplate": False}}}),
        encoding="utf-8",
    )
    assert live_mod.subagent_runtime(reader).idle_close_s == 30
    config.write_text(json.dumps({"subagents": edit}), encoding="utf-8")
    kept = live_mod.subagent_runtime(reader)
    assert (kept.idle_close_s, kept.llm_relay, kept.fork_template) == (30, False, False)

    config.write_text(json.dumps({"subagents": {"runtime": None}}), encoding="utf-8")
    assert live_mod.subagent_runtime(reader).idle_close_s == 600
    config.write_text(json.dumps({}), encoding="utf-8")
    assert live_mod.subagent_runtime(reader).idle_close_s == 600


async def test_an_idle_check_on_another_loop_leaves_this_loops_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """The RPC server runs a CLI command's own ``asyncio.run`` on a worker thread, beside its own loop.

    A connection's pipes belong to the loop that launched it, so only that
    loop's check may close it.
    """
    monkeypatch.setattr(pool_mod, "_IDLE_CHECK_MIN_S", 0.01)
    monkeypatch.setattr(AcpConnectionPool, "_idle_budget", staticmethod(lambda: 0.04))
    here = _Client(idle=3600)
    pool = _pool_holding(here=_held(here, _LOADS))

    async def beside() -> list[str]:
        closed = await pool.close_idle(0.04)
        pool._watch_idle()
        await asyncio.sleep(0.2)
        return closed

    assert await asyncio.to_thread(asyncio.run, beside()) == []
    assert not here.closed and "here" in pool._connections

    assert await pool.close_idle(0.04) == ["here"]
    assert here.closed


async def test_an_idle_check_on_another_loop_leaves_an_agents_lock_usable_here() -> None:
    """A worker loop's check must not bind an agent's lock while this loop holds it.

    The main loop and a worker loop (a CLI command the RPC server runs) each
    hold a binding of one agent. ``asyncio.Lock`` binds to the loop of its first
    waiter, so a worker check that waited on the lock this loop holds would leave
    every later wait here raising "bound to a different event loop".
    """
    here = _Client(idle=0)
    there = _Client(idle=3600)
    pool = AcpConnectionPool()
    beside_connection = _held(there, _LOADS)
    pool._connections["agent"] = {"here": _held(here, _LOADS, leased_ago=0), "there": beside_connection}
    lock = pool._lock_for("agent")
    await lock.acquire()

    async def beside() -> None:
        beside_connection.loop = asyncio.get_running_loop()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(pool.close_idle(60), timeout=0.5)

    await asyncio.to_thread(asyncio.run, beside())
    lock.release()

    async def hold_briefly() -> None:
        async with pool._lock_for("agent"):
            await asyncio.sleep(0.01)

    await asyncio.gather(hold_briefly(), hold_briefly())


async def test_a_failed_idle_check_does_not_end_the_next_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool_mod, "_IDLE_CHECK_MIN_S", 0.01)
    monkeypatch.setattr(AcpConnectionPool, "_idle_budget", staticmethod(lambda: 0.04))
    client = _Client(idle=0)
    pool = _pool_holding(soon=_held(client, _LOADS, leased_ago=0))
    real = pool.close_idle
    calls: list[int] = []

    async def flaky(budget=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("once")
        return await real(budget)

    monkeypatch.setattr(pool, "close_idle", flaky)
    pool._watch_idle()
    await asyncio.wait_for(pool._idle_tasks[asyncio.get_running_loop()], timeout=5)
    assert len(calls) >= 2 and client.closed
