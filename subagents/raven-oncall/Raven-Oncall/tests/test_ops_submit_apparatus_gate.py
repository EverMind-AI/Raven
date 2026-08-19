"""Submitting checks that the apparatus is still the one the campaign declared.

The gate has two halves and they are deliberately unequal.

**meta.json refuses.** It is the campaign's own declaration -- objective, budget,
host, which case, which command. An agent editing it is out of role in every
domain, and the harm is not confined to scoring: on 2026-08-12 an arm rewrote
``remote_dir`` / ``staged_case`` / ``command`` because the task text named a
different case path than the meta did. The job then ran out of an undeclared
directory against a shared baseline case, and the budget guard watching the
declared path watched an empty directory for three hours. It was right that the
two disagreed -- the inconsistency was ours -- but the way to raise that is
ops_ask_owner, not editing the apparatus and carrying on.

**Case edits do not refuse.** Editing the case is the work: the same day, another
arm corrected a viscosity that was wrong by a thousand times, and that was the
task being done well. They are recorded and shown on every submit, and whether a
particular edit amounted to changing the question belongs to that task's
pre-registration, not to this layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops import OpsSubmitTool
from raven.ops.apparatus import BASELINE_FILE, baseline_of, save_baseline


class _Job:
    def __init__(self, i): self.id = str(i)


class _FakeCron:
    def __init__(self): self.jobs = []
    def add_job(self, **kw): self.jobs.append(kw); return _Job(len(self.jobs))
    def list_jobs(self):
        from types import SimpleNamespace
        return [SimpleNamespace(id=str(i), name=j["name"]) for i, j in enumerate(self.jobs)]
    def remove_job(self, job_id): del self.jobs[int(job_id)]; return True


class _Runner:
    """Remote shell stand-in serving one case file whose content we control."""
    def __init__(self, sha="aaa"): self.sha = sha
    def __call__(self, cmd):
        return 0, f"{self.sha}  /remote/case/system/controlDict\n"


def _observed(cdir: Path) -> None:
    """A recorded probe, so round >= 1 can cite one -- exactly as a real turn does."""
    from raven.ops.state_claims import StateFacts, write_facts
    write_facts(cdir, StateFacts(metric_readings={"ndcg": (0.29,)}, probe_seq=1))


BASIS = "ndcg 0.29 on the latest sample; trying the next config"


def _campaign(tmp_path: Path) -> Path:
    cdir = tmp_path / "c"
    cdir.mkdir(exist_ok=True)
    (cdir / "meta.json").write_text(json.dumps({
        "backend": "process", "host": "h", "port": 22, "key": "~/.ssh/id_rsa",
        "command": "run {config} {job_dir}", "staged_case": "/remote/case",
        "budget": {"unit": "core-minute", "total": 150, "overlap": "additive"},
    }, ensure_ascii=False), encoding="utf-8")
    return cdir


def _install(monkeypatch, sha="aaa"):
    """A backend that submits without touching a real host."""
    from types import SimpleNamespace

    runner = _Runner(sha)

    class _Backend:
        _run = staticmethod(runner)
        async def spent_minutes(self): return 0.0
        async def remaining_minutes(self): return 150.0
        def unmeasured_spend(self): return {}
        async def submit(self, spec):
            return SimpleNamespace(backend="process", job_id=f"ops-{spec.idem_key}")
        async def poll(self, handle):
            from raven.ops import JobStatus
            return JobStatus.RUNNING

    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: _Backend())
    monkeypatch.setattr("raven.ops.prepare_from_meta", lambda *a, **k: None, raising=False)
    return runner


@pytest.mark.asyncio
async def test_first_submit_records_the_baseline(tmp_path, monkeypatch):
    cdir = _campaign(tmp_path)
    _install(monkeypatch)
    sub = OpsSubmitTool(cron_service=_FakeCron()); sub.set_context("cli", "direct")
    out = await sub.execute(host="h", configs=[{"a": 1}], objective="o",
                            ledger=str(cdir / "ledger.json"), eta_seconds=60,
                            round=0, campaign="c")
    assert "Submitted" in out
    assert (cdir / BASELINE_FILE).exists(), "the first submit is when the apparatus is known good"


@pytest.mark.asyncio
async def test_an_edited_meta_refuses_the_next_submit(tmp_path, monkeypatch):
    cdir = _campaign(tmp_path)
    _install(monkeypatch)
    sub = OpsSubmitTool(cron_service=_FakeCron()); sub.set_context("cli", "direct")
    await sub.execute(host="h", configs=[{"a": 1}], objective="o",
                      ledger=str(cdir / "ledger.json"), eta_seconds=60, round=0, campaign="c")

    m = json.loads((cdir / "meta.json").read_text())
    m["staged_case"] = "/remote/somewhere-else"
    (cdir / "meta.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    _observed(cdir)
    out = await sub.execute(host="h", configs=[{"a": 2}], objective="o",
                            ledger=str(cdir / "ledger.json"), eta_seconds=60, round=1,
                            campaign="c", basis=BASIS)
    assert "REFUSED" in out
    assert "meta.json" in out
    assert "ops_ask_owner" in out, "refusing without naming the way out just blocks it"


@pytest.mark.asyncio
async def test_an_edited_case_is_reported_and_still_submits(tmp_path, monkeypatch):
    cdir = _campaign(tmp_path)
    _install(monkeypatch, sha="aaa")
    sub = OpsSubmitTool(cron_service=_FakeCron()); sub.set_context("cli", "direct")
    await sub.execute(host="h", configs=[{"a": 1}], objective="o",
                      ledger=str(cdir / "ledger.json"), eta_seconds=60, round=0, campaign="c")

    _install(monkeypatch, sha="REWRITTEN")     # the case file changed underneath
    _observed(cdir)
    out = await sub.execute(host="h", configs=[{"a": 2}], objective="o",
                            ledger=str(cdir / "ledger.json"), eta_seconds=60, round=1,
                            campaign="c", basis=BASIS)

    assert "Submitted" in out, "editing the case is the job, not a violation"
    assert "controlDict" in out, "but it must be said out loud"
    events = (cdir / "events.jsonl").read_text()
    assert "apparatus_drift" in events, "and it must survive the turn that saw it"


@pytest.mark.asyncio
async def test_an_unreadable_case_does_not_block_the_submit(tmp_path, monkeypatch):
    """A host that cannot answer must not read as a wiped apparatus."""
    cdir = _campaign(tmp_path)
    _install(monkeypatch)
    sub = OpsSubmitTool(cron_service=_FakeCron()); sub.set_context("cli", "direct")
    await sub.execute(host="h", configs=[{"a": 1}], objective="o",
                      ledger=str(cdir / "ledger.json"), eta_seconds=60, round=0, campaign="c")

    from types import SimpleNamespace

    class _Dead:
        _run = staticmethod(lambda cmd: (1, ""))
        async def spent_minutes(self): return 0.0
        async def remaining_minutes(self): return 150.0
        def unmeasured_spend(self): return {}
        async def submit(self, spec):
            return SimpleNamespace(backend="process", job_id=f"ops-{spec.idem_key}")
        async def poll(self, handle):
            from raven.ops import JobStatus
            return JobStatus.RUNNING

    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: _Dead())
    _observed(cdir)
    out = await sub.execute(host="h", configs=[{"a": 2}], objective="o",
                            ledger=str(cdir / "ledger.json"), eta_seconds=60, round=1,
                            campaign="c", basis=BASIS)
    assert "Submitted" in out
    assert "REFUSED" not in out
