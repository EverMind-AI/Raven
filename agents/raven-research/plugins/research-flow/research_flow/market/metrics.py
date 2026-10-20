"""The stop policy's own numbers, reported apart from accuracy.

A stop policy is the rule that says *enough* - the point where the run stops
retrieving and writes. Its accuracy is the number everybody reports, and it is
the number that hides the two that matter:

* **Precision** - of the times the policy said "stop", how many were right.
  A policy that says "stop" rarely is trivially accurate and useless; the
  cheap win is to stop often, and only precision says the win was real.
* **Expected calibration error** - how far the policy's confidence is from
  how often it is right. Accuracy cannot see this: a policy that is right
  80% of the time and says "sure" every time scores 80% and is badly
  calibrated, and a caller reading 80% has no way to know which policy they
  are looking at.

So all three are reported, and **a regression in precision or in ECE fails the
gate** even when accuracy is flat or better. That direction is deliberate: a
change that makes the policy chattier raises accuracy and destroys precision,
and a gate that only watched accuracy would call that an improvement.

**No labels, no numbers.** Every metric here is computed over decisions that
carry a ground truth. A run with none reports "not measured" rather than a
perfect score, because a metric with an invented denominator is worse than
no metric - and the denominator of a stop policy's accuracy is exactly the
thing a reader cannot check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "StopDecision",
    "StopMetrics",
    "compare_stop_metrics",
    "stop_metrics",
]

#: The confidence bins the calibration error is computed over. Ten is the
#: usual choice: few enough that each bin holds something, many enough that
#: the error does not depend on the exact bin edges.
DEFAULT_BINS = 10


@dataclass(frozen=True)
class StopDecision:
    """One stop decision, with the ground truth beside it.

    ``stopped`` is the policy's answer, ``confidence`` is how sure it was,
    and ``correct`` is what actually happened - the ground truth the policy
    was trying to predict.
    """

    stopped: bool
    confidence: float
    correct: bool

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("a confidence is a probability, so it is between zero and one")


@dataclass(frozen=True)
class StopMetrics:
    """The stop policy's three numbers, kept apart."""

    accuracy: float | None = None
    precision: float | None = None
    ece: float | None = None
    decisions: int = 0
    stops: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "accuracy": self.accuracy,
            "precision": self.precision,
            "ece": self.ece,
            "decisions": self.decisions,
            "stops": self.stops,
        }

    def measured(self) -> bool:
        """Whether anything was measured at all."""
        return self.decisions > 0


def _bin_of(confidence: float, bins: int) -> int:
    """The bin a confidence falls in, on ``[0, 1]`` split into ``bins``."""
    edge = min(int(confidence * bins), bins - 1)
    return max(edge, 0)


def stop_metrics(
    decisions: Sequence[StopDecision],
    *,
    bins: int = DEFAULT_BINS,
) -> StopMetrics:
    """The three numbers, over labelled decisions.

    ``precision`` is the precision of the **stop** class: of the decisions
    where the policy said stop, the fraction that were right. It is ``None``
    when the policy never stopped - a policy that never stops has no
    precision to report, and reporting zero there would read as "every stop
    it made was wrong", which is a different and wrong claim.

    ``ece`` is the expected calibration error over ``bins`` confidence bins:
    the size-weighted gap between the policy's confidence and the share of
    decisions in that bin that were right. It is computed over **every**
    decision, including the ones where the policy carried on. A policy that
    never stops and says "50% sure" about every one of them is badly
    calibrated - it is right every time and never says so - and a metric
    that skipped those decisions would score it as perfect. Which is why
    ECE and precision are read together and never alone: a policy that
    never stops has no precision to report, and one that always stops with
    false confidence has no calibration to report.
    """
    if not decisions:
        return StopMetrics()

    total = len(decisions)
    correct = sum(1 for d in decisions if d.correct)
    stops = [d for d in decisions if d.stopped]
    accuracy = correct / total

    precision: float | None = None
    if stops:
        precision = sum(1 for d in stops if d.correct) / len(stops)

    buckets: dict[int, list[StopDecision]] = {}
    for decision in decisions:
        buckets.setdefault(_bin_of(decision.confidence, bins), []).append(decision)

    ece = 0.0
    for bucket in buckets.values():
        share = len(bucket) / total
        bucket_accuracy = sum(1 for d in bucket if d.correct) / len(bucket)
        mean_confidence = sum(d.confidence for d in bucket) / len(bucket)
        ece += share * abs(bucket_accuracy - mean_confidence)

    return StopMetrics(
        accuracy=accuracy,
        precision=precision,
        ece=ece,
        decisions=total,
        stops=len(stops),
    )


@dataclass(frozen=True)
class MetricRegression:
    """One metric's movement against the baseline, and whether it is a regression."""

    metric: str
    baseline: float | None
    current: float | None
    delta: float | None
    regressed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline": self.baseline,
            "current": self.current,
            "delta": self.delta,
            "regressed": self.regressed,
        }

    def describe(self) -> str:
        if self.baseline is None or self.current is None or self.delta is None:
            return f"{self.metric}: not measured, so no regression can be claimed"
        direction = "down" if self.delta < 0 else "up"
        return f"{self.metric}: {self.baseline:.4f} -> {self.current:.4f} ({direction} {abs(self.delta):.4f})"


def compare_stop_metrics(
    current: StopMetrics,
    baseline: StopMetrics | None,
    *,
    precision_tolerance: float = 0.0,
    ece_tolerance: float = 0.0,
) -> list[MetricRegression]:
    """The movement of precision and ECE against a baseline.

    **Accuracy is not compared.** It is reported, and it is reported apart
    from the other two, but a gate that failed on accuracy would reward a
    policy that stops more often - which is the change that trades precision
    away. The plan calls for precision and ECE to fail the gate, and that is
    what this does.

    A metric that is unmeasured on either side is reported as not measured
    rather than as a regression: a missing baseline is not a fall, and a
    missing current reading is not a rise.
    """
    if baseline is None:
        return []

    out: list[MetricRegression] = []

    if baseline.precision is None or current.precision is None:
        out.append(MetricRegression("precision", baseline.precision, current.precision, None, False))
    else:
        delta = current.precision - baseline.precision
        out.append(
            MetricRegression("precision", baseline.precision, current.precision, delta, delta < -precision_tolerance)
        )

    if baseline.ece is None or current.ece is None:
        out.append(MetricRegression("ece", baseline.ece, current.ece, None, False))
    else:
        # A rise in calibration error is the regression; a fall is an improvement.
        delta = current.ece - baseline.ece
        out.append(MetricRegression("ece", baseline.ece, current.ece, delta, delta > ece_tolerance))

    return out
