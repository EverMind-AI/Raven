"""Orphan guard for ACP agent process groups (issue #928).

``launch`` starts agents with ``start_new_session=True``, so a host that dies
without ``close_pool`` (SIGKILL, OOM, crash) leaves the agent running forever.
These tests walk the guard directly: a real stub process group, a real record
entry, and the sweep's three verdicts -- kill the orphan, spare the live
owner, never kill a recycled pid.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from raven.acp_client.client import (
    _boot_token,
    _process_birth,
    forget_agent_group,
    record_agent_group,
    sweep_orphaned_agents,
)

_STUB = """import time; time.sleep(120)"""


@pytest.fixture()
def guard_file(tmp_path, monkeypatch):
    path = str(tmp_path / "guard.json")
    monkeypatch.setenv("RAVEN_ACP_ORPHAN_FILE", path)
    return path


def _spawn_stub() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _STUB],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # same shape launch() gives every agent
    )


def _spawn_transient() -> subprocess.Popen:
    """A process that exists long enough to be born, then dies."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(0.2)
    return proc


def _kill_transient(proc: subprocess.Popen) -> None:
    """End a transient owner now: its pid must be free for the next verdict."""
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _entry(pgid: int, owner_pid: int, owner_birth: str | None) -> dict:
    return {
        "pgid": pgid,
        "birth": _process_birth(pgid),
        "owner_pid": owner_pid,
        "owner_birth": owner_birth,
    }


def _assert_group_gone(popen: subprocess.Popen) -> None:
    for _ in range(50):
        if popen.poll() is not None:
            return
        time.sleep(0.1)
    pytest.fail("stub agent survived the sweep")


def test_boot_token_changes_shape_only(guard_file):
    token = _boot_token()
    assert isinstance(token, str) and token


def test_sweep_kills_orphan_whose_owner_died(guard_file):
    owner = _spawn_transient()
    owner_birth = _process_birth(owner.pid)
    _kill_transient(owner)
    stub = _spawn_stub()
    try:
        record_agent_group(stub.pid)
        entries = json.loads(open(guard_file).read())
        # Point the entry at an owner that then dies: the sweep must kill the
        # agent group even though the sweep runs from a different process.
        proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit"])
        proc.wait()
        entries[-1]["owner_pid"] = proc.pid
        entries[-1]["owner_birth"] = owner_birth
        json.dump(entries, open(guard_file, "w"))

        killed = sweep_orphaned_agents()
        assert stub.pid in killed
        _assert_group_gone(stub)
        assert json.loads(open(guard_file).read()) == []
    finally:
        if stub.poll() is None:
            os.killpg(stub.pid, signal.SIGKILL)


def test_sweep_spares_live_owner(guard_file):
    stub = _spawn_stub()
    try:
        record_agent_group(stub.pid)
        killed = sweep_orphaned_agents()
        assert killed == []
        assert stub.poll() is None
        # close()'s path: the entry is dropped so no sweep ever sees it again.
        forget_agent_group(stub.pid)
        assert json.loads(open(guard_file).read()) == []
    finally:
        if stub.poll() is None:
            os.killpg(stub.pid, signal.SIGKILL)


def test_sweep_never_kills_recycled_pid(guard_file):
    # A live process sits at the recorded pgid, but its birth differs from
    # the recorded one: a reused pid that must be left alone.
    stranger = _spawn_stub()
    try:
        stranger_birth = _process_birth(stranger.pid)
        owner = _spawn_transient()
        _kill_transient(owner)
        json.dump(
            [
                {
                    "pgid": stranger.pid,
                    "birth": "0:never-matched",
                    "owner_pid": owner.pid,
                    "owner_birth": stranger_birth,
                }
            ],
            open(guard_file, "w"),
        )
        assert sweep_orphaned_agents() == []
        assert stranger.poll() is None
    finally:
        if stranger.poll() is None:
            os.killpg(stranger.pid, signal.SIGKILL)


def test_process_birth_of_dead_pid_is_none(guard_file):
    proc = _spawn_transient()
    _kill_transient(proc)
    assert _process_birth(proc.pid) is None
