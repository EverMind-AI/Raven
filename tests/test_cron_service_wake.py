"""Wake-loop behavior of CronService.

The service runs one persistent loop (process due jobs, then wait for the
next claimable run or a mutation signal). Job mutations set an event instead
of cancelling a timer task, so an in-flight execution is never cancelled by
add/remove/enable/run — the double-fire + claim-leak failure mode of the old
cancel-and-rearm timer.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from raven.proactive_engine.schedulers.cron import service as cron_service
from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule


def _now_ms() -> int:
    return int(time.time() * 1000)


async def test_mutation_during_inflight_run_fires_once_and_leaves_no_claim(tmp_path: Path) -> None:
    """Regression for the double-fire bug: add_job while a one-shot is
    executing must not cancel the in-flight run. The one-shot completes,
    its writeback lands (job deleted, no claim residue), and it never
    fires a second time."""
    store_path = tmp_path / "jobs.json"
    events: list[tuple[str, str]] = []
    started = asyncio.Event()

    async def on_job(job) -> None:
        events.append(("start", job.id))
        started.set()
        await asyncio.sleep(0.3)
        events.append(("end", job.id))

    svc = CronService(store_path, on_job=on_job)
    job = svc.add_job(
        name="oneshot",
        schedule=CronSchedule(kind="at", at_ms=_now_ms() + 150),
        message="fire exactly once",
        channel="tui",
        to="direct",
        delete_after_run=True,
    )

    await svc.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=5.0)
        # Mutate while the one-shot is mid-execution. Different `to` and a
        # far schedule so no dedup layer swallows the add.
        svc.add_job(
            name="unrelated",
            schedule=CronSchedule(kind="every", every_ms=3600_000),
            message="a different far-future job",
            channel="tui",
            to="elsewhere",
        )
        # Let the run finish, the writeback land, and the loop take at
        # least one more wake (the add signalled it) that could re-fire.
        await asyncio.sleep(0.8)
    finally:
        svc.stop()

    assert events == [("start", job.id), ("end", job.id)], (
        f"one-shot must execute exactly once and not be cancelled mid-run, got {events}"
    )
    data = json.loads(store_path.read_text(encoding="utf-8"))
    ids = [j["id"] for j in data["jobs"]]
    assert job.id not in ids, "delete_after_run one-shot must be gone after the run"
    for j in data["jobs"]:
        assert j["state"]["claimedByPid"] is None, f"claim residue on {j['id']}: {j['state']}"


async def test_failing_tick_does_not_kill_loop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cron_service, "_ERROR_BACKOFF_S", 0.05)
    store_path = tmp_path / "jobs.json"
    fired = asyncio.Event()

    async def on_job(job) -> None:
        fired.set()

    svc = CronService(store_path, on_job=on_job)
    svc.add_job(
        name="soon",
        schedule=CronSchedule(kind="at", at_ms=_now_ms() + 1_000),
        message="fires after a bad tick",
        channel="tui",
        to="direct",
        delete_after_run=True,
    )

    real_process_due = svc._process_due
    calls = {"n": 0}

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        await real_process_due()

    monkeypatch.setattr(svc, "_process_due", flaky)

    await svc.start()
    try:
        await asyncio.wait_for(fired.wait(), timeout=5.0)
    finally:
        svc.stop()
    assert calls["n"] >= 2, "loop must keep ticking after a failed tick"


def test_due_but_unclaimable_job_does_not_zero_wake_delay(tmp_path: Path) -> None:
    """Busy-loop guard: a due job owned by a foreign partition must not
    drive this runner's wake delay to 0 — it waits at the poll cap."""
    store_path = tmp_path / "jobs.json"
    owner = CronService(store_path, allowed_channels={"tui"})
    owner.add_job(
        name="tui reminder",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="drink water",
        channel="tui",
        to="direct",
    )
    owner._store.jobs[0].state.next_run_at_ms = 1
    owner._save_store()

    gw = CronService(store_path, allowed_channels={"weixin"})
    assert gw._compute_wake_delay() == cron_service._MAX_WAKE_INTERVAL_S

    assert owner._compute_wake_delay() == 0.0


async def test_add_job_wakes_idle_loop(tmp_path: Path) -> None:
    """A mutation must wake the loop promptly — a due-soon job added while
    the loop is parked on the 30s poll fires well under the poll cap."""
    store_path = tmp_path / "jobs.json"
    fired = asyncio.Event()

    async def on_job(job) -> None:
        fired.set()

    svc = CronService(store_path, on_job=on_job)
    await svc.start()
    try:
        await asyncio.sleep(0.1)
        svc.add_job(
            name="soon",
            schedule=CronSchedule(kind="at", at_ms=_now_ms() + 200),
            message="added while idle",
            channel="tui",
            to="direct",
            delete_after_run=True,
        )
        await asyncio.wait_for(fired.wait(), timeout=3.0)
    finally:
        svc.stop()


async def test_manual_run_uses_the_shared_writeback(tmp_path: Path) -> None:
    """run_job rides the same execute/writeback path as the wake loop: the
    claim is cleared, the recurring next_run advances, and last_status lands
    — a test-fire cannot diverge from real scheduling."""
    store_path = tmp_path / "jobs.json"
    fired: list[str] = []

    async def on_job(job) -> None:
        fired.append(job.id)

    svc = CronService(store_path, on_job=on_job)
    job = svc.add_job(
        name="recurring",
        schedule=CronSchedule(kind="every", every_ms=3600_000),
        message="manual fire",
        channel="tui",
        to="direct",
    )

    before = _now_ms()
    assert await svc.run_job(job.id) is True

    assert fired == [job.id]
    data = json.loads(store_path.read_text(encoding="utf-8"))
    (j,) = data["jobs"]
    assert j["state"]["claimedByPid"] is None
    assert j["state"]["lastStatus"] == "ok"
    assert j["state"]["lastRunAtMs"] >= before
    assert j["state"]["nextRunAtMs"] > _now_ms()


async def test_a_failed_one_shot_stays_on_the_table_disabled(tmp_path: Path) -> None:
    """A one-shot whose turn failed (a reload cut it, the provider was down) is
    not deleted: it stays, disabled, carrying the error, so the record that the
    reminder never reached anyone survives the run."""
    store_path = tmp_path / "jobs.json"

    async def on_job(job) -> None:
        raise RuntimeError("turn was cancelled or failed before it completed")

    svc = CronService(store_path, on_job=on_job)
    job = svc.add_job(
        name="oneshot",
        schedule=CronSchedule(kind="at", at_ms=_now_ms() + 60_000),
        message="fire once",
        channel="tui",
        to="direct",
        delete_after_run=True,
    )
    await svc._execute_job(job)

    kept = [j for j in svc._store.jobs if j.id == job.id]
    assert len(kept) == 1
    assert kept[0].enabled is False
    assert kept[0].state.last_status == "error"
    assert "cancelled" in (kept[0].state.last_error or "")


async def test_a_wake_re_armed_during_its_own_run_survives_the_writeback(tmp_path: Path) -> None:
    """The ordinary on-call round: the wake fires, the turn it runs calls
    ``schedule_wake`` under the same key -- the same job id -- for the next
    look, and the turn ends. The post-run writeback then reloaded the store and
    removed that id as the finished one-shot, so the next look never came (and
    an ACP prompt held open on it waited forever; reviewed 2026-09-10). A wake
    whose schedule the run never had is a new wake: its claim is cleared, its
    schedule stands, and it is not deleted."""
    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path)
    first = svc.schedule_wake("ops:c1", _now_ms() + 1_000, "look", channel="acp", to="w1")
    svc._store.jobs[0].state.next_run_at_ms = 1  # due now; the floor kept at_ms a second out
    svc._save_store()
    fired_at = first.schedule.at_ms
    next_look = _now_ms() + 600_000

    async def on_job(job) -> None:
        assert job.id == first.id
        replaced = svc.schedule_wake("ops:c1", next_look, "look again", channel="acp", to="w1")
        assert replaced.id == first.id, "one key, one id: the re-arm replaces in place"

    svc.on_job = on_job
    await svc._process_due()

    pending = svc.pending_wakes("ops:c1")
    assert [j.id for j in pending] == [first.id], "the re-armed wake is still on the table"
    job = pending[0]
    assert job.schedule.at_ms == next_look != fired_at and job.state.next_run_at_ms == next_look
    assert job.enabled and job.state.claimed_by_pid is None and job.state.claimed_at_ms is None
    assert job.state.last_status == "ok", "the run that re-armed it is still on its record"


async def test_a_wake_that_did_not_re_arm_is_still_deleted_after_its_run(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path)
    svc.schedule_wake("ops:c2", _now_ms() + 1_000, "look once", channel="acp", to="w1")
    svc._store.jobs[0].state.next_run_at_ms = 1
    svc._save_store()
    fired: list[str] = []

    async def on_job(job) -> None:
        fired.append(job.id)

    svc.on_job = on_job
    await svc._process_due()

    assert fired == ["wake:ops:c2"] and svc.pending_wakes("ops:c2") == []
