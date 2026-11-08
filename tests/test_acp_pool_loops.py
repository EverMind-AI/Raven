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
import os
import sys
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from raven.acp_client.pool import AcpConnectionPool
from raven.utils.commands import command_quote

_STUB = Path(__file__).with_name("acp_stub_server.py")


def _command() -> str:
    return f"{command_quote(sys.executable)} {command_quote(str(_STUB))}"


def _running(pid: int) -> bool:
    """Whether this pid still names a running process.

    A child of this process that has exited but not been reaped is a zombie,
    which is not running; ``os.kill`` would still find it, so ``/proc`` is
    read where it is there.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    else:
        return raw[raw.rindex(")") + 2] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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

        pids = [connection.client.endpoint.pid for connection, _ in ((first, first_session), (second, second_session))]
        assert len(set(pids)) == 2, "the two loops were handed the same process"
        assert all(pid is not None and _running(pid) for pid in pids)

        await pool.close_all()
        assert not any(_running(pid) for pid in pids if pid is not None), "a process outlived close_all"
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
    finally:
        thread.stop()
    assert connection.loop.is_closed()

    await pool.close_all()
    assert connection.client.closed, "the connection of a gone loop was not ended"
    assert pool.connections("stub") == []
