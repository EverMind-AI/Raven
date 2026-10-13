"""The orphan sweep: recorded groups whose host died, ended at the next launch.

Real processes throughout -- a sweep judged on mocks has never killed anything.
The "agent" is a plain ``setsid`` child, which is what an ACP launch produces
(``start_new_session=True``): its own session, its own group, insensible to its
host's death.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from raven.acp_client import orphans


@pytest.fixture(autouse=True)
def _registry(tmp_path, monkeypatch):
    monkeypatch.setattr(orphans, "registry_path", lambda: tmp_path / "agents.json")
    yield


def _spawn_group() -> subprocess.Popen:
    """A stand-in agent: own session (as every ACP launch makes), quiet, long-lived."""
    return subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def _dead_host_record(proc: subprocess.Popen) -> None:
    """Record ``proc``'s group under a host that has already exited.

    The "host" is a real process killed before the record is written, so the
    entry is exactly what a crashed host leaves: a pid that answers nothing,
    with the start time it had while alive.
    """
    victim = _spawn_group()
    victim_pid = victim.pid
    victim_start = orphans.process_start(victim_pid)
    victim.kill()
    victim.wait()
    entry = {
        "name": "standin",
        "pgid": proc.pid,
        "agent_pid": proc.pid,
        "agent_start": orphans.process_start(proc.pid),
        "host_pid": victim_pid,
        "host_start": victim_start,
    }
    _write([entry])


def _write(entries: list[dict]) -> None:
    import json

    path = orphans.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def _read() -> list[dict]:
    import json

    raw = orphans.registry_path().read_text(encoding="utf-8")
    return json.loads(raw)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _reaped(proc: subprocess.Popen, sig: int) -> bool:
    """Whether ``proc`` died by ``sig`` -- waits out the zombie the kill left."""
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == -sig


def test_start_time_names_the_live_process_on_this_host():
    start = orphans.process_start(os.getpid())
    assert start is not None
    # A second read of the same process agrees with the first: this is the
    # property the whole reuse guard stands on.
    assert orphans.process_start(os.getpid()) == pytest.approx(start)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="no process groups on this platform")
def test_sweep_ends_the_group_of_a_dead_host():
    agent = _spawn_group()
    try:
        _dead_host_record(agent)
        assert orphans.sweep_orphans() == 1
        assert _reaped(agent, signal.SIGKILL)
        assert _read() == []  # the judged record is spent
    finally:
        try:
            os.killpg(agent.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="no process groups on this platform")
def test_sweep_spares_a_group_whose_host_still_lives():
    agent = _spawn_group()
    try:
        _write(
            [
                {
                    "name": "standin",
                    "pgid": agent.pid,
                    "agent_pid": agent.pid,
                    "agent_start": orphans.process_start(agent.pid),
                    "host_pid": os.getpid(),  # this very process: alive, start matches
                    "host_start": orphans.process_start(os.getpid()),
                }
            ]
        )
        assert orphans.sweep_orphans() == 0
        assert _alive(agent.pid)
        assert len(_read()) == 1  # kept: this host may yet die unwound
    finally:
        os.killpg(agent.pid, signal.SIGKILL)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="no process groups on this platform")
def test_sweep_judges_a_reused_host_pid_by_start_time():
    agent = _spawn_group()
    try:
        # The pid is alive (ours), but its start time is not the one recorded:
        # the host died and its pid was recycled. That is an orphan.
        _write(
            [
                {
                    "name": "standin",
                    "pgid": agent.pid,
                    "agent_pid": agent.pid,
                    "agent_start": orphans.process_start(agent.pid),
                    "host_pid": os.getpid(),
                    "host_start": 1.0,
                }
            ]
        )
        assert orphans.sweep_orphans() == 1
        assert _reaped(agent, signal.SIGKILL)
    finally:
        try:
            os.killpg(agent.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="no process groups on this platform")
def test_sweep_never_kills_when_the_agent_pid_was_recycled():
    stranger = _spawn_group()
    try:
        # An orphaned host whose agent already exited; its pid now names a
        # stranger. The recorded start time no longer matches, so the group
        # is left alone -- and the spent record is dropped either way.
        _write(
            [
                {
                    "name": "standin",
                    "pgid": stranger.pid,
                    "agent_pid": stranger.pid,
                    "agent_start": 1.0,
                    "host_pid": -1,
                    "host_start": None,
                }
            ]
        )
        assert orphans.sweep_orphans() == 0
        assert _alive(stranger.pid)
        assert _read() == []
    finally:
        os.killpg(stranger.pid, signal.SIGKILL)


def test_a_corrupt_registry_reads_as_empty():
    path = orphans.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert orphans.sweep_orphans() == 0
    # The next write replaces it wholesale; nothing lingers.
    orphans.record_agent("x", 123456)
    assert [e["pgid"] for e in _read()] == [123456]


def test_record_then_forget_leaves_no_record():
    orphans.record_agent("standin", 424242)
    assert [e["pgid"] for e in _read()] == [424242]
    orphans.forget(424242)
    assert _read() == []


@pytest.mark.skipif(sys.platform == "win32", reason="posix process table")
def test_start_time_matches_the_system_view():
    agent = _spawn_group()
    try:
        start = orphans.process_start(agent.pid)
        assert start is not None
        out = subprocess.run(  # noqa: S603 - fixed argv
            ["ps", "-p", str(agent.pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert out.returncode == 0
        # Second-granularity agreement with ps is the check that the sysctl /
        # /proc read names the same moment the system table does.
        seen = time.mktime(time.strptime(out.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
        assert abs(start - seen) < 2
    finally:
        os.killpg(agent.pid, signal.SIGKILL)
        agent.wait(timeout=10)
