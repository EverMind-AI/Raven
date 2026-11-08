"""CronService.add_job rejects non-runnable schedules at add time.

A schedule that maps to no next run (an `at` in the past, every_seconds <= 0,
an unparseable cron expr) used to be stored as a job that silently never fires
— a false success to every caller (cron tool / CLI / Sentinel). add_job now
raises ValueError so each caller surfaces it. This is the single service-layer
invariant covering all three callers.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule


@pytest.fixture
def svc(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "jobs.json")


def _add(svc: CronService, schedule: CronSchedule, message: str = "m"):
    return svc.add_job(name="t", schedule=schedule, message=message, channel="tui", to="direct")


def test_past_at_is_rejected(svc: CronService) -> None:
    past_ms = int(time.time() * 1000) - 60_000
    with pytest.raises(ValueError, match="at time is in the past"):
        _add(svc, CronSchedule(kind="at", at_ms=past_ms))
    assert svc.list_jobs() == []  # not stored as a dormant job


def test_non_positive_every_is_rejected(svc: CronService) -> None:
    with pytest.raises(ValueError, match="every_seconds must be positive"):
        _add(svc, CronSchedule(kind="every", every_ms=0))
    with pytest.raises(ValueError, match="every_seconds must be positive"):
        _add(svc, CronSchedule(kind="every", every_ms=-5_000))
    assert svc.list_jobs() == []


def test_invalid_cron_expr_is_rejected(svc: CronService) -> None:
    with pytest.raises(ValueError, match="invalid cron expression"):
        _add(svc, CronSchedule(kind="cron", expr="not a cron expr"))
    assert svc.list_jobs() == []


def test_runnable_schedules_still_created(tmp_path: Path) -> None:
    # A fresh service per kind avoids the cross-job dedup layers (which would
    # otherwise collapse three soon-firing jobs) — here we only assert each
    # runnable schedule is stored with a next run.
    future_ms = int(time.time() * 1000) + 60_000
    for schedule in (
        CronSchedule(kind="at", at_ms=future_ms),
        CronSchedule(kind="every", every_ms=60_000),
        CronSchedule(kind="cron", expr="0 9 * * *"),
    ):
        svc = CronService(tmp_path / f"{schedule.kind}.json")
        job = _add(svc, schedule)
        assert job.state.next_run_at_ms is not None  # runnable
        assert len(svc.list_jobs()) == 1


@pytest.mark.parametrize("explicit", [None, "UTC"])
def test_a_new_cron_stores_its_timezone_and_preserves_dst_rules(tmp_path, explicit):
    from zoneinfo import ZoneInfo

    now = datetime(2026, 3, 7, 15, tzinfo=timezone.utc)
    svc = CronService(tmp_path / "jobs.json", now_fn=lambda: now, default_timezone=lambda: "America/New_York")
    schedule = CronSchedule(kind="cron", expr="0 9 * * *", tz=explicit)
    job = _add(svc, schedule)
    zone = explicit or "America/New_York"
    expected = datetime(2026, 3, 8, 9, tzinfo=ZoneInfo(zone))
    assert job.schedule.tz == zone
    assert schedule.tz == explicit
    assert job.state.next_run_at_ms == int(expected.timestamp() * 1000)
    assert CronService(svc.store_path).list_jobs()[0].schedule.tz == zone


@pytest.mark.parametrize("old_zone", [None, "UTC"])
@pytest.mark.parametrize("by_topic", [False, True, "missing-id"])
def test_an_implicit_timezone_edit_keeps_the_previous_job_zone(tmp_path, old_zone, by_topic):
    path = tmp_path / "jobs.json"
    old = CronService(path).add_job(
        "old", CronSchedule(kind="cron", expr="0 9 * * *", tz=old_zone), "old", topic_tag="guide"
    )

    def unused_default():
        raise AssertionError("an edit must not read the new-job default")

    svc = CronService(path, default_timezone=unused_default)
    updated = svc.add_job(
        "edited",
        CronSchedule(kind="cron", expr="0 10 * * *"),
        "edited",
        **(
            {"topic_tag": "guide", "job_id": "gone" if by_topic == "missing-id" else None}
            if by_topic
            else {"job_id": old.id}
        ),
    )
    assert updated.id == old.id
    assert updated.schedule.tz == old_zone
    assert updated.schedule.expr == "0 10 * * *"


def test_an_invalid_default_is_rejected_without_mutating_jobs(tmp_path):
    svc = CronService(tmp_path / "jobs.json", default_timezone=lambda: "Not/AZone")
    with pytest.raises(ValueError, match="unknown timezone"):
        _add(svc, CronSchedule(kind="cron", expr="0 9 * * *"))
    assert svc.list_jobs() == []
    assert _add(svc, CronSchedule(kind="cron", expr="0 9 * * *", tz="UTC")).schedule.tz == "UTC"


@pytest.mark.parametrize("kind", ["at", "every"])
def test_non_cron_schedules_do_not_read_the_default(tmp_path, kind):
    def unused_default():
        raise AssertionError("only cron expressions use the default")

    svc = CronService(tmp_path / "jobs.json", default_timezone=unused_default)
    schedule = CronSchedule(kind=kind, at_ms=int(time.time() * 1000) + 60000, every_ms=60000)
    assert _add(svc, schedule).schedule == schedule


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="moving the process zone needs time.tzset")
@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2027, 3, 13, 15, tzinfo=timezone.utc), datetime(2027, 3, 14, 13, tzinfo=timezone.utc)),
        (datetime(2027, 11, 6, 14, tzinfo=timezone.utc), datetime(2027, 11, 7, 14, tzinfo=timezone.utc)),
    ],
)
def test_cron_without_tz_follows_local_dst(monkeypatch, now, expected) -> None:
    """A 09:00 cron with no tz stays at 09:00 local time across a DST change."""
    from raven.proactive_engine.schedulers.cron.service import _compute_next_run

    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        now_ms = int(now.timestamp() * 1000)
        next_ms = _compute_next_run(CronSchedule(kind="cron", expr="0 9 * * *"), now_ms)
        assert next_ms == int(expected.timestamp() * 1000)
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="moving the process zone needs time.tzset")
@pytest.mark.parametrize("minute, second", [(5, 0), (15, 30)])
@pytest.mark.parametrize("expr", ["30 1 * * *", "*/10 * * * *", "*/5 * * * *", "* * * * *", "* * * * * *"])
def test_cron_without_tz_is_never_scheduled_in_the_past_on_dst_fall_back(monkeypatch, minute, second, expr) -> None:
    """In the repeated hour of a fall-back, the next run must be after `now`."""
    from raven.proactive_engine.schedulers.cron.service import _compute_next_run

    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        now_ms = int(datetime(2027, 11, 7, 6, minute, second, tzinfo=timezone.utc).timestamp() * 1000)
        next_ms = _compute_next_run(CronSchedule(kind="cron", expr=expr), now_ms)
        expected = (
            datetime(2027, 11, 8, 6, 30, tzinfo=timezone.utc)
            if expr == "30 1 * * *"
            else datetime(2027, 11, 7, 7, tzinfo=timezone.utc)
        )
        assert next_ms == int(expected.timestamp() * 1000)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_cron_stops_when_no_future_slot_is_found(monkeypatch) -> None:
    from unittest.mock import Mock

    from raven.proactive_engine.schedulers.cron.service import _compute_next_run

    past = datetime(2027, 11, 7, 5, tzinfo=timezone.utc)
    iterator = Mock()
    iterator.get_next.return_value = past
    monkeypatch.setattr("croniter.croniter", Mock(return_value=iterator))
    now_ms = int(datetime(2027, 11, 7, 6, tzinfo=timezone.utc).timestamp() * 1000)

    assert _compute_next_run(CronSchedule(kind="cron", expr="* * * * * *"), now_ms) is None
    assert iterator.get_next.call_count == 10_000
