"""Focused-subset statistics for the two-stage Fisher gate (SOP §2 ⑤/⑥).

Ported from the AppWorld evolution driver so the two-stage gate is in-package
and unit-testable. The stage-1 test asks a sharp, cheap question on a candidate's
WHY subset: *is the candidate's pass-rate significantly above the baseline's on
exactly the tasks the pathology occurs?* A one-sided Fisher exact on the 2x2
trial table answers it without a full-train run.

Denominator discipline (SOP §0 hard rule): infra-contaminated trials are NOT
dropped from the denominator. Recoverable infra is salvaged upstream by the ≤2
rerun ladder (:func:`raven.evolver.orchestrator.scoring.eval_with_infra_rerun`);
only genuinely persistent infra reaches here and counts as a non-pass (fail),
kept in the denominator. Dropping infra tasks would shrink the denominator and
overestimate pass@1 (the fair-subset extrapolation trap).
"""

from __future__ import annotations

import math

from raven.evolver.orchestrator.scoring import TaskEval


def focused_counts(evals: dict[str, TaskEval], focused_ids: list[str]) -> tuple[int, int]:
    """``(passes, fails)`` trial counts over the focused subset.

    Infra-still-fail trials count as fails, not excluded — a task is never
    dropped (SOP §0). A task absent from ``evals`` (never launched) has no trials
    to count and is skipped here; the full-train denominator that governs pass@1
    lives in :func:`train_mean`.
    """
    passes = fails = 0
    for tid in focused_ids:
        ev = evals.get(tid)
        if ev is None:
            continue
        passes += ev.passes
        fails += ev.attempts - ev.passes
    return passes, fails


def train_mean(evals: dict[str, TaskEval], task_ids: list[str]) -> float:
    """Mean per-task pass@1 over ``task_ids`` with a FIXED denominator (SOP §0).

    Denominator = ``len(task_ids)`` (the task count), always. A task missing or
    all-infra contributes 0.0 — never dropped from the denominator, because
    dropping it shrinks the denominator and overestimates pass@1. infra trials
    count as non-passes via ``TaskEval.pass_rate`` (``passes / attempts``).
    """
    if not task_ids:
        return 0.0
    total = 0.0
    for t in task_ids:
        ev = evals.get(t)
        total += ev.pass_rate if ev is not None else 0.0
    return total / len(task_ids)


def fisher_one_sided(cp: int, cn: int, vp: int, vn: int) -> float:
    """One-sided Fisher exact P(candidate pass-rate > vanilla) on a 2x2.

    Table ``[[cp, cn], [vp, vn]]`` = candidate pass/fail vs vanilla pass/fail
    (trial counts). Returns 1.0 (not significant) for degenerate margins.
    """
    row1, row2 = cp + cn, vp + vn
    col1, tot = cp + vp, cp + cn + vp + vn
    if row1 == 0 or row2 == 0 or col1 == 0 or col1 == tot:
        return 1.0

    def _p(a: int) -> float:
        b, c, d = row1 - a, col1 - a, tot - col1 - (row1 - a)
        if b < 0 or c < 0 or d < 0:
            return 0.0
        return math.exp(
            math.lgamma(row1 + 1) + math.lgamma(row2 + 1)
            + math.lgamma(col1 + 1) + math.lgamma(tot - col1 + 1)
            - math.lgamma(tot + 1)
            - sum(math.lgamma(x + 1) for x in (a, b, c, d))
        )

    hi = min(row1, col1)
    return min(1.0, sum(_p(a) for a in range(cp, hi + 1)))


__all__ = [
    "focused_counts",
    "train_mean",
    "fisher_one_sided",
]
