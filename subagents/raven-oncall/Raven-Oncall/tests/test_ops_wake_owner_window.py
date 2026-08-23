"""A wake belongs to one window, and waits when that window is gone.

Measured 2026-08-14 on two FEA campaigns whose windows had been closed: their
wakes carried ``owner=cron:<one-shot session>`` with the pid of a *different*
experiment's window, fired there, read that campaign's ledger and submitted
jobs. Deleting the jobs did not stop it, because every turn armed the next one.

Two rules come out of it, and this file pins both:

* the owner of a wake is the window watching the campaign, not the session that
  happened to schedule it -- those differ from round 1 on, because the turn that
  reschedules is itself a wake turn;
* a wake whose window is gone stays pending. It is not handed to whoever is
  open. A window takes it over by naming the campaign, which rebinds it.
"""

from __future__ import annotations

import os
from pathlib import Path

from raven.ops.window import bind_window, window_for_campaign


def _dead_pid() -> int:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def test_the_campaigns_window_is_found_by_name(tmp_path: Path):
    bind_window(tmp_path, "tui:a", "fea-limit-load", pid=os.getpid())
    assert window_for_campaign(tmp_path, "fea-limit-load") == ("tui:a", os.getpid())


def test_a_campaign_nobody_bound_has_no_window(tmp_path: Path):
    bind_window(tmp_path, "tui:a", "fea-limit-load", pid=os.getpid())
    assert window_for_campaign(tmp_path, "fea-contact") is None


def test_a_live_window_wins_over_a_closed_one(tmp_path: Path):
    """A campaign taken over by a second window stops pointing at the first."""
    bind_window(tmp_path, "tui:closed", "dambreak-legA", pid=_dead_pid())
    bind_window(tmp_path, "tui:open", "dambreak-legA", pid=os.getpid())
    assert window_for_campaign(tmp_path, "dambreak-legA") == ("tui:open", os.getpid())


def test_a_closed_window_is_still_the_owner_when_it_is_the_only_one(tmp_path: Path):
    """The wake must stay addressed to it: that is what makes it wait rather
    than fall to whoever is open, and what a takeover later replaces."""
    dead = _dead_pid()
    bind_window(tmp_path, "tui:closed", "dambreak-legA", pid=dead)
    assert window_for_campaign(tmp_path, "dambreak-legA") == ("tui:closed", dead)


def test_naming_a_campaign_moves_its_pending_wake_to_this_window(tmp_path, monkeypatch):
    """The takeover path: a wake left by a closed window runs in the window that
    names the campaign, without anyone rewriting the stored job."""
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.proactive_engine.schedulers.cron.types import (
        CronJob, CronJobState, CronPayload, CronSchedule,
    )

    monkeypatch.setattr("raven.config.paths.get_ops_home", lambda: tmp_path)
    closed = _dead_pid()
    job = CronJob(
        id="j3", name="ops:dambreak-legA:r1", enabled=True,
        schedule=CronSchedule(kind="at", at_ms=1),
        payload=CronPayload(kind="agent_turn", message="check",
                            campaign="dambreak-legA", owner="tui:closed",
                            owner_pid=closed),
        state=CronJobState(),
    )

    bind_window(tmp_path, "tui:closed", "dambreak-legA", pid=closed)
    assert CronService._owner_is_elsewhere(job, os.getpid()) is True

    bind_window(tmp_path, "tui:new", "dambreak-legA", pid=os.getpid())
    assert CronService._owner_is_elsewhere(job, os.getpid()) is False


def test_a_wake_for_a_campaign_nobody_named_still_waits(tmp_path, monkeypatch):
    """No binding at all falls back to the recorded owner, so a closed window's
    wake stays put rather than running wherever."""
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.proactive_engine.schedulers.cron.types import (
        CronJob, CronJobState, CronPayload, CronSchedule,
    )

    monkeypatch.setattr("raven.config.paths.get_ops_home", lambda: tmp_path)
    job = CronJob(
        id="j4", name="ops:fea-contact:r1", enabled=True,
        schedule=CronSchedule(kind="at", at_ms=1),
        payload=CronPayload(kind="agent_turn", message="check",
                            campaign="fea-contact", owner="tui:gone",
                            owner_pid=_dead_pid()),
        state=CronJobState(),
    )
    assert CronService._owner_is_elsewhere(job, os.getpid()) is True
