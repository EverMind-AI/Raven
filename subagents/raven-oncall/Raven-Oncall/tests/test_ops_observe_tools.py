"""The raw-output tools must hand output over whole, and judge nothing.

These exist because the compressing status tool loses almost everything on a job
that writes many lines per step, and shows nothing at all for a finished trial
whose backend reports no metrics. Both failures are asserted here.
"""

from __future__ import annotations

import pytest

from raven.agent.tools import ops_observe as mod
from raven.agent.tools.ops_observe import OpsOutputsTool
from raven.ops.backend import JobHandle, JobResult, JobStatus

JUDGEMENT_WORDS = ("diverg", "converg", "hopeless", "nan", "trend", "best", "healthy")


class FakeBackend:
    def __init__(self, lines: list[str], result: JobResult) -> None:
        self.lines = lines
        self.result = result
        self.tails: list[int] = []

    async def fetch_progress(self, handle, tail=5):
        self.tails.append(tail)
        return [{"line": line} for line in self.lines[-tail:]]

    async def fetch_result(self, handle):
        return self.result


class FakeRecord:
    def __init__(self, key: str) -> None:
        self.idem_key = key
        self.handle = JobHandle("fake", key)


class FakeLedger:
    """Mirrors raven.ops.Ledger's surface: get() and all().

    The first version of this fake exposed records() instead of all(). The tools
    called records() too, so the tests passed green while the real code path --
    which meets a real Ledger that has no such method -- raised AttributeError.
    A fake with a method the real class lacks tests the fake.
    """

    def __init__(self, keys: list[str]) -> None:
        self._recs = {k: FakeRecord(k) for k in keys}

    def get(self, key):
        return self._recs.get(key)

    def all(self):
        return list(self._recs.values())


def _wire(monkeypatch, backend, keys=("t1",)):
    monkeypatch.setattr(
        mod, "_campaign_backend",
        lambda campaign, ledger: (backend, FakeLedger(list(keys)), None),
    )


# One interFoam timestep: 18 lines, of which 2 carry the phase-fraction stats.
TIMESTEP = [
    "Time = 0.005", "PIMPLE: iteration 1",
    "smoothSolver:  Solving for alpha.water, Initial residual = 0.000106784",
    "Phase-1 volume fraction = 0.130194  Min(alpha.water) = 0  Max(alpha.water) = 1",
    "MULES: Correcting alpha.water", "MULES: Correcting alpha.water",
    "Phase-1 volume fraction = 0.130194  Min(alpha.water) = -7.1e-21  Max(alpha.water) = 1",
    "DICPCG:  Solving for p_rgh, Initial residual = 0.0016456",
    "time step continuity errors : sum local = 3.03e-05",
    "DICPCG:  Solving for p_rgh, Initial residual = 8.24e-05",
    "time step continuity errors : sum local = 1.41e-06",
    "DICPCG:  Solving for p_rgh, Initial residual = 3.86e-06",
    "time step continuity errors : sum local = 3.31e-08",
    "ExecutionTime = 0.07 s  ClockTime = 0 s",
    "Courant Number mean: 0.00217535 max: 0.026189",
    "Interface Courant Number mean: 3.19e-05 max: 0.0212012",
    "deltaT = 0.005", "Time = 0.01",
]


@pytest.mark.asyncio
async def test_outputs_prints_the_whole_result_even_with_no_metrics(monkeypatch):
    # A backend that refuses to invent a metric must not be rendered as
    # "nothing to report": that is exactly what the status tool did.
    result = JobResult(
        JobStatus.SUCCEEDED,
        metrics={},
        output={"log_tail": "SIMPLE solution converged in 281 iterations\nEnd",
                "time_directories": ["0", "100", "281"], "cores": 4},
    )
    backend = FakeBackend(TIMESTEP, result)
    _wire(monkeypatch, backend)
    out = await OpsOutputsTool().execute(campaign="c", trial="t1")
    assert "time_directories" in out and "281" in out
    assert "log_tail" in out
    assert '"metrics": {}' in out


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [OpsOutputsTool()])
def test_descriptions_suggest_no_verdict_and_no_action(tool):
    text = tool.description.lower()
    for word in JUDGEMENT_WORDS:
        assert word not in text, f"{tool.name} description hints a verdict via {word!r}"
    assert "consider" not in text
