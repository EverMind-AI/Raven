"""A failed trial is routed by whether its text states a cause, not by failing.

The wake message used to carry one failure branch: "if trials FAILED with a code
error in the trial script, do NOT rewrite the trial code yourself -- ops_finish
with outcome='failed' to hand it off." Sound advice, and the only named case, so
an unexplained death borrowed it.

Measured 2026-08-14, M3's third leg. A trial was killed from outside at 422s.
SIGKILL runs no ``finally``, so no result.json was written, and the process
backend fills ``error`` from ``tail -30 job.log``. For this training script that
tail is the checkpoint write from the last eval:

    Loading weights: 100%|##########| 310/310
    [transformers] `use_cache=True` is incompatible with gradient checkpointing.
    Writing model shards: 100%|##########| 1/1

No traceback, no exception, nothing that failed -- and the process stops right
after. The loop read the progress bars as the crash site, reported "a code-level
fault in the trial script / Loading weights stage", took the branch above, and
handed the campaign back with 88% of the budget unspent. Its own report said, in
the same breath, that training had been normal and the loss carried no NaN.

So the fix is not "try harder": the branch had no name for a death with no
stated cause, and the evidence surface reads like a cause either way. These
tests pin both halves of the split.

They assert on prompt text on purpose. The text is the contract the loop is
handed each wake; if it silently loses the unexplained case, the leg that
follows it looks like an agent that gave up.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.tools.ops import OpsSubmitTool


class _Job:
    def __init__(self, i):
        self.id = str(i)


class _FakeCron:
    def __init__(self):
        self.jobs: list[dict] = []

    def add_job(self, **kw):
        self.jobs.append(kw)
        return _Job(len(self.jobs))

    def list_jobs(self):
        return [SimpleNamespace(id=str(i), name=j["name"]) for i, j in enumerate(self.jobs)]

    def remove_job(self, job_id):
        del self.jobs[int(job_id)]
        return True


def _campaign(tmp_path: Path) -> Path:
    cdir = tmp_path / "c"
    cdir.mkdir(exist_ok=True)
    (cdir / "meta.json").write_text(
        json.dumps(
            {
                "backend": "process", "host": "h", "port": 22, "key": "~/.ssh/id_rsa",
                "command": "run {config} {job_dir}",
                "budget": {"unit": "gpu-minute", "total": 60, "overlap": "additive"},
            }
        ),
        encoding="utf-8",
    )
    return cdir


def _install(monkeypatch):
    class _Backend:
        _run = staticmethod(lambda cmd: (0, ""))
        async def spent_minutes(self): return 0.0
        async def remaining_minutes(self): return 60.0
        def unmeasured_spend(self): return {}
        async def submit(self, spec):
            return SimpleNamespace(backend="process", job_id=f"ops-{spec.idem_key}")
        async def poll(self, handle):
            from raven.ops import JobStatus
            return JobStatus.RUNNING

    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: _Backend())
    monkeypatch.setattr("raven.ops.prepare_from_meta", lambda *a, **k: None, raising=False)


async def _wake_message(tmp_path: Path, monkeypatch) -> str:
    cdir = _campaign(tmp_path)
    _install(monkeypatch)
    cron = _FakeCron()
    tool = OpsSubmitTool(cron_service=cron)
    tool.set_context("cli", "direct")
    await tool.execute(
        ledger=str(cdir / "ledger.json"), campaign="c", host="h", objective="o",
        eta_seconds=60, round=0, configs=[{"lr": 2e-05}],
    )
    assert cron.jobs, "submitting a round must schedule the wake that reads it"
    return cron.jobs[-1]["message"]


@pytest.mark.asyncio
async def test_a_stated_code_error_is_still_handed_off(tmp_path, monkeypatch) -> None:
    """The original branch survives: a real script bug is not the loop's to fix."""
    message = await _wake_message(tmp_path, monkeypatch)
    assert "code error in the trial script" in message
    assert "do NOT rewrite the trial code yourself" in message
    assert "outcome='failed'" in message


@pytest.mark.asyncio
async def test_the_unexplained_death_is_named_and_is_not_a_hand_off(tmp_path, monkeypatch) -> None:
    """The case M3 fell into now has its own name, and it points at resubmit."""
    message = await _wake_message(tmp_path, monkeypatch)
    assert "no traceback" in message
    assert "states no cause" in message
    assert "ended from outside" in message
    assert "resubmit before handing it back" in message


@pytest.mark.asyncio
async def test_the_branch_turns_on_the_text_not_on_having_failed(tmp_path, monkeypatch) -> None:
    """Both arms are conditioned on what the failure says, so neither is the default.

    The wording that produced M3's leg named only one arm; whichever way a future
    edit shortens this, a failed trial must not arrive with a single option.
    """
    message = await _wake_message(tmp_path, monkeypatch)
    assert "decide from what the failure text actually states" in message
    # A stated cause is what the hand-off arm requires, and it is spelled out
    # rather than left to the reader: bars and warnings are not one.
    assert "a named exception or a traceback is a stated cause" in message
    assert "progress bars" in message
