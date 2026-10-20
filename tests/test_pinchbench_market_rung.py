"""The paired-2σ gate the market rung has to clear.

The gate is a pure function over two arms of graded runs, so it is tested
without a model, without a network and without a research flow. The three
properties that matter are all about **not lying**:

* a run with no grade is **skipped**, never counted as a zero;
* one pair carries no spread, so it cannot clear a standard-error bar, and
  the gate says so rather than passing on a single lucky run;
* the bar is one-sided, because the criterion asks the rung not to score
  *worse* than the baseline and a rung that is merely indistinguishable is
  not evidence that it helped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "benchmarks" / "pinchbench" / "direct"))

from market_rung import ArmResult, paired_gate, summarise  # noqa: E402


def _arm(name: str, scores: list[float | None]) -> list[ArmResult]:
    return [ArmResult(arm=name, repeat=i, score=s) for i, s in enumerate(scores, start=1)]


def test_the_gate_passes_when_the_mean_difference_clears_two_standard_errors() -> None:
    treatment = _arm("market-on", [0.9, 1.0, 0.8])
    baseline = _arm("market-off", [0.5, 0.6, 0.4])
    verdict = paired_gate(treatment, baseline, k=2.0)
    assert verdict.pairs == 3
    assert verdict.mean_difference is not None and verdict.mean_difference > 0
    assert verdict.standard_error is not None
    assert verdict.threshold is not None
    assert verdict.passed is True


def test_the_gate_fails_when_the_difference_is_inside_the_noise() -> None:
    treatment = _arm("market-on", [0.90, 0.10, 0.90, 0.10])
    baseline = _arm("market-off", [0.90, 0.10, 0.90, 0.10])
    verdict = paired_gate(treatment, baseline, k=2.0)
    assert verdict.mean_difference == pytest.approx(0.0)
    assert verdict.passed is False


def test_one_pair_cannot_clear_a_standard_error_bar() -> None:
    """A single difference has no spread, so there is no bar to clear."""
    verdict = paired_gate(_arm("market-on", [1.0]), _arm("market-off", [0.5]), k=2.0)
    assert verdict.pairs == 1
    assert verdict.standard_error is None
    assert verdict.passed is False
    assert "no spread" in verdict.note


def test_a_run_with_no_grade_is_skipped_and_not_counted_as_a_zero() -> None:
    treatment = _arm("market-on", [1.0, None])
    baseline = _arm("market-off", [0.5, 0.4])
    verdict = paired_gate(treatment, baseline, k=2.0)
    assert verdict.pairs == 1
    assert "Skipped repeats" in verdict.note


def test_a_pair_with_a_grade_missing_from_one_arm_is_skipped() -> None:
    treatment = _arm("market-on", [1.0, 0.9])
    baseline = _arm("market-off", [0.5])
    verdict = paired_gate(treatment, baseline, k=2.0)
    assert verdict.pairs == 1


def test_no_graded_pair_reports_that_the_gate_could_not_be_computed() -> None:
    verdict = paired_gate(_arm("market-on", [None]), _arm("market-off", [None]), k=2.0)
    assert verdict.pairs == 0
    assert verdict.passed is False
    assert "not a zero" in verdict.note


def test_the_summary_says_how_many_runs_were_measured() -> None:
    arms = _arm("market-on", [1.0, None]) + _arm("market-off", [0.5])
    summary = summarise(arms)
    assert summary["market-on"]["runs"] == 2
    assert summary["market-on"]["measured"] == 1
    assert summary["market-on"]["unmeasured"] == [2]
    assert summary["market-off"]["mean_score"] == pytest.approx(0.5)
