"""Step ⑤a — the K=1 anchor screen with a wide-pass verdict.

The screen is deliberately wide (wide-pass): a candidate advances to the full-set
confirm unless its anchor-mean pass@1 falls *clearly* below vanilla's. The SOP
describes three buckets, but operationally there is a single cut:

- clear win        (margin >= +cull_threshold)  -> confirm
- within the band  (|margin| <  cull_threshold)  -> confirm  (a slightly-low
                                                    anchor mean is not the
                                                    full-set mean; don't cull)
- clear loss       (margin <= -cull_threshold)  -> pruned_at_screen

``cull_threshold`` is ``cull_sigma_mult * sigma_screen`` from ``select_anchor``,
i.e. sized off the *same* vanilla thick ledger the anchor was drawn from, so the
band reflects genuine K=1 anchor-mean sampling noise rather than a guess.

Vanilla's anchor mean is read from the cold-start thick-ledger per-task rates
(K=3, the fixed baseline the funnel always compares against); the candidate's is
the K=1 screen run. Comparing a noisy K=1 mean against a tighter K=3 mean is
exactly why the band is wide.
"""

from __future__ import annotations

from dataclasses import dataclass

from raven.evolver.orchestrator.scoring import (
    TaskEval,
    anchor_mean_pass_rate,
)
from raven.evolver.scheduler.anchor_selection import AnchorSelection


@dataclass(frozen=True)
class ScreenResult:
    """Verdict of the anchor screen for one candidate."""

    candidate_mean: float
    vanilla_mean: float
    cull_threshold: float
    sigma_screen: float
    passes_to_confirm: bool
    bucket: str  # "clear_win" | "within_band" | "cull" — for the log/ledger

    @property
    def margin(self) -> float:
        """Candidate anchor mean minus vanilla anchor mean (pp as a fraction)."""
        return self.candidate_mean - self.vanilla_mean


def _vanilla_anchor_mean(vanilla_evals: dict[str, TaskEval], anchor_task_ids: list[str]) -> float:
    """Mean vanilla per-task pass rate over the anchor subset (control arm).

    A control arm that is a fresh eval (e.g. a same-session paired baseline) can
    legitimately be missing an anchor task that failed to launch, so a missing id
    contributes 0.0 — symmetric with the candidate side (``anchor_mean_pass_rate``)
    and safe for a wide-pass screen (a lower vanilla mean only advances more
    candidates). Frozen ledger baselines have every anchor id, so this is a no-op
    there.
    """
    total = 0.0
    for task_id in anchor_task_ids:
        ev = vanilla_evals.get(task_id)
        total += ev.pass_rate if ev is not None else 0.0
    return total / len(anchor_task_ids)


def screen_candidate(
    *,
    candidate_evals: dict[str, TaskEval],
    anchor: AnchorSelection,
    vanilla_evals: dict[str, TaskEval],
) -> ScreenResult:
    """Apply the wide-pass screen cut to one candidate's K=1 anchor eval."""
    candidate_mean = anchor_mean_pass_rate(candidate_evals, anchor.task_ids)
    vanilla_mean = _vanilla_anchor_mean(vanilla_evals, anchor.task_ids)
    margin = candidate_mean - vanilla_mean
    cull = anchor.cull_threshold

    if margin >= cull:
        bucket = "clear_win"
    elif margin > -cull:
        bucket = "within_band"
    else:
        bucket = "cull"

    return ScreenResult(
        candidate_mean=candidate_mean,
        vanilla_mean=vanilla_mean,
        cull_threshold=cull,
        sigma_screen=anchor.sigma_screen,
        passes_to_confirm=(bucket != "cull"),
        bucket=bucket,
    )


__all__ = ["ScreenResult", "screen_candidate"]
