"""Gate2 — paired lift + 2σ significance (a generalisation of round7_paired).

The scratchpad ``round7_paired.py`` hard-coded its anchor/explore task lists and
its two arms. This is the same statistics with the task set and the two arms
passed in, so it works for any round and any domain.

Pairing is what makes the test sensitive: comparing candidate and vanilla on the
*same* tasks removes between-task difficulty, so the standard error is the
spread of the per-task differences, not the spread of raw pass rates. For each
task we take the per-task pass-rate difference ``d_i = rate_candidate,i -
rate_vanilla,i`` (K=3 rates), and test whether the mean difference is far enough
from zero:

    mean_lift = mean(d_i)
    se        = stdev(d_i) / sqrt(n)          # paired standard error
    z         = mean_lift / se

Promotion to bank (SOP §2 ⑥ Gate2) is the *navigator* condition alone: the
candidate's mean pass@1 beats vanilla on the shared train set. The paired 2σ
test is a separate *credited-significance* label (``credited_2sigma``) reported
alongside — it says whether the lift is statistically significant, not whether
the candidate banks. This matches the manual round-3 decision, where
budgetnudge banked on +6.4pp over vanilla even though its paired z (1.71) fell
short of 2σ. A candidate that improves every shared task identically has
``se == 0``; that deterministic win is reported as ``z = inf``.

Whether a banked candidate becomes the next parent, or is pruned for a
qualitative reason (e.g. anchor/full-set sign-flip signalling interference), is
a semantic step ⑦ decision layered on top of this gate, not part of it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, stdev

from raven.evolver.orchestrator.scoring import TaskEval


@dataclass(frozen=True)
class PairedResult:
    """Outcome of the paired lift test between a candidate and a control arm."""

    n_tasks: int
    candidate_mean: float
    control_mean: float
    mean_lift: float
    se: float
    z: float
    z_threshold: float
    promoted: bool  # navigator: candidate_mean > control_mean -> banks
    credited_2sigma: bool  # separate significance label: z >= z_threshold


def _rate(evals: dict[str, TaskEval], task_id: str) -> float:
    ev = evals.get(task_id)
    return ev.pass_rate if ev is not None else 0.0


def paired_lift(
    *,
    candidate_evals: dict[str, TaskEval],
    control_evals: dict[str, TaskEval],
    task_ids: list[str],
    z_threshold: float = 2.0,
) -> PairedResult:
    """Paired lift + 2σ test over ``task_ids`` (candidate arm vs control arm).

    ``task_ids`` is the shared task set both arms were evaluated on (the full
    train set for a confirm). A task missing from an arm scores 0.0 for that arm
    — a candidate that failed to launch is not rewarded for the gap.
    """
    if not task_ids:
        raise ValueError("paired_lift requires a non-empty task list")

    diffs = [_rate(candidate_evals, t) - _rate(control_evals, t) for t in task_ids]
    candidate_mean = mean(_rate(candidate_evals, t) for t in task_ids)
    control_mean = mean(_rate(control_evals, t) for t in task_ids)
    mean_lift = mean(diffs)

    n = len(task_ids)
    se = stdev(diffs) / math.sqrt(n) if n > 1 else 0.0
    if se == 0.0:
        z = 0.0 if mean_lift == 0.0 else math.copysign(math.inf, mean_lift)
    else:
        z = mean_lift / se

    promoted = candidate_mean > control_mean  # navigator: banks on beating vanilla
    credited_2sigma = z >= z_threshold

    return PairedResult(
        n_tasks=n,
        candidate_mean=candidate_mean,
        control_mean=control_mean,
        mean_lift=mean_lift,
        se=se,
        z=z,
        z_threshold=z_threshold,
        promoted=promoted,
        credited_2sigma=credited_2sigma,
    )


__all__ = ["PairedResult", "paired_lift"]
