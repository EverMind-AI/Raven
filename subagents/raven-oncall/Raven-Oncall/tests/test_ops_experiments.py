"""Tests for the experiment A/B skeletons and campaign metrics extraction."""

from __future__ import annotations

import json
from pathlib import Path

from raven.ops import JobPlan, Trial, config_key, log_event
from raven.ops.experiments import experiment_a_interrupt, experiment_b_paired
from raven.ops.metrics import campaign_metrics
from raven.ops.proposer import GridProposer, LLMProposer


async def test_experiment_a_resume_completes_without_duplicate_compute(tmp_path: Path) -> None:
    trials = [Trial("t1"), Trial("t2"), Trial("t3")]
    plans = {t.trial_id: JobPlan(succeed_after_polls=2, metrics={"score": 0.5}) for t in trials}

    report = await experiment_a_interrupt(trials, plans, tmp_path, interrupt_after_passes=1)

    assert report["completed"] is True
    assert report["duplicated_jobs"] == 0  # the framework claim: no double-spend across the crash
    assert report["best"] == 0.5


async def test_experiment_b_steered_beats_grid_on_trials_to_target(tmp_path: Path) -> None:
    # A scripted surface where the optimum sits late in grid order, so the
    # baseline pays for the whole prefix while a steered run jumps to it.
    grid = [{"k1": v} for v in (0.5, 1.0, 1.5, 2.0, 2.5)]
    scores = {0.5: 0.10, 1.0: 0.12, 1.5: 0.15, 2.0: 0.20, 2.5: 0.40}
    plans = {config_key(c): JobPlan(metrics={"score": scores[c["k1"]]}, output={"config": c}) for c in grid}

    async def complete(prompt: str) -> str:
        return '[{"k1": 2.5}]'  # scripted "smart" proposer: goes straight to the optimum

    proposer = LLMProposer(complete, objective="max score", seed=[{"k1": 1.0}], batch_size=2, max_rounds=3)
    report = await experiment_b_paired(grid, plans, proposer, tmp_path, metric="score", target=0.4)

    assert report["grid_trials_to_target"] == 5  # grid pays the whole prefix
    assert report["steered_trials_to_target"] == 2  # seed + the jump
    assert report["trials_saved"] == 3


async def test_experiment_b_grid_proposer_is_the_null_pair(tmp_path: Path) -> None:
    # Sanity: steering with the SAME grid = no savings (paired-control sanity check).
    grid = [{"k1": v} for v in (0.5, 1.0)]
    plans = {config_key(c): JobPlan(metrics={"score": 0.1 * c["k1"]}, output={"config": c}) for c in grid}

    report = await experiment_b_paired(grid, plans, GridProposer(grid), tmp_path, metric="score", target=0.1)

    assert report["grid_trials_to_target"] == report["steered_trials_to_target"]
    assert report["trials_saved"] == 0


def test_campaign_metrics_reads_the_trail(tmp_path: Path) -> None:
    log_event(tmp_path, "submit", round=0, trials=["a", "b"])
    log_event(tmp_path, "wake_scheduled", round_due=0, eta_seconds=60)
    log_event(tmp_path, "event_wake_advanced", reason="round terminal")
    log_event(tmp_path, "submit", round=1, trials=["c"])
    log_event(tmp_path, "kill", trial="c", reason="nan")
    log_event(tmp_path, "concluded", reason="user accepted")

    m = campaign_metrics(tmp_path)

    assert m["submits"] == 2
    assert m["rounds"] == 2
    assert m["wakes_scheduled"] == 1
    assert m["event_wake_advances"] == 1
    assert m["kills"] == 1
    assert m["concluded"] is True
    assert m["first_event_ts"] is not None


async def test_wake_tradeoff_pays_latency_for_fewer_wakes(tmp_path: Path) -> None:
    """The core efficiency trade-off, measured: a slack cadence spends fewer wakes
    but discovers the finished job later (wasted machine time)."""
    from raven.ops import JobScript, experiment_wake_tradeoff

    scripts = {"t1": JobScript(finish_after_ms=60_000, metrics={"score": 0.5})}  # finishes at 1 min

    tight = await experiment_wake_tradeoff(
        scripts, tmp_path, check_interval_ms=10_000, horizon_ms=3_600_000)
    slack = await experiment_wake_tradeoff(
        scripts, tmp_path, check_interval_ms=600_000, horizon_ms=3_600_000)

    assert tight["completed"] and slack["completed"]
    assert tight["detection_latency_max_ms"] < slack["detection_latency_max_ms"]  # faster detection
    assert tight["wakes"] > slack["wakes"]  # paid for with more wakes
    assert slack["detection_latency_max_ms"] >= 540_000  # slack cadence wasted ~9 min of machine time


async def test_wake_tradeoff_bounds_a_no_op_script(tmp_path: Path) -> None:
    """A no-op job (the awaited event never happens) must not hang the eval and
    must yield no conclusion -- the false-wake guard."""
    from raven.ops import JobScript, experiment_wake_tradeoff

    report = await experiment_wake_tradeoff(
        {"noop": JobScript(finish_after_ms=None)}, tmp_path,
        check_interval_ms=3_600_000, horizon_ms=3 * 86_400_000)  # hourly checks over 3 campaign days

    assert report["completed"] is False
    assert report["detection_latency_ms"] == {"noop": None}
    assert report["wakes"] <= 73  # bounded: 3 days of hourly checks, not an infinite loop


async def test_wake_tradeoff_curve_is_monotone_in_the_expected_direction(tmp_path: Path) -> None:
    from raven.ops import JobScript, wake_tradeoff_curve

    scripts = {f"t{i}": JobScript(finish_after_ms=120_000 + i * 30_000) for i in range(3)}
    curve = await wake_tradeoff_curve(
        scripts, tmp_path, intervals_ms=[15_000, 60_000, 300_000], horizon_ms=3_600_000)

    wakes = [point["wakes"] for point in curve]
    latency = [point["detection_latency_mean_ms"] for point in curve]
    assert wakes == sorted(wakes, reverse=True)  # tighter cadence -> more wakes
    assert latency == sorted(latency)  # ... and lower latency


async def test_experiment_a_sweep_interrupts_every_boundary(tmp_path: Path) -> None:
    from raven.ops import experiment_a_sweep

    trials = [Trial("t1"), Trial("t2")]
    plans = {t.trial_id: JobPlan(succeed_after_polls=3, metrics={"score": 0.4}) for t in trials}

    report = await experiment_a_sweep(trials, plans, tmp_path, max_interrupt_points=3)

    assert len(report["points"]) == 3
    assert report["all_completed"] is True  # every interrupt boundary still finishes
    assert report["max_duplicated_jobs"] == 0  # and never double-spends compute


def test_cost_metrics_pair_with_outcome(tmp_path: Path) -> None:
    """Cost must be readable next to what it bought: a per-succeeded-trial figure
    so a loop that gave up cannot look efficient."""
    from raven.ops import log_event
    from raven.ops.metrics import campaign_metrics

    (tmp_path / "ledger.json").write_text(json.dumps({"version": 1, "records": {
        "t1": {"idem_key": "t1", "status": "succeeded", "campaign": "c", "handle": None,
               "result": {"status": "succeeded", "metrics": {"score": 1.0}, "output": {}, "error": None},
               "attempts": 1, "escalated": False},
        "t2": {"idem_key": "t2", "status": "failed", "campaign": "c", "handle": None,
               "result": {"status": "failed", "metrics": {}, "output": {}, "error": "x"},
               "attempts": 1, "escalated": False},
    }}), encoding="utf-8")
    log_event(tmp_path, "wake_turn", total_tokens=800, prompt_tokens=600, completion_tokens=200,
              total_prompt_tokens=4000, total_completion_tokens=900, usage_calls=6)
    log_event(tmp_path, "wake_turn", total_tokens=1200, prompt_tokens=900, completion_tokens=300,
              total_prompt_tokens=7000, total_completion_tokens=1100, usage_calls=8)

    m = campaign_metrics(tmp_path)

    # The three original fields are each wake's last model call, unchanged.
    assert m["wake_turns"] == 2 and m["total_tokens"] == 2000
    assert m["trials_succeeded"] == 1
    # The per-trial figure is built from the summed caliber only. Off the last-call
    # fields it would read 2000.0 -- a number that looks like a price and
    # under-reports by roughly the call count.
    assert m["total_tokens_summed"] == 13000
    assert m["usage_calls"] == 14
    assert m["tokens_per_succeeded_trial"] == 13000.0


def test_cost_per_trial_is_refused_when_a_wake_predates_the_summed_caliber(tmp_path: Path) -> None:
    """A campaign whose wakes only carry the last-call fields gets no per-trial
    figure, and the reason says how many wakes were missing it. Averaging over
    the wakes that do have it would report a campaign total from a subset."""
    from raven.ops import log_event
    from raven.ops.metrics import campaign_metrics

    (tmp_path / "ledger.json").write_text(json.dumps({"version": 1, "records": {
        "t1": {"idem_key": "t1", "status": "succeeded", "campaign": "c", "handle": None,
               "result": {"status": "succeeded", "metrics": {"score": 1.0}, "output": {}, "error": None},
               "attempts": 1, "escalated": False},
    }}), encoding="utf-8")
    log_event(tmp_path, "wake_turn", total_tokens=800, prompt_tokens=600, completion_tokens=200)
    log_event(tmp_path, "wake_turn", total_tokens=1200, prompt_tokens=900, completion_tokens=300,
              total_prompt_tokens=7000, total_completion_tokens=1100, usage_calls=8)

    m = campaign_metrics(tmp_path)

    assert m["total_tokens"] == 2000, "the last-call fields still read as before"
    assert m["total_tokens_summed"] is None
    assert m["tokens_per_succeeded_trial"] is None
    assert m["wakes_with_summed_usage"] == 1 and m["wakes_total"] == 2
    assert "1/2" in m["tokens_per_succeeded_trial_unavailable_reason"]


def test_cost_per_trial_is_none_when_nothing_succeeded(tmp_path: Path) -> None:
    from raven.ops import log_event
    from raven.ops.metrics import campaign_metrics

    log_event(tmp_path, "wake_turn", total_tokens=500)
    m = campaign_metrics(tmp_path)

    assert m["total_tokens"] == 500
    assert m["tokens_per_succeeded_trial"] is None  # no false "cheap" reading
