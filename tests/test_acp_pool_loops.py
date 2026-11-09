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
import gc
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


async def test_a_dead_loops_slot_is_forgotten_after_its_connection_went_first(tmp_path: Path) -> None:
    """A connection retired while its loop lived must not keep the slot's records forever.

    ``drop`` retires a name's connections while the loop that owned them can go
    on, and an idle close retires one the same loop may relaunch into. Only the
    loop's death ends that reuse -- and by then no connection is left in the
    pool to carry the identity into the reap.
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
        acp_agent._fresh_session_state(connection.client, "stub-session-1")
        identity = connection.client.session_identity
        await pool.drop("stub")
        assert pool.connections("stub") == [], "the connection was not retired first"
    finally:
        thread.stop()
    assert connection.loop.is_closed()

    pool._reap_gone_loops()

    assert identity not in acp_agent._SESSION_STATE, "a dead loop's records outlived its reaped connection"


async def test_a_live_loops_slot_survives_a_reap(tmp_path: Path) -> None:
    """The reap drops the slots of loops that have gone, and only those."""
    pool = AcpConnectionPool()
    command = _command()
    env = {"ACP_STUB_MODE": "ok"}
    connection = await pool.acquire(name="stub", command=command, cwd=str(tmp_path), env=env)
    acp_agent._fresh_session_state(connection.client, "stub-session-1")
    identity = connection.client.session_identity
    try:
        pool._reap_gone_loops()

        assert identity in acp_agent._SESSION_STATE, "a live loop's records were dropped"
    finally:
        acp_agent._SESSION_STATE.pop(identity, None)
        await pool.close_all()


def test_a_collected_loops_token_is_reported_and_dropped() -> None:
    """A loop gone before any reap is still named by its token, then forgotten."""
    loop = asyncio.new_event_loop()
    token = pool_mod._loop_token(loop)
    loop.close()
    del loop
    gc.collect()

    assert token in pool_mod._gone_loop_tokens()
    assert token not in pool_mod._LOOP_REFS, "a collected loop's token stayed in the registry"


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


async def test_a_gone_loops_connection_is_aborted_with_its_session_records() -> None:
    """The reap ends the process a closed loop left behind and drops its slot."""
    pool = AcpConnectionPool()
    gone = asyncio.new_event_loop()
    gone.close()
    identity = ("stub", "{}", "loop-gone")
    aborted: list[str] = []
    client = SimpleNamespace(name="stub", session_identity=identity, abort=lambda: aborted.append("abort"))
    pool._connections["stub"] = {("{}", "loop-gone"): SimpleNamespace(loop=gone, client=client)}
    acp_agent._SESSION_STATE[identity] = {}

    pool._reap_gone_loops()

    assert aborted == ["abort"]
    assert pool.connections("stub") == []
    assert identity not in acp_agent._SESSION_STATE


async def test_a_cross_loop_close_that_cannot_finish_falls_back_to_an_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close handed to another loop and then waited out ends the process directly."""
    pool = AcpConnectionPool()
    release = threading.Event()
    aborted: list[str] = []

    class _Client:
        name = "stub"

        async def close(self) -> None:
            while not release.is_set():
                await asyncio.sleep(0.01)

        def abort(self) -> None:
            aborted.append("abort")

    thread = _LoopThread()
    thread.start()
    try:
        connection = SimpleNamespace(loop=thread.loop, client=_Client())
        monkeypatch.setattr(pool_mod, "_CLOSE_TIMEOUT_S", 0.05)
        await pool._end(connection)
        assert aborted == ["abort"]
    finally:
        release.set()
        thread.stop()


async def test_a_reap_survives_session_records_it_cannot_forget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing or failing record hook is bookkeeping, not a failed reap."""
    pool = AcpConnectionPool()
    gone = asyncio.new_event_loop()
    gone.close()
    identity = ("stub", "{}", "loop-gone")
    client = SimpleNamespace(name="stub", session_identity=identity, abort=lambda: None)

    def place() -> None:
        pool._connections["stub"] = {("{}", "loop-gone"): SimpleNamespace(loop=gone, client=client)}

    monkeypatch.setattr(pool_mod, "_session_state_forget", None)
    place()
    pool._reap_gone_loops()

    def boom(record: tuple[str, str, str]) -> None:
        raise RuntimeError("records are gone")

    monkeypatch.setattr(pool_mod, "_session_state_forget", boom)
    place()
    pool._reap_gone_loops()

    assert pool.connections("stub") == []


async def test_a_drop_survives_a_close_that_raises() -> None:
    """One failing close must not take the drop, or the rest of the name, down."""
    pool = AcpConnectionPool()

    class _Client:
        name = "stub"

        async def close(self) -> None:
            raise RuntimeError("the agent refused to end")

    pool._connections["stub"] = {("{}", "loop-1"): SimpleNamespace(loop=asyncio.get_running_loop(), client=_Client())}

    await pool.drop("stub")

    assert pool.connections("stub") == []


async def test_an_idle_close_leaves_a_slot_replaced_while_it_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot another thread already dropped is not this check's to close."""
    pool = AcpConnectionPool()
    loop = asyncio.get_running_loop()

    class _Client:
        name = "stub"

        async def close(self) -> None: ...

    slot = ("{}", "loop-1")
    connection = SimpleNamespace(loop=loop, alive=True, client=_Client())
    pool._connections["stub"] = {slot: connection}
    monkeypatch.setattr(AcpConnectionPool, "_closable", staticmethod(lambda *args: True))

    async def replaced(target: Any) -> bool:
        pool._connections["stub"].pop(slot, None)
        return False

    monkeypatch.setattr(AcpConnectionPool, "_holds_resident_work", staticmethod(replaced))

    assert await pool.close_idle(0) == []


async def test_the_idle_watch_prunes_a_closed_loops_task() -> None:
    """Two loops pruning the same dead loop's task is not an error."""
    pool = AcpConnectionPool()
    gone = asyncio.new_event_loop()
    gone.close()
    pool._idle_tasks[gone] = None  # type: ignore[assignment]

    pool._watch_idle()

    assert gone not in pool._idle_tasks
    await pool.close_all()


async def test_a_bounded_drop_still_ends_a_close_that_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shutdown budget must not cancel the abort fallback away.

    ``close_all`` bounds each name with the same budget a cross-loop close is
    given, so the outer wait expires first and cancels the drop. The close
    handed to the other loop is still running at that moment: the cancellation
    has to end the process directly, or a hung close leaves the agent behind.
    """
    pool = AcpConnectionPool()
    release = threading.Event()
    aborted: list[str] = []

    class _Client:
        name = "stub"

        async def close(self) -> None:
            while not release.is_set():
                await asyncio.sleep(0.01)

        def abort(self) -> None:
            aborted.append("abort")

    thread = _LoopThread()
    thread.start()
    try:
        pool._connections["stub"] = {("{}", "loop-1"): SimpleNamespace(loop=thread.loop, client=_Client())}
        monkeypatch.setattr(pool_mod, "_CLOSE_TIMEOUT_S", 0.05)
        await pool._drop_bounded("stub")
        assert aborted == ["abort"], "the bounded close abandoned the agent without ending it"
    finally:
        release.set()
        thread.stop()
