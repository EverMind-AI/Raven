"""The ACP pool hands a connection only to the loop that can read it.

One pool, more than one loop: the RPC server runs a CLI command such as
``raven agent -m`` on a worker thread under its own ``asyncio.run``, beside
the host's loop. A connection's pipes and its reader task belong to the loop
that made them, so a connection handed to another loop answers nothing -- the
frames arrive on a reader that loop is not running, and the request waiting
on them waits on a future another thread settles.

So a loop that holds nothing launches its own, and the pool keeps both: a
process that ran outside the pool's bookkeeping was one no idle check and no
``close_all`` could end.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.acp_client import acp_agent
from raven.acp_client import pool as pool_mod
from raven.acp_client.pool import AcpConnectionPool
from raven.utils.commands import command_quote

_STUB = Path(__file__).with_name("acp_stub_server.py")


def _command() -> str:
    return f"{command_quote(sys.executable)} {command_quote(str(_STUB))}"


class _LoopThread:
    """An event loop of its own, in a thread of its own.

    The shape the RPC server gives a CLI command it runs on a worker thread.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="acp-pool-loop", daemon=True)
        self._ready = threading.Event()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(10), "the loop did not start"

    async def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coro`` on this loop, from the caller's."""
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self.loop))

    def stop(self) -> None:
        """End the loop the way ``asyncio.run`` ends one, and close it."""

        async def shutdown() -> None:
            # Rounds, not one: a cancelled reader leaves a shielded reap behind
            # it, and that one is a task of its own to cancel as well.
            for _ in range(3):
                tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                if not tasks:
                    return
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(10)
        self.loop.close()


async def test_two_loops_acquire_one_agent_and_each_gets_a_connection_it_can_read(tmp_path: Path) -> None:
    """The repro from #1152: two loops take one agent at the same moment.

    Each loop gets a connection whose streams it can read -- so it can answer
    on it -- and the pool keeps both, so neither process runs outside the
    pool's bookkeeping.
    """
    pool = AcpConnectionPool()
    command = _command()
    env = {"ACP_STUB_MODE": "ok"}
    threads = [_LoopThread() for _ in range(2)]
    for thread in threads:
        thread.start()

    async def acquire(thread: _LoopThread) -> Any:
        connection = await pool.acquire(name="stub", command=command, cwd=str(tmp_path), env=env)
        opened = await connection.client.request("session/new", {"cwd": str(tmp_path), "mcpServers": []})
        return connection, opened["sessionId"]

    try:
        (first, first_session), (second, second_session) = await asyncio.gather(
            *(thread.run(acquire(thread)) for thread in threads)
        )
        assert first is not second, "the two loops were handed the same connection"
        assert first.loop is not second.loop
        assert first_session and second_session, "a loop was handed a connection it cannot answer on"
        assert len(pool.connections("stub")) == 2, "the pool kept only one of the two connections"
        # Two processes, two slots: the records keyed by a slot belong to one
        # process, and these two processes number their sessions apart.
        assert first.client.session_identity != second.client.session_identity

        processes = [
            connection.client.endpoint.proc for connection, _ in ((first, first_session), (second, second_session))
        ]
        assert len({process.pid for process in processes}) == 2, "the two loops were handed the same process"
        # A reaped child's return code is the cross-platform answer to "is it
        # still running": Windows keeps a terminated process's handle open while
        # this process holds the Process object, so a pid probe still finds it.
        assert all(process.returncode is None for process in processes)

        await pool.close_all()
        assert all(process.returncode is not None for process in processes), "a process outlived close_all"
        assert pool.connections("stub") == []
    finally:
        for thread in threads:
            thread.stop()


async def test_a_loop_is_not_handed_the_connection_of_another_loop(tmp_path: Path) -> None:
    """A connection made on another loop cannot answer from here."""
    pool = AcpConnectionPool()
    command = _command()
    env = {"ACP_STUB_MODE": "ok"}
    thread = _LoopThread()
    thread.start()
    try:
        there = await thread.run(
            pool.acquire(name="stub", command=command, cwd=str(tmp_path), env=env),
        )
        here = await pool.acquire(name="stub", command=command, cwd=str(tmp_path), env=env)
        assert here is not there
        assert here.loop is asyncio.get_running_loop()
        assert len(pool.connections("stub")) == 2
    finally:
        await pool.close_all()
        thread.stop()


async def test_a_second_acquire_on_the_same_loop_reuses_the_connection(tmp_path: Path) -> None:
    """One loop, one process: the pool still shares a connection between callers."""
    pool = AcpConnectionPool()
    command = _command()
    env = {"ACP_STUB_MODE": "ok"}
    first = await pool.acquire(name="stub", command=command, cwd=str(tmp_path), env=env)
    second = await pool.acquire(name="stub", command=command, cwd=str(tmp_path), env=env)
    assert second is first
    assert len(pool.connections("stub")) == 1
    await pool.close_all()


async def test_a_connection_whose_loop_is_gone_is_ended_directly(tmp_path: Path) -> None:
    """A loop that has closed leaves a connection nothing can wait on.

    Its close would wait on tasks that loop is not running, so the process is
    ended directly instead.
    """
    pool = AcpConnectionPool()
    command = _command()
    env = {"ACP_STUB_MODE": "ok"}
    thread = _LoopThread()
    thread.start()
    try:
        connection = await thread.run(
            pool.acquire(name="stub", command=command, cwd=str(tmp_path), env=env),
        )
        # What a session opened on this connection records for the slot.
        acp_agent._fresh_session_state(connection.client, "stub-session-1")
        identity = connection.client.session_identity
    finally:
        thread.stop()
    assert connection.loop.is_closed()

    await pool.close_all()
    assert connection.client.closed, "the connection of a gone loop was not ended"
    assert pool.connections("stub") == []
    # A dead loop never relaunches on its slot's name, so its records can only
    # leak: nothing can resume a session of the agent process it owned.
    assert identity not in acp_agent._SESSION_STATE, "a dead loop's session records were left behind"


class _QuietClient:
    """A client whose launch and handshake the test can hold open."""

    def __init__(self, started: threading.Event, resume: threading.Event) -> None:
        self.name = "stub"
        self.session_identity: tuple[str, str, str] | None = None
        self.closed = False
        self._started = started
        self._resume = resume

    @property
    def alive(self) -> bool:
        return not self.closed

    async def request(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
        if method == "initialize":
            self._started.set()
            while not self._resume.is_set():
                await asyncio.sleep(0.005)
        return {}

    async def close(self) -> None:
        self.closed = True


async def test_an_acquire_finishing_under_an_idle_close_stays_in_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle close must not orphan a launch that is already in flight.

    ``acquire`` reads the name's entry before a handshake that can take
    seconds, and registers its connection after it. Another loop's idle check
    can empty and drop that entry in between; the registration has to make the
    entry again, or the connection runs outside the pool's bookkeeping -- the
    state #1152 is about, where no idle check and no ``close_all`` could end
    its process.
    """
    pool = AcpConnectionPool()
    loop = asyncio.get_running_loop()

    class _DeadClient:
        name = "stub"

        async def close(self) -> None: ...

    pool._connections["stub"] = {
        ("host-binding", "loop-host"): SimpleNamespace(loop=loop, alive=False, client=_DeadClient())
    }

    started = threading.Event()
    resume = threading.Event()
    client = _QuietClient(started, resume)

    async def _launcher() -> object:
        return object()

    monkeypatch.setattr(pool_mod.AcpClient, "over", staticmethod(lambda endpoint, **kwargs: client))
    monkeypatch.setattr(pool_mod, "open_journal", lambda name: None)

    thread = _LoopThread()
    thread.start()
    try:
        acquiring = asyncio.create_task(
            thread.run(pool.acquire(name="stub", command="stub", cwd=None, env=None, launcher=_launcher))
        )
        assert await asyncio.to_thread(started.wait, 10), "the launch never reached its handshake"
        # The other loop's idle check empties the name and drops its entry.
        assert await pool.close_idle(0) == ["stub"]
        assert pool._connections == {}
        resume.set()
        connection = await acquiring
        assert pool.connections("stub") == [connection], "the finished launch was left outside the pool"
    finally:
        resume.set()
        await pool.close_all()
        thread.stop()


async def test_a_session_delete_prefers_the_connection_of_the_asking_loop(monkeypatch) -> None:
    """A session/delete must not be sent over another loop's connection."""
    pool = AcpConnectionPool()
    loop = asyncio.get_running_loop()
    foreign_loop = asyncio.new_event_loop()
    try:

        class _Client:
            def __init__(self, name: str) -> None:
                self.name = name
                self.calls: list[str] = []

            async def request(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
                self.calls.append(method)
                return {}

        foreign_client = _Client("foreign")
        here_client = _Client("here")
        foreign = SimpleNamespace(
            loop=foreign_loop,
            alive=True,
            client=foreign_client,
            router=SimpleNamespace(seen={"s1"}),
            initialize={},
        )
        here = SimpleNamespace(
            loop=loop,
            alive=True,
            client=here_client,
            router=SimpleNamespace(seen=set()),
            initialize={},
        )
        pool._connections["agent"] = {("b", "loop-1"): foreign, ("b", "loop-2"): here}
        monkeypatch.setattr(pool_mod, "handshake_of", lambda initialize: SimpleNamespace(can_delete=True))

        assert await pool.delete_session("agent", "s1") is True

        assert foreign_client.calls == [], "the delete was sent over another loop's connection"
        assert here_client.calls == ["session/delete"]
    finally:
        foreign_loop.close()


async def test_a_session_delete_with_only_another_loops_connection_is_skipped(monkeypatch) -> None:
    """That connection cannot answer here, so the best effort is to send nothing."""
    pool = AcpConnectionPool()
    foreign_loop = asyncio.new_event_loop()

    class _Client:
        def __init__(self) -> None:
            self.name = "foreign"
            self.calls: list[str] = []

        async def request(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
            self.calls.append(method)
            return {}

    client = _Client()
    pool._connections["agent"] = {
        ("b", "loop-1"): SimpleNamespace(
            loop=foreign_loop,
            alive=True,
            client=client,
            router=SimpleNamespace(seen={"s1"}),
            initialize={},
        )
    }
    monkeypatch.setattr(pool_mod, "handshake_of", lambda initialize: SimpleNamespace(can_delete=True))
    try:
        assert await pool.delete_session("agent", "s1") is False
        assert client.calls == []
    finally:
        foreign_loop.close()


async def test_a_drop_starts_every_close_before_waiting_for_one() -> None:
    """One slow close must not keep the others' from being started."""
    pool = AcpConnectionPool()
    loop = asyncio.get_running_loop()
    started: list[str] = []
    release = asyncio.Event()

    class _Client:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            started.append(self.name)
            await release.wait()

    pool._connections["agent"] = {
        ("a", "loop-1"): SimpleNamespace(loop=loop, client=_Client("a")),
        ("b", "loop-1"): SimpleNamespace(loop=loop, client=_Client("b")),
    }

    dropping = asyncio.create_task(pool.drop("agent"))
    for _ in range(100):
        if len(started) == 2:
            break
        await asyncio.sleep(0.01)
    assert started == ["a", "b"], "a drop waited for one close before starting the next"

    release.set()
    await dropping
