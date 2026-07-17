"""Bandit-on-tasks scheduler.

Selects an informative subset of tasks to evaluate each new harness
candidate on, rather than running the full benchmark every time.

The scheduler maintains per-task state across candidate evaluations:

- ``n_trials``  — how many candidates have run this task
- ``successes``  — how many of those passed
- ``per_candidate``  — task_id → {candidate_id → pass/fail} so we can
  compute per-task variance across candidates (= discrimination power)

For each new candidate, :meth:`choose` returns ``n`` tasks scored by
**information value × uncertainty bonus**:

- information value = task's posterior variance across already-seen
  candidates (high variance = strong discriminator)
- uncertainty bonus = ``1/sqrt(n_trials+1)`` UCB-style, so under-tried
  tasks get a fair shot in early rounds

This is a quality-diversity flavoured variant of best-arm identification
(Even-Dar et al. 2006, Audibert & Bubeck 2010). It is **not** classical
UCB — UCB would maximize expected reward; we want to maximize
**ranking information**, which is captured by between-candidate variance.

Algorithmic notes for the paper:

- Beta(1+s, 1+f) posterior per (task, candidate) cell — Laplace prior
- Successive elimination by ``mark_resolved``: once a task is "decided"
  (e.g., always-pass / always-fail across many candidates), it drops
  out of future ``choose`` rounds
- ``rng`` is seedable for reproducibility (paper requirement)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class TaskStats:
    """Per-task state accumulated across candidate evaluations.

    The ``per_candidate`` map is the source of truth for variance
    estimation: variance is computed across the recorded outcomes,
    so a task that always passes / always fails has variance ≈ 0
    and naturally gets de-prioritized.

    ``resolved`` is a sticky drop-out flag, set by
    :meth:`BanditTaskScheduler.mark_resolved` when the task is no
    longer informative (e.g. trivially-easy or trivially-hard across
    every candidate seen so far). Resolved tasks return to play only
    if explicitly un-resolved.
    """

    task_id: str
    n_trials: int = 0
    successes: int = 0
    per_candidate: dict[str, bool] = field(default_factory=dict)
    resolved: bool = False

    @property
    def empirical_pass_rate(self) -> float:
        """``successes / n_trials``, or 0.5 if never tried (uninformative prior)."""
        if self.n_trials == 0:
            return 0.5
        return self.successes / self.n_trials

    @property
    def variance_across_candidates(self) -> float:
        """Variance of pass outcomes across distinct candidates seen.

        Returns 0.25 (max for Bernoulli) when fewer than 2 candidates
        have run this task — this default is the "cold start exploration
        bonus": new tasks look maximally informative until proven
        otherwise.

        Once ≥2 candidates contribute, the variance is computed
        empirically: ``p * (1 - p)`` where ``p`` is the fraction of
        candidates that passed. A task all candidates pass (or all fail)
        gets variance 0 and is naturally deprioritized.
        """
        n = len(self.per_candidate)
        if n < 2:
            return 0.25
        passes = sum(1 for v in self.per_candidate.values() if v)
        p = passes / n
        return p * (1.0 - p)


class BanditTaskScheduler:
    """Schedule which tasks to evaluate the next harness candidate on.

    Typical use:

    >>> scheduler = BanditTaskScheduler(["task_001", "task_002", ...])
    >>> # for each new candidate node N:
    >>> task_subset = scheduler.choose(n=30, candidate_id=N.node_id)
    >>> results = run_evaluation(N, task_subset)  # {task_id: bool}
    >>> scheduler.update(N.node_id, results)

    The internal posterior is Beta-Bernoulli with Laplace prior; the
    selection score combines per-task variance (discrimination power)
    with a UCB-style uncertainty bonus over ``n_trials``.

    Parameters
    ----------
    all_task_ids
        The complete pool of available task ids. ``choose`` returns a
        subset of this pool.
    prior
        Optional ``{task_id: prior_pass_rate}`` to seed initial variance
        estimates. Useful when porting between benchmarks or warm-starting
        from historical runs.
    exploration_weight
        Scales the UCB-style uncertainty bonus. Larger value → favours
        under-tried tasks more aggressively. Default 1.0 matches standard
        UCB1 constant.
    rng_seed
        Seed for the internal RNG used to break ties and inject small
        exploration noise. Set this for reproducible paper experiments.
    """

    def __init__(
        self,
        all_task_ids: Iterable[str],
        *,
        prior: dict[str, float] | None = None,
        exploration_weight: float = 1.0,
        rng_seed: int | None = None,
    ) -> None:
        self.tasks: dict[str, TaskStats] = {tid: TaskStats(task_id=tid) for tid in all_task_ids}
        if not self.tasks:
            raise ValueError("BanditTaskScheduler requires at least one task id")
        self._exploration_weight = exploration_weight
        self._rng = random.Random(rng_seed)
        if prior:
            self._apply_prior(prior)

    def _apply_prior(self, prior: dict[str, float]) -> None:
        """Seed variance estimates from a prior pass-rate map.

        Each prior entry is treated as one "synthetic candidate" outcome
        — recorded under candidate id ``"__prior__"`` so the variance
        machinery picks it up. A prior entry alone does not produce
        variance (variance needs ≥2 distinct candidates), but it shifts
        what subsequent real candidates need to deviate from to look
        informative.
        """
        for task_id, p in prior.items():
            if task_id not in self.tasks:
                continue
            # Treat prior as a single synthetic observation
            self.tasks[task_id].per_candidate["__prior__"] = bool(p >= 0.5)
            self.tasks[task_id].n_trials += 1
            if p >= 0.5:
                self.tasks[task_id].successes += 1

    def choose(self, n: int = 30, *, candidate_id: str | None = None) -> list[str]:
        """Select ``n`` tasks to evaluate the next candidate on.

        Tasks already seen by ``candidate_id`` (if provided) are excluded
        — we don't pay to re-evaluate the same (task, candidate) pair.

        If fewer than ``n`` active (non-resolved, non-already-seen) tasks
        exist, returns whatever is available.

        Scoring per task::

            score = variance_across_candidates
                  + exploration_weight * 1/sqrt(n_trials + 1)
                  + tiny_random_jitter

        Top-``n`` by score wins. The random jitter is small enough not
        to flip clearly-better tasks but breaks ties deterministically
        given the seed.
        """
        if n <= 0:
            return []
        candidates: list[tuple[float, str]] = []
        for stats in self.tasks.values():
            if stats.resolved:
                continue
            if candidate_id is not None and candidate_id in stats.per_candidate:
                continue
            uncertainty = self._exploration_weight / math.sqrt(stats.n_trials + 1)
            jitter = self._rng.random() * 1e-6
            score = stats.variance_across_candidates + uncertainty + jitter
            candidates.append((score, stats.task_id))
        candidates.sort(reverse=True)
        return [task_id for _, task_id in candidates[:n]]

    def update(self, candidate_id: str, results: dict[str, bool]) -> None:
        """Record per-task results for one candidate evaluation.

        Updates ``n_trials``, ``successes``, and ``per_candidate``. The
        variance estimate is recomputed lazily via the property — no
        explicit recalculation needed here.

        Unknown ``task_id`` keys in ``results`` are silently skipped so
        the scheduler tolerates results that include tasks added later
        (e.g. when the task pool expands between rounds).
        """
        for task_id, passed in results.items():
            stats = self.tasks.get(task_id)
            if stats is None:
                continue
            # Re-running the same candidate on the same task is a no-op
            # (it would double-count successes). Caller should de-dup,
            # but we defend here too.
            if candidate_id in stats.per_candidate:
                continue
            stats.per_candidate[candidate_id] = bool(passed)
            stats.n_trials += 1
            if passed:
                stats.successes += 1

    def get_rank_estimate(self) -> list[tuple[str, float]]:
        """Return tasks ranked by Beta-Bernoulli posterior mean pass rate.

        Posterior mean is ``(successes + 1) / (n_trials + 2)`` (Laplace
        smoothing). Useful for diagnostic / analysis, not for the
        ``choose`` decision (which uses variance, not mean).

        Sorted descending — easiest tasks first.
        """
        rows: list[tuple[str, float]] = []
        for stats in self.tasks.values():
            mean = (stats.successes + 1) / (stats.n_trials + 2)
            rows.append((stats.task_id, mean))
        rows.sort(key=lambda r: -r[1])
        return rows

    def mark_resolved(self, task_id: str, resolved: bool = True) -> None:
        """Manually flag a task as resolved (skipped by ``choose``).

        Called by the harness when, e.g., a task has been seen by ≥K
        candidates with unanimous outcome — further evaluation wastes
        budget. Passing ``resolved=False`` reverts the flag.
        """
        stats = self.tasks.get(task_id)
        if stats is None:
            return
        stats.resolved = resolved

    def active_task_count(self) -> int:
        """Number of tasks currently eligible for ``choose``."""
        return sum(1 for s in self.tasks.values() if not s.resolved)

    def __repr__(self) -> str:
        active = self.active_task_count()
        total = len(self.tasks)
        return f"BanditTaskScheduler(total={total}, active={active}, exploration_weight={self._exploration_weight})"
