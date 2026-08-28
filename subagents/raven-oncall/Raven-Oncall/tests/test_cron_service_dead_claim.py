"""A claim held by a process that is gone is not a claim.

``_claim_is_live`` read the claiming pid and then never asked whether it was
still running: any pid other than this process's counted as a live peer for the
whole 30-minute TTL. So closing the window that held a claim froze that campaign
for half an hour -- the watch has nothing to do but wait, and nothing on screen
says why.

That matters most for the case it was asked about on 2026-08-13: when the window
watching an experiment is closed, another window should be able to pick the wake
up, because the campaign's state is on disk and a wake turn is a cold start that
reads it. Being able to in principle but only after thirty minutes is not being
able to.

Pid reuse is the one way this can be wrong, and it errs the safe way: a reused
pid reads as alive, which keeps the old behaviour of waiting out the TTL rather
than running a turn twice.
"""

from __future__ import annotations

import os

import pytest

from raven.proactive_engine.schedulers.cron.service import CronService, _CLAIM_TTL_MS
from raven.proactive_engine.schedulers.cron.types import CronJob, CronJobState, CronPayload, CronSchedule


def _job(pid: int | None, claimed_at_ms: int | None) -> CronJob:
    return CronJob(
        id="j1",
        name="ops:cfd-dambreak:r1",
        enabled=True,
        schedule=CronSchedule(kind="at", at_ms=1),
        payload=CronPayload(message="check"),
        state=CronJobState(claimed_by_pid=pid, claimed_at_ms=claimed_at_ms),
    )


NOW = 10 * _CLAIM_TTL_MS


def _dead_pid() -> int:
    """A pid that is certainly not running: fork a child and reap it."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def test_a_fresh_claim_by_a_live_process_is_respected():
    """The whole point of claiming: two services must not run one job twice."""
    live = os.getppid()          # our parent is running by construction
    assert CronService._claim_is_live(_job(live, NOW - 1000), NOW) is True


def test_a_claim_by_a_dead_process_is_not_live():
    assert CronService._claim_is_live(_job(_dead_pid(), NOW - 1000), NOW) is False


def test_an_expired_claim_is_not_live_even_from_a_live_process():
    live = os.getppid()
    assert CronService._claim_is_live(_job(live, NOW - _CLAIM_TTL_MS - 1), NOW) is False


def test_our_own_claim_is_not_a_peer_s():
    assert CronService._claim_is_live(_job(os.getpid(), NOW - 1000), NOW) is False


def test_an_unclaimed_job_is_not_live():
    assert CronService._claim_is_live(_job(None, None), NOW) is False
    assert CronService._claim_is_live(_job(1234, None), NOW) is False


def _owned_job(owner_pid: int | None) -> CronJob:
    return CronJob(
        id="j2",
        name="ops:fea-contact:r1",
        enabled=True,
        schedule=CronSchedule(kind="at", at_ms=1),
        payload=CronPayload(message="check", owner="tui:a",
                            owner_pid=owner_pid),
        state=CronJobState(),
    )


def test_a_wake_whose_window_is_closed_is_not_taken_by_another_window():
    """2026-08-14: two closed windows' wakes ran inside a third window, read a
    different experiment's ledger and submitted jobs. A wake with an owner is
    that owner's alone; when the owner is gone it waits to be claimed by name."""
    job = _owned_job(_dead_pid())
    assert CronService._owning_pid(job, os.getpid()) is not None


def test_a_window_runs_its_own_wake():
    assert CronService._owning_pid(_owned_job(os.getpid()), os.getpid()) is None


def test_a_wake_written_before_the_owner_field_still_runs():
    """Jobs from before ownership existed carry no owner and must not stall."""
    assert CronService._owning_pid(_owned_job(None), os.getpid()) is None
