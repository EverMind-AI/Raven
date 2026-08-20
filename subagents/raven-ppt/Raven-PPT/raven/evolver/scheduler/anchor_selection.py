"""Anchor-subset selection for the per-round evolution screen.

:func:`select_anchor` composes the cold-start thick ledger (vanilla K=3 over
the train pool) into the small anchor subset used for K=1 screening, and emits
the screen cull threshold derived from the *same* ledger.

Why one call returns both the task list and the threshold: the anchor tasks are
picked by per-task Bernoulli variance ``p*(1-p)``, and the screen noise band
``sigma_screen`` is ``sqrt((1/n^2) * sum p*(1-p))`` over exactly those tasks.
Same ledger, same per-task rates -> selecting the subset and sizing its jitter
are one computation, not two. ``sigma_screen`` is the *K=1 anchor-mean* sampling
sigma (large/loose: K=1 + small n + deliberately high-variance tasks); it is a
different quantity from the full-set K=3 paired sigma used for credited
significance.

The subset mixes three roles (cold start, round 1):

- ``sentinel``   : STABLE_PASS tasks; catch a candidate that breaks easy tasks.
- ``icebreaker`` : STABLE_FAIL tasks; room for a mechanism to rescue a hard one.
- ``borderline`` : BORDERLINE_* tasks; the high-variance discriminators that
  dominate ``sigma_screen``.

Round 2+: pass an ``affinity`` map (``{task_id: mechanism-fire density}``, e.g.
from ``affinity_task_picker.per_task_density``) to bias icebreaker slots toward
tasks where the round's mechanism actually fires. Without it the selection is
ledger-only and mechanism-agnostic.

Selection is deterministic: ties break by ascending ``task_id`` so the same
ledger always yields the same anchor (reproducible paper experiments).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from raven.evolver.analysis.stability_bucket import (
    StabilityBucket,
    TaskStability,
    compute_stability,
)

_BORDERLINE_BUCKETS = (
    StabilityBucket.BORDERLINE_2_3,
    StabilityBucket.BORDERLINE_1_3,
)


@dataclass(frozen=True)
class AnchorTask:
    """One picked anchor task with the ledger stats that placed it."""

    task_id: str
    role: str  # "sentinel" | "icebreaker" | "borderline"
    pass_rate: float
    variance: float


@dataclass(frozen=True)
class AnchorSelection:
    """Result of :func:`select_anchor`.

    ``task_ids`` is the screen subset; ``cull_threshold`` is the pp margin
    below vanilla beyond which a candidate is dropped at screen (a candidate
    whose anchor-mean pass@1 is more than ``cull_threshold`` under vanilla's
    anchor-mean is pruned; anything closer goes to the full-set confirm).
    """

    task_ids: list[str]
    sigma_screen: float
    cull_threshold: float
    tasks: list[AnchorTask]
    shortfalls: dict[str, int]

    def __len__(self) -> int:
        return len(self.task_ids)


def _variance(stab: TaskStability) -> float:
    """Plug-in Bernoulli variance ``p*(1-p)`` from the ledger pass rate."""
    if stab.attempts == 0:
        return 0.0
    p = stab.passes / stab.attempts
    return p * (1.0 - p)


def _pass_rate(stab: TaskStability) -> float:
    if stab.attempts == 0:
        return 0.0
    return stab.passes / stab.attempts


def select_anchor(
    ledger_dir: str | Path,
    *,
    n_sentinel: int = 3,
    n_icebreaker: int = 5,
    n_borderline: int = 7,
    cull_sigma_mult: float = 1.5,
    affinity: dict[str, float] | None = None,
) -> AnchorSelection:
    """Pick the anchor screen subset from a vanilla K=3 thick ledger.

    Parameters
    ----------
    ledger_dir
        Path accepted by
        :func:`raven.evolver.analysis.stability_bucket.compute_stability`
        (a legacy jobs dir or its dated subdir) holding the vanilla K=3 run
        over the train pool. Must be train-only — the anchor is part of the
        evolution decision, so a sealed test task entering it is a leak.
    n_sentinel, n_icebreaker, n_borderline
        Slot budget per role. Defaults give a ~15-task anchor. If a bucket has
        fewer tasks than its budget the slot count is recorded in
        ``shortfalls`` rather than raising.
    cull_sigma_mult
        Multiplier on ``sigma_screen`` for the cull threshold (default 1.5;
        wide-pass screen — only egregious losers are dropped at screen).
    affinity
        Optional ``{task_id: density}`` to rank icebreaker (STABLE_FAIL) slots
        by mechanism-fire density (round 2+). Tasks absent from the map score
        0. Without it icebreakers are taken in ``task_id`` order.

    Returns
    -------
    AnchorSelection
        With ``task_ids``, ``sigma_screen``, ``cull_threshold``, the per-task
        breakdown, and per-role ``shortfalls``.
    """
    stability = compute_stability(ledger_dir)
    if not stability:
        raise ValueError(f"no tasks found in ledger dir: {ledger_dir}")

    by_bucket: dict[StabilityBucket, list[TaskStability]] = defaultdict(list)
    for stab in stability.values():
        by_bucket[stab.bucket].append(stab)

    affinity = affinity or {}
    picked: list[AnchorTask] = []
    shortfalls: dict[str, int] = {}

    def _record(role: str, stats: list[TaskStability], budget: int, key) -> None:
        chosen = sorted(stats, key=key)[:budget]
        for stab in chosen:
            picked.append(
                AnchorTask(
                    task_id=stab.task_id,
                    role=role,
                    pass_rate=_pass_rate(stab),
                    variance=_variance(stab),
                )
            )
        missing = budget - len(chosen)
        if missing > 0:
            shortfalls[role] = missing

    # sentinels / icebreakers carry no within-task variance (p in {0, 1});
    # order them by affinity (icebreakers, round 2+) then task_id for determinism.
    _record(
        "sentinel",
        by_bucket[StabilityBucket.STABLE_PASS],
        n_sentinel,
        key=lambda s: s.task_id,
    )
    _record(
        "icebreaker",
        by_bucket[StabilityBucket.STABLE_FAIL],
        n_icebreaker,
        key=lambda s: (-affinity.get(s.task_id, 0.0), s.task_id),
    )
    # borderline tasks are the discriminators: rank by variance desc, then id.
    borderline_pool = [stab for bucket in _BORDERLINE_BUCKETS for stab in by_bucket[bucket]]
    _record(
        "borderline",
        borderline_pool,
        n_borderline,
        key=lambda s: (-_variance(s), s.task_id),
    )

    n = len(picked)
    if n == 0:
        raise ValueError(f"anchor selection produced 0 tasks from {ledger_dir} (empty buckets for every role?)")
    sigma_screen = math.sqrt(sum(t.variance for t in picked) / (n * n))
    cull_threshold = cull_sigma_mult * sigma_screen

    return AnchorSelection(
        task_ids=[t.task_id for t in picked],
        sigma_screen=sigma_screen,
        cull_threshold=cull_threshold,
        tasks=picked,
        shortfalls=shortfalls,
    )


def simple_anchor(
    stability: dict[str, TaskStability],
    *,
    task_ids: list[str] | None = None,
    cull_sigma_mult: float = 1.5,
) -> AnchorSelection:
    """Build an ``AnchorSelection`` over ``task_ids`` (or all) from a stability map.

    A lightweight alternative to :func:`select_anchor` for benches where a K=1
    cold start yields no BORDERLINE bucket (so the sentinel/icebreaker/borderline
    split is degenerate). ``sigma_screen`` uses the same Bernoulli-variance
    formula ``sqrt((1/n^2) * sum p*(1-p))``, so the wide-pass cull threshold is
    sized identically to ``select_anchor``. Takes an already-built stability map
    (not a ledger dir) so any bench that can produce ``{task_id: TaskStability}``
    reuses it — no bench-specific format knowledge lives here.
    """
    ids = sorted(task_ids if task_ids is not None else stability)
    if not ids:
        raise ValueError("simple_anchor requires at least one task")
    tasks: list[AnchorTask] = []
    for tid in ids:
        st = stability[tid]
        p = st.passes / st.attempts if st.attempts else 0.0
        role = (
            "sentinel"
            if st.bucket == StabilityBucket.STABLE_PASS
            else "icebreaker"
            if st.bucket == StabilityBucket.STABLE_FAIL
            else "borderline"
        )
        tasks.append(AnchorTask(task_id=tid, role=role, pass_rate=p, variance=p * (1 - p)))
    n = len(tasks)
    sigma_screen = math.sqrt(sum(t.variance for t in tasks) / (n * n))
    return AnchorSelection(
        task_ids=ids,
        sigma_screen=sigma_screen,
        cull_threshold=cull_sigma_mult * sigma_screen,
        tasks=tasks,
        shortfalls={},
    )
