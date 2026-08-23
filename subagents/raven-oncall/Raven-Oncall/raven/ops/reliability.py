"""Ops reliability eval: score orchestration correctness under injected faults.

Each scenario drives a campaign on the deterministic MockJobBackend (no wall
clock, so it is repeatable) with one fault injected -- a crash in the window
between submit and persisting the handle, a duplicate completion event, a
transient backend outage, or exhausted retries -- and checks one invariant. The
suite returns a ScoreCard whose ``reliability`` is the fraction of invariants
that held. This is the reward substrate for self-evolving the orchestration
policy: an edit that breaks an invariant lowers the score.

The scenarios are deliberately parameterizable where it lets a broken
environment be constructed (e.g. a non-idempotent backend), so the eval can be
shown to *fail* -- an eval that only ever passes measures nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from raven.ops.backend import JobSpec
from raven.ops.campaign import Campaign, Trial
from raven.ops.ledger import JobRecord, Ledger
from raven.ops.mock_backend import JobPlan, MockJobBackend
from raven.ops.policy import RetryPolicy


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class ScoreCard:
    results: list[ScenarioResult] = field(default_factory=list)

    @property
    def reliability(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.passed) / len(self.results)

    @property
    def failed(self) -> list[ScenarioResult]:
        return [r for r in self.results if not r.passed]


async def resume_completes(workdir: Path) -> ScenarioResult:
    backend = MockJobBackend(default_plan=JobPlan(succeed_after_polls=3, metrics={"score": 0.5}))
    trials = [Trial("t1"), Trial("t2"), Trial("t3")]
    path = workdir / "l.json"
    first = Campaign("s", trials, backend, Ledger(path))
    await first.step()
    await first.step()
    resumed = Campaign("s", trials, backend, Ledger(path))
    await resumed.run()
    ok = resumed.is_done() and len(backend._jobs) == len(trials) and resumed.best() is not None
    return ScenarioResult("resume_completes", ok, f"jobs={len(backend._jobs)} done={resumed.is_done()}")


async def submit_window_no_double_submit(workdir: Path, *, idempotent: bool = True) -> ScenarioResult:
    backend = MockJobBackend(default_plan=JobPlan(metrics={"score": 0.5}), idempotent=idempotent)
    ledger = Ledger(workdir / "l.json")
    await backend.submit(JobSpec({}, idem_key="t1"))
    ledger.record("t1", campaign="s")
    campaign = Campaign("s", [Trial("t1")], backend, ledger)
    await campaign.run()
    ok = campaign.is_done() and len(backend._jobs) == 1
    return ScenarioResult("submit_window_no_double_submit", ok, f"jobs={len(backend._jobs)} expected=1")


async def duplicate_event_idempotent(workdir: Path) -> ScenarioResult:
    backend = MockJobBackend(default_plan=JobPlan(metrics={"score": 0.7}))
    trials = [Trial("t1"), Trial("t2")]
    campaign = Campaign("s", trials, backend, Ledger(workdir / "l.json"), metric="score")
    await campaign.run()
    jobs_before = len(backend._jobs)
    best_before = campaign.best().result.metrics["score"]
    await campaign.step()
    ok = len(backend._jobs) == jobs_before and campaign.best().result.metrics["score"] == best_before
    return ScenarioResult("duplicate_event_idempotent", ok, f"jobs {jobs_before}->{len(backend._jobs)}")


async def transient_backend_recovers(workdir: Path) -> ScenarioResult:
    backend = MockJobBackend(default_plan=JobPlan(metrics={"score": 0.6}), transient_poll_errors=3)
    trials = [Trial("t1"), Trial("t2")]
    campaign = Campaign("s", trials, backend, Ledger(workdir / "l.json"), metric="score")
    await campaign.run()
    ok = campaign.is_done() and campaign.best() is not None
    return ScenarioResult("transient_backend_recovers", ok, f"done={campaign.is_done()}")


async def escalation_fires_once(workdir: Path) -> ScenarioResult:
    backend = MockJobBackend(
        plans={"t1": JobPlan(fail=True, error="boom"), "t1#a2": JobPlan(fail=True, error="boom")}
    )
    count = 0

    async def on_escalate(trial_id: str, rec: JobRecord) -> None:
        nonlocal count
        count += 1

    campaign = Campaign(
        "s", [Trial("t1")], backend, Ledger(workdir / "l.json"),
        retry_policy=RetryPolicy(max_retries=1), escalation=on_escalate,
    )
    await campaign.run()
    ok = campaign.is_done() and count == 1
    return ScenarioResult("escalation_fires_once", ok, f"escalations={count}")


SCENARIOS: list[Callable[[Path], Awaitable[ScenarioResult]]] = [
    resume_completes,
    submit_window_no_double_submit,
    duplicate_event_idempotent,
    transient_backend_recovers,
    escalation_fires_once,
]


async def run_suite(workdir: Path) -> ScoreCard:
    results = []
    for i, scenario in enumerate(SCENARIOS):
        cell = workdir / f"scn{i}"
        cell.mkdir(parents=True, exist_ok=True)
        results.append(await scenario(cell))
    return ScoreCard(results)
