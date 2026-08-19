"""A trial is its config *and* the apparatus it ran against.

Measured 2026-08-13, the CFD divergence leg. Round 0 failed; the arm made ten
edits to the case; round 1 resubmitted the same config. The name is derived from
the config alone, so it was the same name, so it was the same job directory --
and the run script's restart branch keeps ``system/`` rather than re-staging.
None of the ten edits reached the job. It failed again, identically, eleven
minutes later.

Three losses from one cause:

  * the edits did not apply, and nothing said so;
  * round 0's ``job.log`` was overwritten in place, and it was the evidence;
  * round 0's spend vanished with it. The backend measures per job directory, so
    the overwritten round stopped existing: the report and the ledger both said
    115.15 core-minutes where 126.2 had been spent.

Idempotency is not weakened by this, it is corrected. "The same config against a
changed case" is a different run, and treating it as a repeat was the bug.

A campaign with no staged case -- every ML campaign -- has no apparatus digest
and its names are untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops import OpsSubmitTool


class _Job:
    def __init__(self, i): self.id = str(i)


class _FakeCron:
    def __init__(self): self.jobs = []
    def add_job(self, **kw): self.jobs.append(kw); return _Job(len(self.jobs))
    def list_jobs(self, **kw):
        from types import SimpleNamespace
        return [SimpleNamespace(id=str(i), name=j["name"],
                                payload=SimpleNamespace(campaign=j.get("campaign")))
                for i, j in enumerate(self.jobs)]
    def remove_job(self, job_id): del self.jobs[int(job_id)]; return True


def _campaign(tmp_path: Path, *, staged: str = "") -> Path:
    cdir = tmp_path / "c"
    cdir.mkdir(exist_ok=True)
    meta = {"backend": "process", "host": "h", "port": 22, "key": "~/.ssh/id_rsa",
            "command": "run {config} {job_dir}",
            "budget": {"unit": "core-minute", "total": 150, "overlap": "additive"}}
    if staged:
        meta["staged_case"] = staged
    (cdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return cdir


def _install(monkeypatch, case_sha="aaa", submitted=None):
    from types import SimpleNamespace

    submitted = [] if submitted is None else submitted

    class _Backend:
        _run = staticmethod(lambda cmd: (0, f"{case_sha}  /remote/case/system/controlDict\n"))
        async def spent_minutes(self): return 0.0
        async def remaining_minutes(self): return 150.0
        def unmeasured_spend(self): return {}
        async def submit(self, spec):
            submitted.append(spec.idem_key)
            return SimpleNamespace(backend="process", job_id=f"ops-{spec.idem_key}")
        async def poll(self, handle):
            from raven.ops import JobStatus
            return JobStatus.RUNNING

    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: _Backend())
    monkeypatch.setattr("raven.ops.prepare_from_meta", lambda *a, **k: None, raising=False)
    return submitted


def _observed(cdir: Path) -> None:
    from raven.ops.state_claims import StateFacts, write_facts
    write_facts(cdir, StateFacts(metric_readings={"x": (1.0,)}, probe_seq=1))


async def _submit(cdir: Path, cron, **kw):
    sub = OpsSubmitTool(cron_service=cron)
    sub.set_context("cli", "direct")
    kw.setdefault("host", "h"); kw.setdefault("objective", "o")
    kw.setdefault("eta_seconds", 60); kw.setdefault("campaign", "c")
    return await sub.execute(ledger=str(cdir / "ledger.json"), **kw)


@pytest.mark.asyncio
async def test_the_same_config_on_a_changed_case_is_a_new_trial(tmp_path, monkeypatch):
    cdir = _campaign(tmp_path, staged="/remote/case")
    sent = _install(monkeypatch, case_sha="before")
    cron = _FakeCron()
    await _submit(cdir, cron, configs=[{"deltaT": "5e-4"}], round=0)

    _install(monkeypatch, case_sha="AFTER", submitted=sent)   # the arm edited the case
    _observed(cdir)
    await _submit(cdir, cron, configs=[{"deltaT": "5e-4"}], round=1,
                  basis="x 1.0 on the latest probe; retrying on the fixed case")

    assert len(sent) == 2 and sent[0] != sent[1], (
        f"same job directory reused: {sent}"
    )
    assert sent[0].startswith("deltaT5em4") and sent[1].startswith("deltaT5em4")


@pytest.mark.asyncio
async def test_the_same_config_on_the_same_case_is_the_same_trial(tmp_path, monkeypatch):
    """Crash-resume rests on this: re-driving a submit must not spend twice."""
    cdir = _campaign(tmp_path, staged="/remote/case")
    sent = _install(monkeypatch, case_sha="same")
    cron = _FakeCron()
    await _submit(cdir, cron, configs=[{"deltaT": "5e-4"}], round=0)
    _observed(cdir)
    await _submit(cdir, cron, configs=[{"deltaT": "5e-4"}], round=1, basis="x 1.0 just read")
    assert sent[0] == sent[1]


@pytest.mark.asyncio
async def test_a_campaign_with_no_staged_case_is_untouched(tmp_path, monkeypatch):
    """Every ML campaign. No apparatus to fingerprint, so no suffix."""
    cdir = _campaign(tmp_path)
    sent = _install(monkeypatch)
    await _submit(cdir, _FakeCron(), configs=[{"lr": "5e-06", "epochs": 8}], round=0)
    assert sent == ["epochs8_lr5em06"], sent


@pytest.mark.asyncio
async def test_a_record_under_the_old_spelling_is_reused(tmp_path, monkeypatch):
    """A campaign started before the key was shortened holds its records under the
    old names. Recomputing would read them as trials that never ran."""
    from raven.ops.proposer import legacy_config_key

    cdir = _campaign(tmp_path)
    cfg = {"data": "/a/very/long/path/that/used/to/be/spelled/out", "run": 1}
    old = legacy_config_key(cfg)
    (cdir / "ledger.json").write_text(json.dumps({"version": 1, "records": {
        old: {"idem_key": old, "status": "succeeded", "campaign": "c",
              "handle": None, "attempts": 1, "escalated": False, "metrics": {}},
    }}), encoding="utf-8")

    sent = _install(monkeypatch)
    _observed(cdir)
    await _submit(cdir, _FakeCron(), configs=[cfg], round=1, basis="x 1.0 just read")
    assert sent == [old], f"should have reused {old!r}, sent {sent}"
