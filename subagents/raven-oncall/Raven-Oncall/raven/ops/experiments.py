"""Experiment A/B skeletons: the paired-control harnesses the eval plan defines.

Both run on the deterministic MockJobBackend, so a full experiment is seconds of
wall clock and exactly reproducible -- the time-compression posture the eval
plan mandates for anything "multi-day". Everything reports paired numbers
(orchestrated vs baseline on identical inputs), never absolutes.

Experiment A -- resume across an interrupt: drive a campaign partway, "crash"
(discard the in-memory campaign), rebuild from the same ledger, run to done.
The framework claim under test is *no duplicated compute*: the backend's job
count must equal the trial count.

Experiment B -- compute saved by steering: on identical scripted score surfaces,
compare a full grid sweep against a steered (proposer-driven) run, counting
trials until the target score is reached, in execution order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from raven.ops.campaign import Campaign, Trial
from raven.ops.ledger import Ledger
from raven.ops.mock_backend import JobPlan, MockJobBackend
from raven.ops.proposer import Proposer, config_key
from raven.ops.scripted_backend import JobScript, ScriptedJobBackend
from raven.ops.simclock import SimClock
from raven.ops.tuning import tune


async def experiment_a_interrupt(
    trials: list[Trial],
    plans: dict[str, JobPlan],
    workdir: str | Path,
    *,
    metric: str = "score",
    interrupt_after_passes: int = 1,
) -> dict[str, Any]:
    """Kill-and-resume once, at a chosen pass boundary. Returns the paired facts:
    completion after resume and whether any compute was duplicated."""
    workdir = Path(workdir)
    ledger_path = workdir / "expA-ledger.json"
    backend = MockJobBackend(plans=plans)

    first = Campaign("expA", trials, backend, Ledger(ledger_path), metric=metric)
    for _ in range(interrupt_after_passes):
        await first.step()
    del first  # the crash: in-memory campaign gone; only disk + backend survive

    resumed = Campaign("expA", trials, backend, Ledger(ledger_path), metric=metric)
    await resumed.run()

    jobs_created = len(backend._jobs)
    return {
        "completed": resumed.is_done(),
        "trials": len(trials),
        "jobs_created": jobs_created,
        "duplicated_jobs": jobs_created - len(trials),
        "best": (resumed.best().result.metrics.get(metric) if resumed.best() else None),
    }


async def experiment_a_sweep(
    trials: list[Trial],
    plans: dict[str, JobPlan],
    workdir: str | Path,
    *,
    metric: str = "score",
    max_interrupt_points: int = 4,
) -> dict[str, Any]:
    """Experiment A, systematically: interrupt at every pass boundary, not one.

    A single interrupt point proves little -- the interesting failures hide at
    specific boundaries (between submit and handle persistence, between terminal
    poll and result fetch). Sweeping every boundary and requiring all runs to
    complete with zero duplicated jobs is the framework claim under test.
    """
    workdir = Path(workdir)
    reports = []
    for point in range(1, max_interrupt_points + 1):
        run_dir = workdir / f"interrupt-at-{point}"
        run_dir.mkdir(parents=True, exist_ok=True)
        reports.append(
            {"interrupt_after_passes": point,
             **await experiment_a_interrupt(trials, plans, run_dir, metric=metric, interrupt_after_passes=point)}
        )
    return {
        "points": reports,
        "all_completed": all(r["completed"] for r in reports),
        "max_duplicated_jobs": max(r["duplicated_jobs"] for r in reports),
    }


async def experiment_wake_tradeoff(
    scripts: dict[str, JobScript],
    workdir: str | Path,
    *,
    check_interval_ms: int,
    horizon_ms: int,
    metric: str = "score",
) -> dict[str, Any]:
    """Drive a campaign on the scripted world, checking every ``check_interval_ms``
    of campaign time, and report the paired cost of that cadence.

    This is the detection-latency / wake-overhead trade-off measured directly:
    a slack cadence spends few wakes but discovers terminal jobs late (wasted
    machine time), a tight one discovers fast but pays more wakes. ``horizon_ms``
    bounds the run so a no-op script (a job that never finishes) ends the sweep
    instead of looping forever -- the loop is then expected to have spent bounded
    wakes and reached no conclusion.
    """
    clock = SimClock()
    backend = ScriptedJobBackend(clock, scripts=scripts)
    trials = [Trial(key) for key in scripts]
    ledger = Ledger(Path(workdir) / f"wake-{check_interval_ms}-ledger.json")
    campaign = Campaign("wake_tradeoff", trials, backend, ledger, metric=metric)

    wakes = 0
    while clock.now_ms() < horizon_ms:
        await campaign.step()
        wakes += 1
        if campaign.is_done():
            break
        clock.advance_ms(check_interval_ms)

    latencies = {key: backend.detection_latency_ms(key) for key in scripts}
    observed = [value for value in latencies.values() if value is not None]
    return {
        "check_interval_ms": check_interval_ms,
        "wakes": wakes,
        "polls": backend.poll_count(),
        "completed": campaign.is_done(),
        "campaign_ms_elapsed": clock.now_ms(),
        "detection_latency_ms": latencies,
        "detection_latency_mean_ms": (sum(observed) / len(observed)) if observed else None,
        "detection_latency_max_ms": max(observed) if observed else None,
    }


async def wake_tradeoff_curve(
    scripts: dict[str, JobScript],
    workdir: str | Path,
    *,
    intervals_ms: list[int],
    horizon_ms: int,
    metric: str = "score",
) -> list[dict[str, Any]]:
    """One point per cadence: the Pareto curve (wakes vs detection latency) a
    wake-policy change has to improve on."""
    return [
        await experiment_wake_tradeoff(
            scripts, workdir, check_interval_ms=interval, horizon_ms=horizon_ms, metric=metric
        )
        for interval in intervals_ms
    ]


def _trials_to_target(ledger: Ledger, metric: str, target: float) -> int | None:
    """Trials executed (in submission order) until the running best reaches
    ``target``; None if it never does."""
    count = 0
    best: float | None = None
    for rec in ledger.all():
        count += 1
        if rec.result and metric in rec.result.metrics:
            value = rec.result.metrics[metric]
            best = value if best is None else max(best, value)
            if best >= target:
                return count
    return None


async def experiment_b_paired(
    grid: list[dict[str, Any]],
    plans: dict[str, JobPlan],
    proposer: Proposer,
    workdir: str | Path,
    *,
    metric: str = "score",
    target: float,
    max_rounds: int = 8,
) -> dict[str, Any]:
    """Grid baseline vs steered run on identical scripted surfaces. The paired
    read-out: how many trials each needed to first reach ``target``."""
    workdir = Path(workdir)

    grid_backend = MockJobBackend(plans=plans)
    grid_ledger = Ledger(workdir / "expB-grid-ledger.json")
    baseline = Campaign("expB_grid", [Trial(config_key(c), dict(c)) for c in grid], grid_backend, grid_ledger, metric=metric)
    await baseline.run()

    steered_backend = MockJobBackend(plans=plans)
    steered_ledger = Ledger(workdir / "expB-steered-ledger.json")
    steered = Campaign("expB_steered", [], steered_backend, steered_ledger, metric=metric)
    await tune(steered, proposer, max_rounds=max_rounds)

    grid_n = _trials_to_target(grid_ledger, metric, target)
    steered_n = _trials_to_target(steered_ledger, metric, target)
    return {
        "target": target,
        "grid_trials_to_target": grid_n,
        "steered_trials_to_target": steered_n,
        "trials_saved": (grid_n - steered_n) if (grid_n is not None and steered_n is not None) else None,
        "grid_trials_total": len(grid_ledger.all()),
        "steered_trials_total": len(steered_ledger.all()),
    }
