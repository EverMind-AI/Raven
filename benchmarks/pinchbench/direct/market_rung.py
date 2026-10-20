"""The market rung's paired arms, and the gate they have to clear.

W4's criterion (h) is *"scores ≥ baseline on the pinchbench market-research
task under the paired-2σ gate"*. This is the machinery for it: two arms of
the same task, graded by the same grader, compared **pairwise**.

**Why pairwise.** The task's score moves with the model, the clock and the
pages the web served that minute. Two unpaired runs differ by all three at
once, so a difference between them measures the run, not the rung. Pairing
removes everything that both members of a pair share, and what is left is
the rung.

**The gate.** For each pair the difference is taken, and the mean
difference has to clear *k* standard errors of that difference:

    d_i   = treatment_i - baseline_i
    mean  = mean(d)
    se    = stdev(d) / sqrt(n)
    pass  = mean > k * se

With ``k = 2`` the rung has to be worth more than two standard errors of
the paired difference to ship. The test is deliberately one-sided: the
criterion says the rung must not score *worse* than the baseline, and a
rung that is merely indistinguishable is not evidence that it helped.

**An arm that could not be measured is not a zero.** A run that died, that
timed out, or that produced no grade is reported as missing, and the gate
is computed over the pairs that both members of which are present. A gate
computed over a subset has to say so, because a subset chosen after seeing
the scores is not a random sample.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence

#: The multiplier the criterion names. Two standard errors of the paired
#: difference, one-sided.
DEFAULT_K = 2.0


@dataclass(frozen=True)
class ArmResult:
    """One graded run: which arm, which repeat, and what it scored."""

    arm: str
    repeat: int
    score: float | None
    detail: str = ""

    @property
    def measured(self) -> bool:
        return self.score is not None


@dataclass(frozen=True)
class PairedVerdict:
    """What the gate said, and on how much evidence."""

    pairs: int
    mean_difference: float | None
    standard_error: float | None
    threshold: float | None
    passed: bool
    note: str

    def to_dict(self) -> dict:
        return {
            "pairs": self.pairs,
            "mean_difference": self.mean_difference,
            "standard_error": self.standard_error,
            "threshold": self.threshold,
            "passed": self.passed,
            "note": self.note,
        }


def paired_gate(
    treatment: Sequence[ArmResult],
    baseline: Sequence[ArmResult],
    *,
    k: float = DEFAULT_K,
) -> PairedVerdict:
    """Compare two arms pairwise and say whether the treatment cleared the bar.

    A repeat with no grade in either arm is **skipped**, not counted as a
    zero: an unmeasured run is not a run that scored nothing.
    """
    by_repeat_t = {r.repeat: r for r in treatment}
    by_repeat_b = {r.repeat: r for r in baseline}

    differences: list[float] = []
    skipped: list[int] = []
    for repeat in sorted(set(by_repeat_t) | set(by_repeat_b)):
        t = by_repeat_t.get(repeat)
        b = by_repeat_b.get(repeat)
        if t is None or b is None or not t.measured or not b.measured:
            skipped.append(repeat)
            continue
        differences.append(float(t.score) - float(b.score))  # type: ignore[arg-type]

    if not differences:
        return PairedVerdict(
            pairs=0,
            mean_difference=None,
            standard_error=None,
            threshold=None,
            passed=False,
            note=(
                "no pair had a grade in both arms, so the gate could not be computed. "
                "An unmeasured arm is not a zero."
                + (f" Skipped repeats: {skipped}." if skipped else "")
            ),
        )

    mean = statistics.fmean(differences)
    if len(differences) == 1:
        # One pair carries no spread, so there is no standard error to clear.
        return PairedVerdict(
            pairs=1,
            mean_difference=mean,
            standard_error=None,
            threshold=None,
            passed=False,
            note=(
                "one pair only: a paired difference with no spread cannot clear a "
                "standard-error bar, so the gate is not satisfied. The mean difference "
                f"is {mean:+.4f}."
                + (f" Skipped repeats: {skipped}." if skipped else "")
            ),
        )

    stdev = statistics.stdev(differences)
    se = stdev / math.sqrt(len(differences))
    threshold = k * se
    passed = mean > threshold
    note = (
        f"{len(differences)} paired run(s); mean difference {mean:+.4f}, "
        f"standard error {se:.4f}, bar {k:g} x SE = {threshold:+.4f}."
    )
    if skipped:
        note += f" Skipped repeats (no grade in one arm): {skipped}."
    return PairedVerdict(
        pairs=len(differences),
        mean_difference=mean,
        standard_error=se,
        threshold=threshold,
        passed=passed,
        note=note,
    )


def summarise(arms: Iterable[ArmResult]) -> dict:
    """A per-arm summary: how many, how measured, and the mean score."""
    by_arm: dict[str, list[ArmResult]] = {}
    for result in arms:
        by_arm.setdefault(result.arm, []).append(result)
    out: dict[str, dict] = {}
    for arm, results in sorted(by_arm.items()):
        scores = [r.score for r in results if r.measured]
        out[arm] = {
            "runs": len(results),
            "measured": len(scores),
            "mean_score": statistics.fmean(scores) if scores else None,
            "unmeasured": [r.repeat for r in results if not r.measured],
        }
    return out


__all__ = ["ArmResult", "DEFAULT_K", "PairedVerdict", "paired_gate", "summarise"]
