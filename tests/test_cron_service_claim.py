"""CronService claims only jobs whose channel is in its allowed_channels.

The gateway's allowed_channels is IM-only (no "tui"), so a TUI-originated cron
job is fired by the TUI process, never claimed/forwarded by the gateway — a
TUI-set reminder always delivers to the TUI instead of racing to an IM channel.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule


def _add_due_tui_job(svc: CronService) -> str:
    job = svc.add_job(
        name="tui reminder",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="drink water",
        channel="tui",
        to="default",
    )
    # Force it due: next_run in the past, persisted so _process_due (which
    # reloads from disk) sees it.
    svc._store.jobs[0].state.next_run_at_ms = 1
    svc._save_store()
    return job.id


async def _fired_ids(allowed: set[str], store_path: Path) -> list[str]:
    fired: list[str] = []

    async def on_job(job) -> None:
        fired.append(job.id)

    svc = CronService(store_path, allowed_channels=allowed)
    svc.on_job = on_job
    await svc._process_due()
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


async def test_legacy_channel_none_job_claimable_by_any_partition(tmp_path: Path) -> None:
    store = tmp_path / "jobs.json"
    svc = CronService(store, allowed_channels={"weixin"})
    job = svc.add_job(
        name="legacy",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="pre-attribution job",
        channel=None,
        to=None,
    )
    svc._store.jobs[0].state.next_run_at_ms = 1
    svc._save_store()

    fired = await _fired_ids({"weixin"}, store)
    assert job.id in fired


async def test_a_channel_added_to_the_partition_makes_its_job_claimable(tmp_path: Path) -> None:
    """``allowed_channels`` is read live, and a job refused once is not written off.

    The gateway mutates this very set when a channel is enabled or disabled while it
    runs, so a reminder addressed to that channel has to become claimable without a
    restart -- and the per-job skip log must not double as a permanent verdict.
    """
    store = tmp_path / "jobs.json"
    job_id = _add_due_tui_job(CronService(store, allowed_channels={"tui"}))

    fired: list[str] = []

    async def on_job(job) -> None:
        fired.append(job.id)

    svc = CronService(store, allowed_channels={"weixin"})
    svc.on_job = on_job
    await svc._process_due()
    assert fired == []

    svc.allowed_channels.add("tui")
    await svc._process_due()
    assert fired == [job_id]


async def test_admitting_a_channel_wakes_the_sleeping_loop(tmp_path: Path) -> None:
    """The loop sleeps up to the 30 s poll cap while nothing claimable is due, and a
    job it just excluded as foreign does not count. Mutating the set alone left a
    reminder that was already due when its channel hot-started asleep for the rest
    of that cap; ``admit_channel`` wakes the loop, so it fires within a beat."""
    store = tmp_path / "jobs.json"
    job_id = _add_due_tui_job(CronService(store, allowed_channels={"tui"}))

    fired: asyncio.Queue[str] = asyncio.Queue()

    async def on_job(job) -> None:
        await fired.put(job.id)

    svc = CronService(store, allowed_channels={"weixin"})
    svc.on_job = on_job
    await svc.start()
    try:
        await asyncio.sleep(0.2)
        assert fired.empty(), "foreign while weixin is the whole partition"
        svc.admit_channel("tui")
        assert await asyncio.wait_for(fired.get(), timeout=2.0) == job_id
    finally:
        svc.stop()


async def test_retiring_a_channel_drops_it_from_the_partition_and_wakes_the_loop(tmp_path: Path) -> None:
    """The mirror of admission: a channel stopped from the page leaves the partition
    at once, and the loop is nudged so its next wake is computed without that
    channel's jobs."""
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui", "weixin"})
    svc._wake_event.clear()

    svc.retire_channel("tui")

    assert svc.allowed_channels == {"weixin"}
    assert svc._wake_event.is_set()


async def test_admit_and_retire_do_nothing_without_a_partition(tmp_path: Path) -> None:
    """``allowed_channels is None`` is the CLI's service, which claims everything;
    there is no set to mutate and no reason to wake it."""
    svc = CronService(tmp_path / "jobs.json", allowed_channels=None)
    svc._wake_event.clear()

    svc.admit_channel("weixin")
    svc.retire_channel("weixin")

    assert svc.allowed_channels is None
    assert not svc._wake_event.is_set()


async def test_foreign_channel_skip_logs_once_per_job(tmp_path: Path) -> None:
    from loguru import logger

    store = tmp_path / "jobs.json"
    job_id = _add_due_tui_job(CronService(store, allowed_channels={"tui"}))

    fired: list[str] = []

    async def on_job(job) -> None:
        fired.append(job.id)

    svc = CronService(store, allowed_channels={"weixin"})
    svc.on_job = on_job
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        await svc._process_due()
        await svc._process_due()
    finally:
        logger.remove(sink_id)

    assert fired == []
    skips = [ln for ln in lines if "not claiming job" in ln and job_id in ln]
    assert len(skips) == 1, f"expected exactly one skip log for {job_id}, got {skips}"
    assert "partition" in skips[0]
