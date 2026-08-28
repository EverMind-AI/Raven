"""A concluded campaign is announced into the host's cron store, once, verbatim.

Closes the silence measured 2026-08-25: a campaign dispatched from a host
raven's direct chat concluded entirely inside wake-spawned turns, and nothing
told the person. The shell stays a messenger: outcome and report head are
lifted from the campaign's own files, the reminder is ownerless (whichever
host window is open claims it) and one-shot, and with --notify-store omitted
nothing is written anywhere.
"""

import asyncio
import json
from pathlib import Path

import pytest

from raven.ops.wake_shell import Spawn, WakeShell


def _mk_campaign(home: Path, name: str, concluded: bool, outcome: str = "done") -> None:
    d = home / name
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("", encoding="utf-8")
    if concluded:
        (d / "concluded.json").write_text(json.dumps({"outcome": outcome}), encoding="utf-8")
        (d / "report-20260825T160000.md").write_text(
            "# t\n\n- campaign: x\n\n## Observed\n\n- best: 1\n", encoding="utf-8"
        )


async def _noop_spawner(job):
    return Spawn(job_id=job.id, job_name=job.name, argv=[], at_ms=0, duration_ms=1,
                 returncode=0, error=None)


def _shell(tmp_path: Path, notify: Path | None) -> WakeShell:
    store = tmp_path / "sub" / "cron" / "jobs.json"
    store.parent.mkdir(parents=True)
    return WakeShell(store, spawner=_noop_spawner, notify_store=notify)


def _job():
    from raven.proactive_engine.schedulers.cron.types import CronJob, CronJobState, CronPayload, CronSchedule
    return CronJob(id="j", name="ops:c1:r1", enabled=True,
                   schedule=CronSchedule(kind="at", at_ms=1),
                   payload=CronPayload(message="m", channel="cli", to="x"),
                   state=CronJobState(next_run_at_ms=1))


def _host_jobs(notify: Path) -> list[dict]:
    return json.loads(notify.read_text(encoding="utf-8")).get("jobs", []) if notify.exists() else []


def test_conclusion_is_announced_once(tmp_path, monkeypatch):
    home = tmp_path / "ops"
    monkeypatch.setattr("raven.ops.instrument.ops_home", lambda: home)
    notify = tmp_path / "host" / "cron" / "jobs.json"
    notify.parent.mkdir(parents=True)
    shell = _shell(tmp_path, notify)

    _mk_campaign(home, "c1", concluded=True, outcome="done")
    asyncio.run(shell._fire(_job()))
    jobs = _host_jobs(notify)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["name"] == "ops:c1:concluded"
    assert j["payload"]["channel"] == "tui"
    assert j["payload"].get("owner") in (None, "")
    assert j["deleteAfterRun"] is True
    assert "concluded: done" in j["payload"]["message"]
    assert "## Observed" in j["payload"]["message"]

    asyncio.run(shell._fire(_job()))          # 再 fire 一次：不得重复
    assert len(_host_jobs(notify)) == 1


def test_preexisting_conclusions_are_not_reannounced(tmp_path, monkeypatch):
    home = tmp_path / "ops"
    monkeypatch.setattr("raven.ops.instrument.ops_home", lambda: home)
    _mk_campaign(home, "old", concluded=True)   # 启动前就结题的
    notify = tmp_path / "host" / "cron" / "jobs.json"
    notify.parent.mkdir(parents=True)
    shell = _shell(tmp_path, notify)
    asyncio.run(shell._fire(_job()))
    assert _host_jobs(notify) == []


def test_without_notify_store_nothing_is_written(tmp_path, monkeypatch):
    home = tmp_path / "ops"
    monkeypatch.setattr("raven.ops.instrument.ops_home", lambda: home)
    shell = _shell(tmp_path, None)
    _mk_campaign(home, "c1", concluded=True)
    asyncio.run(shell._fire(_job()))
    assert not (tmp_path / "host").exists()


def test_unconcluded_campaign_is_silent(tmp_path, monkeypatch):
    home = tmp_path / "ops"
    monkeypatch.setattr("raven.ops.instrument.ops_home", lambda: home)
    notify = tmp_path / "host" / "cron" / "jobs.json"
    notify.parent.mkdir(parents=True)
    shell = _shell(tmp_path, notify)
    _mk_campaign(home, "c1", concluded=False)
    asyncio.run(shell._fire(_job()))
    assert _host_jobs(notify) == []
