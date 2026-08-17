"""CronService claims only jobs whose channel is in its allowed_channels.

The gateway's allowed_channels is IM-only (no "tui"), so a TUI-originated cron
job is fired by the TUI process, never claimed/forwarded by the gateway — a
TUI-set reminder always delivers to the TUI instead of racing to an IM channel.
"""

from __future__ import annotations

import os
from pathlib import Path

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


async def test_on_job_scheduling_a_follow_up_does_not_kill_its_own_tick(tmp_path: Path) -> None:
    """A job's callback that schedules a follow-up (the Ops campaign wake path:
    ops tools call add_job directly, dedup=False) must not cancel the tick that
    is currently awaiting it -- that killed the callback mid-await, silently
    skipping its delivery fan-out and post-run cleanup. The callback must run to
    completion and the follow-up wake must still fire."""
    import asyncio

    completed: list[str] = []
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})

    async def on_job(job) -> str:
        if job.name == "ops:c:r1":
            now = svc._now_ms()
            svc.add_job(
                name="ops:c:r2",
                schedule=CronSchedule(kind="at", at_ms=now + 150),
                message="wake r2",
                deliver=True,
                channel="tui",
                to="default",
                delete_after_run=True,
                dedup=False,
            )
            # yield once: the old code's cancel lands here and kills us
            await asyncio.sleep(0)
        completed.append(job.name)  # only reached if the callback survived
        return "ok"

    svc.on_job = on_job
    await svc.start()
    now = svc._now_ms()
    svc.add_job(
        name="ops:c:r1",
        schedule=CronSchedule(kind="at", at_ms=now + 50),
        message="wake r1",
        deliver=True,
        channel="tui",
        to="default",
        delete_after_run=True,
        dedup=False,
    )
    await asyncio.sleep(1.0)
    svc.stop()

    assert "ops:c:r1" in completed  # callback ran to completion, not cancelled
    assert "ops:c:r2" in completed  # the follow-up wake it scheduled also fired


async def test_advance_job_to_now_fires_the_wake_early(tmp_path: Path) -> None:
    """The ops event watcher pulls a far-future wake forward when the job
    finishes early -- the agent wakes on the event, not the ETA guess."""
    import asyncio

    fired: list[str] = []

    async def on_job(job) -> str:
        fired.append(job.name)
        return "ok"

    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    svc.on_job = on_job
    await svc.start()
    now = svc._now_ms()
    job = svc.add_job(
        name="ops:c:r1",
        schedule=CronSchedule(kind="at", at_ms=now + 3_600_000),  # an hour out
        message="wake",
        deliver=True,
        channel="tui",
        to="default",
        delete_after_run=True,
        dedup=False,
    )
    await asyncio.sleep(0.1)
    assert fired == []  # nowhere near due

    assert svc.advance_job_to_now(job.id) is True
    await asyncio.sleep(0.8)
    svc.stop()

    assert fired == ["ops:c:r1"]  # fired now instead of in an hour
    assert svc.advance_job_to_now("nonexistent") is False


def _past_due_claimed_job(store: Path, *, claim_age_ms: int | None) -> str:
    """Seed a store with one past-due 'at' job, optionally held by a peer claim."""
    svc = CronService(store, allowed_channels={"tui"})
    now = svc._now_ms()
    job = svc.add_job(
        name="ops:campaign:recheck",
        schedule=CronSchedule(kind="at", at_ms=now + 60_000),
        message="wake",
        deliver=True,
        channel="tui",
        to="default",
        delete_after_run=True,
        dedup=False,
    )
    seeded = svc._store.jobs[0]
    seeded.schedule.at_ms = now - 9_000
    seeded.state.next_run_at_ms = now - 9_000
    if claim_age_ms is not None:
        seeded.state.claimed_by_pid = os.getpid() + 1
        seeded.state.claimed_at_ms = now - claim_age_ms
    svc._save_store()
    return job.id


async def test_startup_recompute_keeps_past_due_job_held_by_live_peer(tmp_path: Path) -> None:
    """Startup recompute must not delete a past-due job a live peer is executing.

    The recompute runs on every process start, so without this any incidental
    raven invocation (a CLI command, a test that spawns a subprocess) silently
    deletes the in-flight wake of a long-running on-call process.
    """
    store = tmp_path / "jobs.json"
    job_id = _past_due_claimed_job(store, claim_age_ms=0)

    peer = CronService(store, allowed_channels={"tui"})
    await peer.start()
    peer.stop()

    assert [j.id for j in peer._store.jobs] == [job_id]
    assert peer._store.jobs[0].state.next_run_at_ms == peer._store.jobs[0].schedule.at_ms


async def test_startup_recompute_drops_past_due_job_whose_claim_expired(tmp_path: Path) -> None:
    """A claim older than the TTL no longer protects the job -- a crashed owner
    delays the drop by one TTL rather than pinning the job forever."""
    store = tmp_path / "jobs.json"
    _past_due_claimed_job(store, claim_age_ms=31 * 60 * 1000)

    peer = CronService(store, allowed_channels={"tui"})
    await peer.start()
    peer.stop()

    assert peer._store.jobs == []


async def test_startup_recompute_still_drops_unclaimed_past_due_job(tmp_path: Path) -> None:
    """Unclaimed past-due one-shots keep the documented iOS-like drop behavior."""
    store = tmp_path / "jobs.json"
    _past_due_claimed_job(store, claim_age_ms=None)

    peer = CronService(store, allowed_channels={"tui"})
    await peer.start()
    peer.stop()

    assert peer._store.jobs == []
