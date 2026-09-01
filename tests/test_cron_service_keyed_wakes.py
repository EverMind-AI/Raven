"""Keyed wakes: the cron store's replace-not-coexist one-shot partition.

The organ half of the wake-scheduling seam: ids under ``wake:<key>`` are a
partition the four verbs own -- scheduling the same key replaces the pending
wake, an event can pull it to now, and a wake marked ``fire_missed`` fires
late after downtime instead of vanishing, while plain reminders keep the
documented drop-plus-notice startup rule. The verbs never touch a job
outside the partition.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule


def _svc(tmp_path: Path, channels: set[str] | None = None) -> CronService:
    return CronService(tmp_path / "jobs.json", allowed_channels=channels)


def test_scheduling_the_same_key_replaces_the_pending_wake(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    now = int(time.time() * 1000)
    first = svc.schedule_wake("ops:c1", now + 60_000, "look at c1", channel="tui")
    second = svc.schedule_wake("ops:c1", now + 300_000, "look later", channel="tui")
    assert first.id == second.id == "wake:ops:c1"
    pending = svc.pending_wakes()
    assert len(pending) == 1, "one key, at most one pending wake"
    assert pending[0].payload.message == "look later"
    assert pending[0].schedule.at_ms == now + 300_000


def test_advance_pulls_the_wake_to_now_and_cancel_removes_it(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    now = int(time.time() * 1000)
    svc.schedule_wake("ops:c2", now + 600_000, "later", channel="tui")
    assert svc.advance_wake_to_now("ops:c2") is True
    job = svc.pending_wakes("ops:c2")[0]
    assert job.state.next_run_at_ms is not None
    assert job.state.next_run_at_ms <= int(time.time() * 1000)
    assert svc.cancel_wake("ops:c2") is True
    assert svc.pending_wakes() == []
    assert svc.advance_wake_to_now("ops:c2") is False
    assert svc.cancel_wake("ops:c2") is False


def test_pending_narrows_by_prefix_and_never_sees_plain_jobs(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    now = int(time.time() * 1000)
    svc.add_job("plain", CronSchedule(kind="at", at_ms=now + 60_000), "reminder", channel="tui")
    svc.schedule_wake("ops:a", now + 60_000, "a", channel="tui")
    svc.schedule_wake("mon:b", now + 90_000, "b", channel="tui")
    assert {j.id for j in svc.pending_wakes()} == {"wake:ops:a", "wake:mon:b"}
    assert [j.id for j in svc.pending_wakes("ops:")] == ["wake:ops:a"]
    assert len(svc.list_jobs()) == 3, "the plain job is untouched beside the partition"


def test_fire_missed_wake_fires_late_while_a_plain_reminder_drops(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now = int(time.time() * 1000)

    def _job(job_id: str, name: str, payload: dict) -> dict:
        return {
            "id": job_id,
            "name": name,
            "enabled": True,
            "schedule": {"kind": "at", "atMs": now - 60_000},
            "payload": payload,
            "state": {"nextRunAtMs": now - 60_000},
            "createdAtMs": now - 120_000,
            "updatedAtMs": now - 120_000,
            "deleteAfterRun": True,
        }

    jobs = [
        _job("wake:ops:c3", "wake ops:c3", {"message": "missed look", "channel": "tui", "fireMissed": True}),
        _job("plain1", "plain", {"message": "stretch", "channel": "tui"}),
    ]
    store_path.write_text(json.dumps({"version": 1, "jobs": jobs}), encoding="utf-8")

    svc = CronService(store_path, allowed_channels={"tui"})
    svc.list_jobs()
    svc._recompute_next_runs()

    assert [d.name for d in svc.last_startup_drops] == ["plain"], "plain reminders keep drop-plus-notice"
    # Read the recomputed in-memory store directly: a public read may reload
    # from disk, and the recompute is what start() runs before anything saves.
    kept = [j for j in svc._store.jobs if j.id == "wake:ops:c3"]
    assert len(kept) == 1
    assert kept[0].state.next_run_at_ms is not None
    assert kept[0].state.next_run_at_ms >= now - 1_000, "the missed wake is re-anchored to fire now"
