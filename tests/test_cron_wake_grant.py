"""The namespaced wake grant: one plugin's keys cannot reach another's.

The grant half of the wake-scheduling seam: the loop mints one
NamespacedWakeScheduler per contributed tool, so the namespace comes from the
build-time stamp rather than the plugin's own claim, and the paper's face is
what the wrapper satisfies.
"""

from __future__ import annotations

import time
from pathlib import Path

from raven.contracts.scheduling import WakeScheduler
from raven.proactive_engine.schedulers.cron.grant import NamespacedWakeScheduler
from raven.proactive_engine.schedulers.cron.service import CronService


def _svc(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "jobs.json", allowed_channels=None)


def test_the_grant_satisfies_the_paper_and_prefixes_every_key(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    grant = NamespacedWakeScheduler(svc, "plug-a")
    assert isinstance(grant, WakeScheduler)

    now = int(time.time() * 1000)
    job = grant.schedule_wake("c1", now + 60_000, "look", channel="tui")
    assert job.id == "wake:plug-a:c1", "the namespace is baked in before the store sees the key"


def test_one_namespace_cannot_see_or_move_anothers_wakes(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    a = NamespacedWakeScheduler(svc, "plug-a")
    b = NamespacedWakeScheduler(svc, "plug-b")
    now = int(time.time() * 1000)
    a.schedule_wake("c1", now + 60_000, "a-look", channel="tui")
    b.schedule_wake("c1", now + 90_000, "b-look", channel="tui")

    assert [j.payload.message for j in a.pending_wakes()] == ["a-look"]
    assert [j.payload.message for j in b.pending_wakes()] == ["b-look"]
    assert b.cancel_wake("nope") is False
    assert a.advance_wake_to_now("c1") is True
    assert [j.payload.message for j in b.pending_wakes()] == ["b-look"], "b's wake did not move"
    assert a.cancel_wake("c1") is True
    assert svc.pending_wakes("plug-b:") and not svc.pending_wakes("plug-a:")
