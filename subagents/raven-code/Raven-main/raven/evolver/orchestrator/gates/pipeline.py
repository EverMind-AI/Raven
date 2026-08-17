"""The three-shield gate as a composable pipeline (SOP §2 ⑥).

Order matters and each shield narrows the task set the next one judges:

1. **Gate-f (measurement validity).** Report tasks whose eval was contaminated by
   an infrastructure failure on *either* arm, but do NOT drop them from the
   denominator (SOP §0 hard rule: detect -> rerun <=2 -> still-fail counts as 0,
   kept in the denominator; dropping shrinks the denominator and overestimates
   pass@1). Recoverable infra is salvaged upstream by the rerun ladder
   (:func:`raven.evolver.orchestrator.scoring.eval_with_infra_rerun`); by the
   time run_gates sees the evals, a still-infra trial is scored as a non-pass via
   ``pass_rate``. Infra is read from ``infra_attempts`` when present (AppWorld
   surfaces it; benches without it report nothing here).
2. **Gate-b (attribution).** Only credit the candidate on tasks where its
   mechanism actually fired — a patch can't get credit for a task it never
   touched. The set of fired tasks comes from an injectable source (the
   activation ledger / beacon); when none is given this shield is a no-op.
3. **Gate2 (significance).** Paired lift on the surviving tasks: navigator
   promotion (mean > vanilla) plus a separate credited-2σ label.

Keeping this a pure function over eval maps makes it bench-agnostic and unit
testable; the loop calls it once per candidate after the confirm eval.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from raven.evolver.orchestrator.gates.paired import PairedResult, paired_lift
from raven.evolver.orchestrator.scoring import TaskEval


@dataclass
class GateResult:
    """Combined verdict of the three-shield pipeline for one candidate.

    ``infra_contaminated`` is reported for audit only — those tasks stay in the
    scoring denominator (SOP §0), scored by their ``pass_rate`` with infra trials
    as non-passes. Only ``unfired_excluded`` (Gate-b attribution) narrows the set.
    """

    promoted: bool
    paired: PairedResult | None
    eligible_tasks: list[str]
    infra_contaminated: list[str] = field(default_factory=list)
    unfired_excluded: list[str] = field(default_factory=list)


def _infra_attempts(ev: TaskEval | None) -> int:
    """Infra-trial count for an eval, or 0 when absent (SWE / no-bench leave 0)."""
    return ev.infra_attempts if ev is not None else 0


def run_gates(
    *,
    candidate_evals: dict[str, TaskEval],
    control_evals: dict[str, TaskEval],
    task_ids: list[str],
    z_threshold: float = 2.0,
    fired_tasks: set[str] | None = None,
    infra_threshold: int = 1,
) -> GateResult:
    """Run Gate-f -> Gate-b -> Gate2 and return the combined verdict.

    ``fired_tasks`` (Gate-b) restricts attribution when provided; ``None`` skips
    that shield. ``infra_threshold`` is the per-arm infra-trial count at or above
    which a task is *reported* as infra-contaminated (it is NOT dropped — SOP §0
    keeps it in the denominator scored 0). Only Gate-b can leave nothing to
    measure; then the candidate is not promoted and ``paired`` is None.
    """
    infra_contaminated = [
        t
        for t in task_ids
        if _infra_attempts(candidate_evals.get(t)) >= infra_threshold
        or _infra_attempts(control_evals.get(t)) >= infra_threshold
    ]
    # Gate-f no longer excludes: infra tasks stay in the denominator (scored 0
    # via pass_rate). Only Gate-b attribution narrows the eligible set.
    eligible = list(task_ids)
    unfired_excluded: list[str] = []
    if fired_tasks is not None:
        unfired_excluded = [t for t in eligible if t not in fired_tasks]
        eligible = [t for t in eligible if t in fired_tasks]

    if not eligible:
        return GateResult(
            promoted=False,
            paired=None,
            eligible_tasks=[],
            infra_contaminated=infra_contaminated,
            unfired_excluded=unfired_excluded,
        )

    paired = paired_lift(
        candidate_evals=candidate_evals,
        control_evals=control_evals,
        task_ids=eligible,
        z_threshold=z_threshold,
    )
    return GateResult(
        promoted=paired.promoted,
        paired=paired,
        eligible_tasks=eligible,
        infra_contaminated=infra_contaminated,
        unfired_excluded=unfired_excluded,
    )


__all__ = ["GateResult", "run_gates"]
