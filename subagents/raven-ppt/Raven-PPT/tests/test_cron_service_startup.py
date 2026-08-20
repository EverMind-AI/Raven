"""Startup recompute/drop in CronService is scoped to the runner's partition.

_recompute_next_runs runs on start(): it drops past-due one-shot reminders
and re-anchors recurring schedules. Both must only touch jobs whose channel
this runner owns — a gateway restart must never drop or rewrite a past-due
TUI reminder that belongs to the TUI process.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from raven.proactive_engine.schedulers.cron.service import CronService


def _write_store(store_path: Path, jobs: list[dict]) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({"version": 1, "jobs": jobs}), encoding="utf-8")


def _past_due_tui_at_job(now_ms: int) -> dict:
    return {
        "id": "tui1",
        "name": "tui reminder",
        "enabled": True,
        "schedule": {"kind": "at", "atMs": now_ms - 60_000},
        "payload": {"message": "stretch", "channel": "tui", "to": "direct"},
        "state": {"nextRunAtMs": now_ms - 60_000},
        "createdAtMs": now_ms - 120_000,
        "updatedAtMs": now_ms - 120_000,
        "deleteAfterRun": True,
    }


async def test_startup_does_not_drop_foreign_past_due_at_job(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    _write_store(store_path, [_past_due_tui_at_job(now_ms)])

    svc = CronService(store_path, allowed_channels={"weixin"})
    await svc.start()
    svc.stop()

    data = json.loads(store_path.read_text(encoding="utf-8"))
    assert [j["id"] for j in data["jobs"]] == ["tui1"], "foreign past-due job must survive a gateway start"
    assert data["jobs"][0]["state"]["nextRunAtMs"] == now_ms - 60_000, "foreign job state must pass through as loaded"


async def test_startup_drops_own_past_due_at_job_with_warning(tmp_path: Path) -> None:
    from loguru import logger

    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    _write_store(store_path, [_past_due_tui_at_job(now_ms)])

    svc = CronService(store_path, allowed_channels={"tui"})
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        await svc.start()
    finally:
        svc.stop()
        logger.remove(sink_id)

    data = json.loads(store_path.read_text(encoding="utf-8"))
    assert data["jobs"] == [], "own past-due one-shot must be dropped on startup"
    assert any("past-due" in ln for ln in lines), f"expected a drop warning, got {lines}"


async def test_startup_recompute_scoped_to_own_channel(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    foreign_next = now_ms - 5_000
    _write_store(
        store_path,
        [
            {
                "id": "im1",
                "name": "weixin ping",
                "enabled": True,
                "schedule": {"kind": "every", "everyMs": 600_000},
                "payload": {"message": "ping", "channel": "weixin", "to": "u1"},
                "state": {"nextRunAtMs": foreign_next},
                "createdAtMs": now_ms - 600_000,
                "updatedAtMs": now_ms - 600_000,
            },
            {
                "id": "tui2",
                "name": "tui ping",
                "enabled": True,
                "schedule": {"kind": "every", "everyMs": 600_000},
                "payload": {"message": "ping", "channel": "tui", "to": "direct"},
                "state": {"nextRunAtMs": now_ms - 5_000},
                "createdAtMs": now_ms - 600_000,
                "updatedAtMs": now_ms - 600_000,
            },
        ],
    )

    svc = CronService(store_path, allowed_channels={"tui"})
    await svc.start()
    svc.stop()

    data = json.loads(store_path.read_text(encoding="utf-8"))
    by_id = {j["id"]: j for j in data["jobs"]}
    assert by_id["im1"]["state"]["nextRunAtMs"] == foreign_next, "foreign recurring job must not be re-anchored"
    assert by_id["tui2"]["state"]["nextRunAtMs"] >= now_ms, "own recurring job must be re-anchored to the future"


async def test_startup_leaves_own_disabled_job_untouched(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    job = _past_due_tui_at_job(now_ms)
    job["enabled"] = False
    _write_store(store_path, [job])

    svc = CronService(store_path, allowed_channels={"tui"})
    await svc.start()
    svc.stop()

    data = json.loads(store_path.read_text(encoding="utf-8"))
    assert [j["id"] for j in data["jobs"]] == ["tui1"], "disabled jobs are never dropped by startup recompute"


async def test_startup_drop_records_structured_info(tmp_path: Path) -> None:
    """Each startup drop SHALL be recorded on ``last_startup_drops`` with the
    job's name, payload message, and scheduled at_ms, so the embedding process
    can surface a missed-reminders notice."""
    from raven.proactive_engine.schedulers.cron.types import CronStartupDrop

    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    _write_store(store_path, [_past_due_tui_at_job(now_ms)])

    svc = CronService(store_path, allowed_channels={"tui"})
    assert svc.last_startup_drops == [], "readable before start()"
    await svc.start()
    svc.stop()

    assert svc.last_startup_drops == [
        CronStartupDrop(name="tui reminder", message="stretch", at_ms=now_ms - 60_000),
    ]


async def test_startup_drop_records_empty_for_foreign_or_clean_start(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    _write_store(store_path, [_past_due_tui_at_job(now_ms)])

    svc = CronService(store_path, allowed_channels={"weixin"})
    await svc.start()
    svc.stop()
    assert svc.last_startup_drops == [], "foreign past-due job must not be recorded as this runner's drop"

    svc2 = CronService(tmp_path / "empty.json", allowed_channels={"tui"})
    await svc2.start()
    svc2.stop()
    assert svc2.last_startup_drops == []
