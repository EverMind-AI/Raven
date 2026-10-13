"""The orphan sweep: a dead host's agents are reaped, a live host's are not.

Every case here builds a real process group (``start_new_session=True``, the
same shape :meth:`AcpClient.launch` makes) so the sweep's ``killpg`` hits a
group the test actually started -- a monkeypatched ``os.killpg`` would prove
the call, not the reap.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

import pytest

from raven.acp_client import orphans

_STUB = "import time; time.sleep(120)"


async def _spawn_stub_group(script: str = _STUB) -> asyncio.subprocess.Process:
    """One stub in a session of its own, the shape launch() gives an agent."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


def _a_dead_pid() -> int:
    """A pid that belonged to a real process and is fully reaped."""
    out = subprocess.run(["sh", "-c", "true & echo $!"], capture_output=True, text=True)
    return int(out.stdout.strip())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return False
    return True


async def _wait_gone(pid: int, budget_s: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + budget_s
    while asyncio.get_running_loop().time() < deadline:
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


def test_process_start_time_of_self_is_a_number() -> None:
    started = orphans.process_start_time(os.getpid())
    assert started is not None
    assert started > 0


def test_process_start_time_of_dead_pid_is_none() -> None:
    assert orphans.process_start_time(_a_dead_pid()) is None


def test_process_start_time_of_garbage_pid_is_none() -> None:
    assert orphans.process_start_time(0) is None
    assert orphans.process_start_time(-1) is None


@pytest.mark.skipif(not sys.platform.startswith(("darwin", "linux")), reason="posix sweep only")
async def test_sweep_kills_the_group_of_an_agent_whose_host_died() -> None:
    stub = await _spawn_stub_group()
    try:
        orphans.record_agent("stub", stub.pid)
        # The owner is gone: the record names a dead pid, not this test.
        records = orphans._read_registry()
        records[-1]["owner_pid"] = _a_dead_pid()
        orphans._write_registry(records)

        killed = orphans.sweep_orphans()

        assert stub.pid in killed
        assert await _wait_gone(stub.pid), "stub survived the sweep"
        assert orphans._read_registry() == [], "reaped record must not linger"
    finally:
        if _pid_alive(stub.pid):
            os.killpg(stub.pid, signal.SIGKILL)
        await stub.wait()


@pytest.mark.skipif(not sys.platform.startswith(("darwin", "linux")), reason="posix sweep only")
async def test_sweep_spares_the_agents_of_a_live_owner() -> None:
    stub = await _spawn_stub_group()
    try:
        orphans.record_agent("stub", stub.pid)  # owner is this live test process

        assert orphans.sweep_orphans() == []

        assert _pid_alive(stub.pid), "sweep killed an agent whose owner lives"
        assert [r["pgid"] for r in orphans._read_registry()] == [stub.pid]
    finally:
        os.killpg(stub.pid, signal.SIGKILL)
        await stub.wait()


@pytest.mark.skipif(not sys.platform.startswith(("darwin", "linux")), reason="posix sweep only")
async def test_sweep_never_kills_a_reused_pid() -> None:
    stub = await _spawn_stub_group()
    try:
        # A record whose start time predates the process now wearing the pid:
        # the pid was recycled since, and killing it would kill a stranger.
        started = orphans.process_start_time(stub.pid)
        assert started is not None
        orphans._write_registry(
            [
                {
                    "name": "ancient",
                    "pgid": stub.pid,
                    "owner_pid": _a_dead_pid(),
                    "start_time": started - 10_000.0,
                    "recorded_at": 0.0,
                }
            ]
        )

        assert orphans.sweep_orphans() == []

        assert _pid_alive(stub.pid), "the sweep killed a stranger's pid"
        assert orphans._read_registry() == [], "the stale record is dropped regardless"
    finally:
        os.killpg(stub.pid, signal.SIGKILL)
        await stub.wait()


@pytest.mark.skipif(not sys.platform.startswith(("darwin", "linux")), reason="posix sweep only")
async def test_sweep_drops_a_record_whose_agent_is_already_gone() -> None:
    stub = await _spawn_stub_group()
    orphans.record_agent("stub", stub.pid)
    os.killpg(stub.pid, signal.SIGKILL)
    await stub.wait()
    records = orphans._read_registry()
    records[-1]["owner_pid"] = _a_dead_pid()
    orphans._write_registry(records)

    assert orphans.sweep_orphans() == []
    assert orphans._read_registry() == []


@pytest.mark.skipif(not sys.platform.startswith(("darwin", "linux")), reason="posix sweep only")
async def test_launch_records_the_agent_for_a_later_sweep() -> None:
    """The wiring the whole scheme rests on: a real launch writes the record."""
    from raven.acp_client.client import AcpClient

    # A launch needs no protocol traffic to succeed -- it spawns, starts its
    # read loops and records; a stub that never speaks is a per-request
    # timeout, not a launch failure.
    client = await AcpClient.launch(name="wired", command=f"{sys.executable} -c '{_STUB}'")
    try:
        records = [r for r in orphans._read_registry() if r["pgid"] == client._pgid]
        assert records, "launch wrote no orphan record"
        assert records[0]["owner_pid"] == os.getpid()
        assert records[0]["start_time"] == pytest.approx(orphans.process_start_time(client._pgid), abs=2.0)
    finally:
        await client.close()
    # close() killed the group; the record stays but names a dead pid with the
    # right start time, so the next sweep drops it without killing anyone.
    records = [r for r in orphans._read_registry() if r["pgid"] == client._pgid]
    assert records, "a closed launch's record is kept until the next sweep"
    assert orphans.sweep_orphans() == []
    assert not _pid_alive(client._pgid), "closed agent survived its own close()"
