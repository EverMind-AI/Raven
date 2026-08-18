"""Load-time migration of legacy cron delivery bindings in CronService.

The "cli" channel value is retired with the REPL: ``_load_store`` maps a
stored ``payload.channel == "cli"`` to ``"tui"`` at deserialize time (one
INFO log per occurrence) so the TUI — the interactive surface — owns the
job. The rewrite persists on the next save.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from loguru import logger

from raven.proactive_engine.schedulers.cron.service import CronService


def _write_legacy_store(store_path: Path, *, job_id: str = "abcd1234") -> None:
    now_ms = int(time.time() * 1000)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": job_id,
                        "name": "legacy reminder",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 60_000},
                        "payload": {"message": "stretch", "channel": "cli", "to": "direct"},
                        "state": {"nextRunAtMs": now_ms + 60_000},
                        "createdAtMs": now_ms,
                        "updatedAtMs": now_ms,
                        "deleteAfterRun": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_legacy_cli_bound_job_loads_as_tui(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    _write_legacy_store(store_path)

    svc = CronService(store_path)
    jobs = svc.list_jobs(include_disabled=True)

    assert [j.id for j in jobs] == ["abcd1234"]
    assert jobs[0].payload.channel == "tui"
    assert jobs[0].payload.to == "direct"


def test_migration_logs_one_info_line_per_job(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    _write_legacy_store(store_path, job_id="feed5678")

    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        CronService(store_path).list_jobs(include_disabled=True)
    finally:
        logger.remove(sink_id)

    migrated = [ln for ln in lines if "migrated legacy cli-bound job" in ln]
    assert len(migrated) == 1
    assert "feed5678" in migrated[0]
    assert "tui" in migrated[0]


def test_migration_persists_on_next_save(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    _write_legacy_store(store_path)

    svc = CronService(store_path)
    # Any store write persists the migrated binding (enable_job saves).
    assert svc.enable_job("abcd1234", enabled=False) is not None

    data = json.loads(store_path.read_text(encoding="utf-8"))
    by_id = {j["id"]: j for j in data["jobs"]}
    assert by_id["abcd1234"]["payload"]["channel"] == "tui"


def test_non_cli_channels_pass_through_unmigrated(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "aaaa1111",
                        "name": "tui job",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 60_000},
                        "payload": {"message": "a", "channel": "tui", "to": "direct"},
                        "state": {"nextRunAtMs": now_ms + 60_000},
                    },
                    {
                        "id": "bbbb2222",
                        "name": "im job",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 60_000},
                        "payload": {"message": "b", "channel": "feishu", "to": "ou_x"},
                        "state": {"nextRunAtMs": now_ms + 60_000},
                    },
                    {
                        "id": "cccc3333",
                        "name": "pre-attribution job",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 60_000},
                        "payload": {"message": "c", "channel": None, "to": None},
                        "state": {"nextRunAtMs": now_ms + 60_000},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    jobs = {j.id: j for j in CronService(store_path).list_jobs(include_disabled=True)}
    assert jobs["aaaa1111"].payload.channel == "tui"
    assert jobs["bbbb2222"].payload.channel == "feishu"
    assert jobs["cccc3333"].payload.channel is None
