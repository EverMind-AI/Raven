"""A headless runner may adopt a wake whose owning window is dead; nobody else may.

The 2026-08-14 rule -- an owned wake is its owner's alone, open or closed --
protects runners that host conversations. raven.ops.wake_shell hosts none and
exists precisely to run the wakes of one-shot turns, whose owning process is
dead by design when the wake comes due (measured 2026-08-25: a CLI-subagent
campaign's wake sat in the store forever while the shell reloaded it every
30 seconds). ``adopt_orphans`` is that distinction, and this file pins its
three edges: default-off runners never adopt, an adopting runner takes only a
DEAD owner's wake, and a live owner keeps its wake from everyone.
"""

import os
import subprocess
import sys
import time

import pytest

from raven.proactive_engine.schedulers.cron.service import CronService, _pid_alive
from raven.proactive_engine.schedulers.cron.types import CronJob, CronJobState, CronPayload, CronSchedule


def _job(owner_pid, name="ops:t:r1"):
    return CronJob(
        id="j1",
        name=name,
        enabled=True,
        schedule=CronSchedule(kind="at", at_ms=1),
        payload=CronPayload(
            message="m", channel="cli", to="c", owner="cli:x", owner_pid=owner_pid, campaign=None
        ),
        state=CronJobState(next_run_at_ms=1),
    )


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    proc.wait()
    for _ in range(50):
        if not _pid_alive(pid):
            return pid
        time.sleep(0.05)
    pytest.skip("could not obtain a dead pid")


def _svc(tmp_path, adopt):
    return CronService(tmp_path / "jobs.json", allowed_channels=None, adopt_orphans=adopt)


def test_default_runner_never_adopts_a_dead_owners_wake(tmp_path):
    svc = _svc(tmp_path, adopt=False)
    ok, reason = svc._may_claim(_job(_dead_pid()), now=10)
    assert ok is False
    assert "owned by window" in (reason or "")


def test_adopting_runner_takes_a_dead_owners_wake(tmp_path):
    svc = _svc(tmp_path, adopt=True)
    ok, reason = svc._may_claim(_job(_dead_pid()), now=10)
    assert ok is True
    assert reason is None


def test_adopting_runner_leaves_a_live_owners_wake(tmp_path):
    svc = _svc(tmp_path, adopt=True)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ok, reason = svc._may_claim(_job(live.pid), now=10)
        assert ok is False
        assert "owned by window" in (reason or "")
    finally:
        live.kill()
        live.wait()


def test_own_wake_is_always_claimable(tmp_path):
    svc = _svc(tmp_path, adopt=False)
    ok, _ = svc._may_claim(_job(os.getpid()), now=10)
    assert ok is True


def test_pid_alive_edges():
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(_dead_pid()) is False
