"""Anti-runaway silent-fire guard in CronService.

``record_fire`` counts consecutive recurring fires; ``notify_user_active``
resets the counters on genuine user activity; at ``silent_fire_limit`` the
job is auto-disabled so an LLM-created every_seconds-forever job cannot
fire unattended all night. Counter state persists in jobs.json under the
legacy key names (silentFireCount / silentFireLimit) so old stores load.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule


def _add_every_job(svc: CronService, *, channel: str = "tui", to: str = "default", limit: int | None = None) -> str:
    job = svc.add_job(
        name=f"every {channel}:{to}",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="drink water",
        channel=channel,
        to=to,
    )
    if limit is not None:
        stored = next(j for j in svc._store.jobs if j.id == job.id)
        stored.silent_fire_limit = limit
        svc._save_store()
    return job.id


def _stored(store_path: Path, job_id: str) -> dict:
    data = json.loads(store_path.read_text(encoding="utf-8"))
    return next(j for j in data["jobs"] if j["id"] == job_id)


# ── record_fire: count + persist ──────────────────────────────────────


def test_record_fire_increments_and_persists(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path)
    job_id = _add_every_job(svc)

    assert svc.record_fire(job_id) is False
    assert svc.record_fire(job_id) is False

    stored = _stored(store_path, job_id)
    assert stored["state"]["silentFireCount"] == 2
    assert stored["enabled"] is True


def test_record_fire_auto_disables_at_limit(tmp_path: Path) -> None:
    from loguru import logger

    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path)
    job_id = _add_every_job(svc, limit=2)

    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        assert svc.record_fire(job_id) is False
        assert svc.record_fire(job_id) is True
    finally:
        logger.remove(sink_id)

    stored = _stored(store_path, job_id)
    assert stored["enabled"] is False
    assert stored["state"]["nextRunAtMs"] is None
    assert stored["state"]["silentFireCount"] == 2
    assert any("auto-disabled" in ln for ln in lines), f"expected an auto-disable warning, got {lines}"


def test_record_fire_ignores_oneshot_at_jobs(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path)
    job = svc.add_job(
        name="one shot",
        schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 3_600_000),
        message="once",
        channel="tui",
        to="default",
    )

    assert svc.record_fire(job.id) is False

    assert _stored(store_path, job.id)["state"]["silentFireCount"] == 0


def test_record_fire_unknown_id_returns_false(tmp_path: Path) -> None:
    svc = CronService(tmp_path / "jobs.json")
    assert svc.record_fire("nope") is False


def test_record_fire_no_limit_never_disables(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path)
    job_id = _add_every_job(svc, limit=None)
    stored = next(j for j in svc._store.jobs if j.id == job_id)
    stored.silent_fire_limit = None
    svc._save_store()

    for _ in range(20):
        assert svc.record_fire(job_id) is False

    stored_json = _stored(store_path, job_id)
    assert stored_json["enabled"] is True
    assert stored_json["state"]["silentFireCount"] == 20


# ── notify_user_active: reset scoped to (channel, to) ─────────────────


def test_notify_user_active_resets_only_matching_binding(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path)
    tui_id = _add_every_job(svc, channel="tui", to="default")
    im_id = _add_every_job(svc, channel="telegram", to="chat9")
    svc.record_fire(tui_id)
    svc.record_fire(im_id)

    assert svc.notify_user_active("telegram", "chat9") == 1

    assert _stored(store_path, im_id)["state"]["silentFireCount"] == 0
    assert _stored(store_path, tui_id)["state"]["silentFireCount"] == 1


def test_notify_user_active_none_matches_all(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path)
    a = _add_every_job(svc, channel="tui", to="default")
    b = _add_every_job(svc, channel="telegram", to="chat9")
    svc.record_fire(a)
    svc.record_fire(b)

    assert svc.notify_user_active() == 2

    assert _stored(store_path, a)["state"]["silentFireCount"] == 0
    assert _stored(store_path, b)["state"]["silentFireCount"] == 0


def test_notify_user_active_zero_counts_touch_nothing(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path)
    _add_every_job(svc)

    assert svc.notify_user_active("tui", "default") == 0


# ── persistence: legacy JSON keys ─────────────────────────────────────


def test_legacy_store_keys_load(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "old1",
                        "name": "legacy job",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 60_000},
                        "payload": {"message": "ping", "channel": "tui", "to": "default"},
                        "state": {"nextRunAtMs": now_ms + 60_000, "silentFireCount": 5},
                        "createdAtMs": now_ms,
                        "updatedAtMs": now_ms,
                        "silentFireLimit": 3,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    svc = CronService(store_path)
    job = svc.list_jobs()[0]
    assert job.state.silent_fire_count == 5
    assert job.silent_fire_limit == 3
    # Already past its (tighter) legacy limit: the next fire disables.
    assert svc.record_fire("old1") is True


def test_store_without_silent_keys_defaults(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "bare1",
                        "name": "no silent keys",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 60_000},
                        "payload": {"message": "ping", "channel": "tui", "to": "default"},
                        "state": {"nextRunAtMs": now_ms + 60_000},
                        "createdAtMs": now_ms,
                        "updatedAtMs": now_ms,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    job = CronService(store_path).list_jobs()[0]
    assert job.state.silent_fire_count == 0
    assert job.silent_fire_limit == 12


# ── end-to-end: auto-disable survives the wake loop's writeback ───────


async def test_auto_disable_survives_process_due_writeback(tmp_path: Path) -> None:
    """The critical seam: record_fire runs inside on_job, mid-execution;
    _writeback_after_run then patches enabled/next_run from the in-memory
    job. The handler flips job.enabled on disable, so the writeback must
    persist the disable rather than clobber it with a recomputed next run.
    """
    from raven.cli._cron_handler import make_on_cron_job

    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path, allowed_channels={"tui"})
    job_id = _add_every_job(svc, limit=1)
    due = next(j for j in svc._store.jobs if j.id == job_id)
    due.state.next_run_at_ms = 1
    svc._save_store()

    class _Handle:
        async def result(self):
            return None

    svc.on_job = make_on_cron_job(submit=lambda req: _Handle(), cron_service=svc)
    await svc._process_due()

    stored = _stored(store_path, job_id)
    assert stored["enabled"] is False, "writeback must not clobber the auto-disable"
    assert stored["state"]["nextRunAtMs"] is None
    assert stored["state"]["silentFireCount"] == 1
    assert stored["state"]["claimedByPid"] is None
    assert stored["state"]["silentFireCount"] == 1
    assert stored["state"]["claimedByPid"] is None


async def test_writeback_does_not_resurrect_store_side_disable(tmp_path):
    """A caller that only invokes record_fire (no in-flight job flip) must
    still end disabled: the writeback treats a store-side disable as sticky
    instead of patching enabled=True back from the in-memory copy."""
    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path, allowed_channels={"tui"})
    job_id = _add_every_job(svc, limit=1)
    due = next(j for j in svc._store.jobs if j.id == job_id)
    due.state.next_run_at_ms = 1
    svc._save_store()

    async def on_job(job):
        svc.record_fire(job.id)

    svc.on_job = on_job
    await svc._process_due()

    stored = _stored(store_path, job_id)
    assert stored["enabled"] is False, "store-side disable must survive the writeback"
    assert stored["state"]["nextRunAtMs"] is None


async def test_reset_matches_legacy_jobs_without_channel_attribution(tmp_path):
    """A pre-attribution job (payload.channel/to None) is claimable by any
    runner, so any genuine user activity must reset its counter too - an
    exact-match rule would let its count only ever climb until an unfair
    auto-disable."""
    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path, allowed_channels=None)
    job = svc.add_job(
        name="legacy",
        schedule=CronSchedule(kind="every", every_ms=3600_000),
        message="pre-attribution reminder",
    )
    svc.record_fire(job.id)
    assert next(j for j in svc._load_store().jobs).state.silent_fire_count == 1

    assert svc.notify_user_active(channel="telegram", to="tg_1") == 1
    assert next(j for j in svc._load_store().jobs).state.silent_fire_count == 0
