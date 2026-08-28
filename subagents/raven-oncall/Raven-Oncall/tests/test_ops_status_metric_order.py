"""The metric name is resolved before the progress rendering that uses it.

Regression 2026-08-07: the resolution moved below `records = led.all()` so that a
campaign declaring no objective could be read from its records. The running-trial
series is labelled and filtered by that same name, and it renders above -- so it
was handed the empty string, and the loop saw

    , as logged: None:69.89 100:100 220:220 340:340

instead of the evaluation curve. Both arms of r15/r16 spent their first fifty
minutes writing "no nDCG eval results captured yet" while the job was writing
them all along. Cited from the tool's own output rather than from the code,
because the failure was visible only in what the loop was shown.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops import OpsTuneStatusTool
from raven.ops.backend import JobHandle, JobStatus
from raven.ops.ledger import Ledger


class _Backend:
    name = "process"

    def __init__(self, samples):
        self._samples = samples

    async def poll(self, handle):
        return JobStatus.RUNNING

    async def fetch_progress(self, handle, tail=5):
        return self._samples

    async def spent_minutes(self):
        return 12.0

    def unmeasured_spend(self):
        return {}

    async def remaining_minutes(self):
        return 128.0

    async def artifacts(self, handle):
        return []


@pytest.mark.asyncio
async def test_the_running_series_is_labelled_with_the_declared_metric(tmp_path: Path, monkeypatch) -> None:
    cdir = tmp_path / "ops" / "c1"
    cdir.mkdir(parents=True)
    (cdir / "meta.json").write_text(
        json.dumps({"backend": "process", "host": "h", "objective": {"metric": "ndcg", "direction": "max"}}),
        encoding="utf-8",
    )
    ledger = Ledger(cdir / "ledger.json")
    ledger.record("t1", campaign="c1")
    ledger.set_handle("t1", JobHandle(backend="process", job_id="t1"))

    samples = [
        {"step": 200, "loss": 0.5, "ndcg": 0.3444},
        {"step": 400, "loss": 0.4, "ndcg": 0.3454},
        {"step": 420, "loss": 0.35},
    ]
    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: _Backend(samples))

    tool = OpsTuneStatusTool()
    out = await tool.execute(ledger=str(cdir / "ledger.json"))

    assert "ndcg, as logged:" in out, out
    assert "0.3444" in out and "0.3454" in out
    assert ", as logged: None:" not in out, "the empty-named series is the regression"
