"""CronService.add_job rejects non-runnable schedules at add time.

A schedule that maps to no next run (an `at` in the past, every_seconds <= 0,
an unparseable cron expr) used to be stored as a job that silently never fires
— a false success to every caller (cron tool / CLI / Sentinel). add_job now
raises ValueError so each caller surfaces it. This is the single service-layer
invariant covering all three callers.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule


@pytest.fixture
def svc(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "jobs.json")


def _add(svc: CronService, schedule: CronSchedule, message: str = "m"):
    return svc.add_job(
        name="t", schedule=schedule, message=message, deliver=True, channel="cli", to="direct"
    )


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
