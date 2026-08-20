"""Missed foreign one-shot observation in CronService.

``list_missed_foreign_oneshots`` is the read-only helper behind the
gateway's missed-reminder sink: one-shot 'at' jobs bound to another
partition (tui / cli) whose fire time is past the grace window — their
owning session was closed before they could fire. Partition rules forbid
mutating foreign jobs, so observing is all this does.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from raven.proactive_engine.schedulers.cron.service import CronService

_GRACE_MS = 5 * 60 * 1000


def _at_job(
    job_id: str,
    at_ms: int,
    *,
    channel: str | None = "tui",
    kind: str = "at",
    enabled: bool = True,
    claimed_by_pid: int | None = None,
    claimed_at_ms: int | None = None,
) -> dict:
    schedule = {"kind": kind, "atMs": at_ms} if kind == "at" else {"kind": kind, "everyMs": 60_000}
    return {
        "id": job_id,
        "name": f"reminder {job_id}",
        "enabled": enabled,
        "schedule": schedule,
        "payload": {"message": "stretch", "channel": channel, "to": "default"},
        "state": {
            "nextRunAtMs": at_ms,
            "claimedByPid": claimed_by_pid,
            "claimedAtMs": claimed_at_ms,
        },
        "createdAtMs": at_ms - 60_000,
        "updatedAtMs": at_ms - 60_000,
    }


def _write_store(store_path: Path, jobs: list[dict]) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({"version": 1, "jobs": jobs}), encoding="utf-8")


def _gateway_svc(store_path: Path) -> CronService:
    return CronService(store_path, allowed_channels={"weixin"})


def test_missed_requires_grace_window(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now = int(time.time() * 1000)
    _write_store(
        store_path,
        [
            _at_job("fresh", now - 60_000),  # 1 min late: session may just be slow
            _at_job("stale", now - _GRACE_MS - 60_000),  # 6 min late: missed
        ],
    )

    missed = _gateway_svc(store_path).list_missed_foreign_oneshots()
    assert [j.id for j in missed] == ["stale"]


def test_missed_is_foreign_only(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now = int(time.time() * 1000)
    past = now - _GRACE_MS - 60_000
    _write_store(
        store_path,
        [
            _at_job("own", past, channel="weixin"),
            _at_job("legacy", past, channel=None),
            _at_job("foreign", past, channel="tui"),
        ],
    )

    missed = _gateway_svc(store_path).list_missed_foreign_oneshots()
    assert [j.id for j in missed] == ["foreign"], (
        "own-partition and legacy (channel=None) jobs are never missed-foreign"
    )


def test_missed_is_oneshot_only(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now = int(time.time() * 1000)
    past = now - _GRACE_MS - 60_000
    _write_store(
        store_path,
        [
            _at_job("recurring", past, kind="every"),
            _at_job("oneshot", past),
        ],
    )

    missed = _gateway_svc(store_path).list_missed_foreign_oneshots()
    assert [j.id for j in missed] == ["oneshot"], "recurring jobs self-heal on their next tick; only 'at' can strand"


def test_missed_excludes_disabled_jobs(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now = int(time.time() * 1000)
    _write_store(store_path, [_at_job("done", now - _GRACE_MS - 60_000, enabled=False)])

    assert _gateway_svc(store_path).list_missed_foreign_oneshots() == []


def test_missed_excludes_freshly_claimed_jobs(tmp_path: Path) -> None:
    """A live peer claim means the owning process is delivering right now;
    a stale claim (crashed peer) still counts as missed."""
    store_path = tmp_path / "jobs.json"
    now = int(time.time() * 1000)
    past = now - _GRACE_MS - 60_000
    _write_store(
        store_path,
        [
            _at_job("inflight", past, claimed_by_pid=os.getpid() + 1, claimed_at_ms=now - 1_000),
            _at_job("crashed", past, claimed_by_pid=os.getpid() + 1, claimed_at_ms=now - 31 * 60 * 1000),
        ],
    )

    missed = _gateway_svc(store_path).list_missed_foreign_oneshots()
    assert [j.id for j in missed] == ["crashed"]


def test_missed_custom_grace_window(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now = int(time.time() * 1000)
    _write_store(store_path, [_at_job("j1", now - 90_000)])
    svc = _gateway_svc(store_path)

    assert [j.id for j in svc.list_missed_foreign_oneshots(grace_ms=60_000)] == ["j1"]
    assert svc.list_missed_foreign_oneshots(grace_ms=120_000) == []


def test_missed_listing_is_read_only(tmp_path: Path) -> None:
    """Partition rules forbid touching foreign jobs — the helper must not
    write the store (no drop, no recompute, no claim)."""
    store_path = tmp_path / "jobs.json"
    now = int(time.time() * 1000)
    _write_store(store_path, [_at_job("ro1", now - _GRACE_MS - 60_000)])
    before = store_path.read_text(encoding="utf-8")

    missed = _gateway_svc(store_path).list_missed_foreign_oneshots()

    assert [j.id for j in missed] == ["ro1"]
    assert store_path.read_text(encoding="utf-8") == before


async def test_start_invokes_missed_observer_and_leaves_job_intact(tmp_path: Path) -> None:
    """start() runs an observation pass; the foreign job survives the
    startup recompute (which only drops past-due one-shots the runner
    owns) and reaches the observer callback."""
    store_path = tmp_path / "jobs.json"
    now = int(time.time() * 1000)
    _write_store(store_path, [_at_job("m1", now - _GRACE_MS - 60_000)])

    svc = _gateway_svc(store_path)
    seen: list[list] = []
    svc.on_missed_foreign = lambda jobs: seen.append([j.id for j in jobs])
    await svc.start()
    svc.stop()

    assert seen and seen[0] == ["m1"]
    data = json.loads(store_path.read_text(encoding="utf-8"))
    assert [j["id"] for j in data["jobs"]] == ["m1"], "observed job must not be dropped or mutated"


async def test_observer_error_breaks_neither_start_nor_loop(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now = int(time.time() * 1000)
    _write_store(store_path, [_at_job("boom", now - _GRACE_MS - 60_000)])

    svc = _gateway_svc(store_path)

    def _explode(jobs) -> None:
        raise RuntimeError("observer bug")

    svc.on_missed_foreign = _explode
    await svc.start()
    await asyncio.sleep(0.05)  # let the first wake-loop pass run
    svc.stop()

    assert svc._loop_task is None  # stop() ran; no crash escaped


async def test_wake_loop_pass_invokes_missed_observer(tmp_path: Path) -> None:
    """A job crossing the grace window while the service is running is
    picked up by a later wake-loop pass, not only by start(). The service
    re-reports on every pass while the job stays missed — once-only
    notification is the observer callback's job (make_on_missed_foreign's
    notified set), not the service's."""
    store_path = tmp_path / "jobs.json"
    _write_store(store_path, [])

    svc = _gateway_svc(store_path)
    seen: list[str] = []
    svc.on_missed_foreign = lambda jobs: seen.extend(j.id for j in jobs)
    await svc.start()
    try:
        assert seen == []
        # A peer (the tui process) writes a job that is already past grace,
        # then signals the loop the way any store mutation does.
        now = int(time.time() * 1000)
        _write_store(store_path, [_at_job("late1", now - _GRACE_MS - 60_000)])
        svc._signal_wake()
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)
    finally:
        svc.stop()

    assert seen and set(seen) == {"late1"}
