"""CronService claims only jobs whose channel is in its allowed_channels.

The gateway's allowed_channels is IM-only (no "tui"), so a TUI-originated cron
job is fired by the TUI process, never claimed/forwarded by the gateway — a
TUI-set reminder always delivers to the TUI instead of racing to an IM channel.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule


def _add_due_tui_job(svc: CronService) -> str:
    job = svc.add_job(
        name="tui reminder",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="drink water",
        deliver=True,
        channel="tui",
        to="default",
    )
    # Force it due: next_run in the past, persisted so _on_timer (which reloads
    # from disk) sees it.
    svc._store.jobs[0].state.next_run_at_ms = 1
    svc._save_store()
    return job.id


async def _fired_ids(allowed: set[str], store_path: Path) -> list[str]:
    fired: list[str] = []

    async def on_job(job) -> None:
        fired.append(job.id)

    svc = CronService(store_path, allowed_channels=allowed)
    svc.on_job = on_job
    await svc._on_timer()
    return fired


async def test_gateway_does_not_claim_tui_job(tmp_path: Path) -> None:
    store = tmp_path / "jobs.json"
    job_id = _add_due_tui_job(CronService(store, allowed_channels={"tui"}))

    # Gateway-style service (IM-only allow-list) must skip the "tui" job — its
    # channel is non-empty, so it is filtered by allowed_channels, not treated as
    # a legacy any-process job.
    fired = await _fired_ids({"weixin"}, store)
    assert job_id not in fired


async def test_owning_process_claims_its_tui_job(tmp_path: Path) -> None:
    store = tmp_path / "jobs.json"
    job_id = _add_due_tui_job(CronService(store, allowed_channels={"tui"}))

    fired = await _fired_ids({"tui"}, store)
    assert job_id in fired
