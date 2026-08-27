"""A wake message that names no campaign gets its handles appended at fire time.

A wake turn starts from an empty history, so the message is the whole of what it
has. Measured 2026-08-06, one arrived as "check the embedding fine-tuning progress
- round one should be done": no campaign, no ledger path. The turn went scanning
ports over ssh rather than reading the campaign it had been woken for.

The handles are read from disk when the job fires, so no producer has to have
carried them at creation -- which is the point, since the producer that dropped
them was a generic tool with no idea a campaign existed. Two campaigns in flight
and nothing is appended: the wrong campaign reads exactly like the right one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.config import paths as config_paths
from raven.ops.backend import JobStatus
from raven.ops.ledger import Ledger
from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronJob, CronPayload, CronSchedule


def _campaign(root: Path, name: str, *, in_flight: bool = True, concluded: bool = False) -> Path:
    cdir = root / name
    cdir.mkdir(parents=True)
    (cdir / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    ledger = Ledger(cdir / "ledger.json")
    ledger.record("trial-1", campaign=name)
    if not in_flight:
        ledger.set_status("trial-1", JobStatus.SUCCEEDED)
    if concluded:
        (cdir / "concluded.json").write_text("{}", encoding="utf-8")
    return cdir


def _ops_root(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "ops"
    root.mkdir()
    monkeypatch.setattr(config_paths, "get_ops_home", lambda: root)
    return root


def _svc(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "jobs.json", allowed_channels={"tui"})


def _job(name: str, message: str) -> CronJob:
    return CronJob(
        id="j1",
        name=name,
        schedule=CronSchedule(kind="at", at_ms=1),
        payload=CronPayload(message=message, channel="tui", to="default"),
    )


def test_the_measured_message_gets_the_campaign_and_the_ledger(tmp_path: Path, monkeypatch) -> None:
    root = _ops_root(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r15e")

    out = _svc(tmp_path)._with_campaign_handles(
        _job("check the embedding fine", "check the embedding fine-tuning progress - round one should be done")
    )

    assert "armb-embed-r15e" in out.payload.message
    assert str(root / "armb-embed-r15e" / "ledger.json") in out.payload.message


def test_the_job_name_wins_over_the_single_campaign_fallback(tmp_path: Path, monkeypatch) -> None:
    root = _ops_root(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r15e")
    _campaign(root, "cfd-transient")

    out = _svc(tmp_path)._with_campaign_handles(_job("ops:cfd-transient:recheck", "re-check"))

    assert "cfd-transient" in out.payload.message
    assert "armb-embed-r15e" not in out.payload.message


def test_two_campaigns_in_flight_append_nothing(tmp_path: Path, monkeypatch) -> None:
    """Naming the wrong campaign reads exactly like naming the right one, and the
    turn would then drive somebody else's experiment."""
    root = _ops_root(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r15e")
    _campaign(root, "cfd-transient")

    out = _svc(tmp_path)._with_campaign_handles(_job("check progress", "check progress"))

    assert out.payload.message == "check progress"


def test_a_finished_campaign_is_not_a_candidate(tmp_path: Path, monkeypatch) -> None:
    root = _ops_root(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r15e", in_flight=False)

    out = _svc(tmp_path)._with_campaign_handles(_job("check progress", "check progress"))

    assert out.payload.message == "check progress"


def test_a_concluded_campaign_is_not_a_candidate(tmp_path: Path, monkeypatch) -> None:
    root = _ops_root(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r15e", concluded=True)

    out = _svc(tmp_path)._with_campaign_handles(_job("check progress", "check progress"))

    assert out.payload.message == "check progress"


def test_no_campaign_on_disk_leaves_the_message_alone(tmp_path: Path, monkeypatch) -> None:
    _ops_root(monkeypatch, tmp_path)

    out = _svc(tmp_path)._with_campaign_handles(_job("buy milk", "buy milk"))

    assert out.payload.message == "buy milk"


def test_a_message_that_already_carries_the_ledger_is_not_appended_to(tmp_path: Path, monkeypatch) -> None:
    root = _ops_root(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r15e")
    ledger = str(root / "armb-embed-r15e" / "ledger.json")
    message = f"[Ops campaign 'armb-embed-r15e' re-check] Call ops_tune_status(ledger='{ledger}')"

    out = _svc(tmp_path)._with_campaign_handles(_job("ops:armb-embed-r15e:recheck", message))

    assert out.payload.message == message


def test_the_stored_job_is_not_modified(tmp_path: Path, monkeypatch) -> None:
    """A recurring job would otherwise accumulate one handle block per fire."""
    root = _ops_root(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r15e")
    job = _job("check progress", "check progress")

    _svc(tmp_path)._with_campaign_handles(job)

    assert job.payload.message == "check progress"


@pytest.mark.asyncio
async def test_the_callback_receives_the_enriched_copy(tmp_path: Path, monkeypatch) -> None:
    root = _ops_root(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r15e")
    svc = _svc(tmp_path)
    seen: list[str] = []

    async def on_job(job: CronJob) -> None:
        seen.append(job.payload.message)

    svc.on_job = on_job
    await svc._execute_job(_job("check progress", "check progress"))

    assert seen and "armb-embed-r15e" in seen[0]
