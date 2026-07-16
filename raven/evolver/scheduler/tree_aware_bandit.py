"""Tree-aware bandit-on-tasks (spec §18 Option F.1).

Hierarchical Bernoulli model with **single-source ancestry kernel**:

    p̂(t, v_new)  =  Σ_a  w(d(v_new, a)) × outcome(a, t)
                    ─────────────────────────────────────
                    Σ_a  w(d(v_new, a))

    w(d)  =  exp(-λ × d)     # ancestry distance kernel

Where:

- ``a`` ranges over all observed ancestors of ``v_new`` (the new
  candidate whose task subset we're selecting)
- ``d(v_new, a)`` is the ancestry distance in the evolution tree
- ``outcome(a, t)`` is each recorded (a, t) outcome ∈ {0, 1}
- Multi-attempt nodes (root k=3 paired) contribute multiple outcomes;
  descendant k=1 contributes one

Score per task ``t`` for next subset pick:

    score(t)  =  Var_estimate(t)  +  γ × exploration_bonus(t)  +  jitter

    Var_estimate(t)        =  p̂(t) × (1 − p̂(t))         # weighted Bernoulli
    exploration_bonus(t)   =  1 / √(n_observations + 1)    # standard
    jitter                  =  ε ∈ [0, 1e-9)               # tie-break for reproducibility

Multi-source extensions (cell prior, trajectory feature prior, per-task
λ learning) deferred to Option F.2 → F.4 per spec §18.6.3-6. v0.1 keeps
the single ancestry kernel so the core mechanics + sample-complexity
bound (§18.6.6) can land first.

Algorithm family: this mirrors the Beta-Bernoulli + UCB style of
:class:`raven.evolver.scheduler.bandit_tasks.BanditTaskScheduler` —
same exploration-bonus formula, same jitter strategy — extended with
ancestry-weighted posterior. Code structure parallels intentionally.

References:
- Hong, Branavan, Mansinghka (2022) "Hierarchical Bayesian Bandits", NeurIPS
- Audibert, Bubeck (2010) — best-arm identification baseline
- Gelman et al., *Bayesian Data Analysis* — hierarchical model foundations
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class _NodeInfo:
    """Per-node tree topology + accumulated outcomes.

    ``parent_id`` is None only for the tree root.
    ``outcomes`` maps task_id → list[bool] (supports multi-attempt nodes
    such as a k=3 paired root anchor).
    """

    node_id: str
    parent_id: Optional[str]
    outcomes: dict[str, list[bool]] = field(default_factory=lambda: defaultdict(list))


class TreeAwareTaskScheduler:
    """Tree-aware bandit-on-tasks scheduler (Option F.1, single-source kernel).

    Typical lifecycle:

        scheduler = TreeAwareTaskScheduler(
            all_task_ids=tb2_task_ids,
            ancestry_lambda=0.7,
            rng_seed=42,
        )

        # Phase 0: register root + record k=3 paired outcomes
        scheduler.add_node("v7", parent_id=None)
        for attempt in range(3):
            for task, passed in v7_attempt_results[attempt].items():
                scheduler.add_outcome("v7", task, passed)

        # Round 1: add A, B, C as v7's children, record their k=1 outcomes
        scheduler.add_node("A", parent_id="v7")
        for task, passed in A_results.items():
            scheduler.add_outcome("A", task, passed)
        # ... B, C ...

        # Round 2: pick K tasks for new grandchild A1 of A
        scheduler.add_node("A1", parent_id="A")
        tasks_for_A1 = scheduler.choose(v_new_id="A1", K=10)

    Parameters
    ----------
    all_task_ids
        Complete pool of task ids the scheduler may select from.
    ancestry_lambda
        Decay rate of the ancestry kernel ``w(d) = exp(-λ × d)``. Larger
        λ → only close ancestors matter; smaller → distant ancestors
        also contribute. Default 0.7 gives w(1) ≈ 0.50, w(2) ≈ 0.25,
        w(3) ≈ 0.12.
    exploration_weight
        UCB-style exploration bonus scaler. Default 1.0.
    rng_seed
        Seed for the tie-break jitter so identical scenarios reproduce.

    Notes
    -----
    v0.1 limitations (deferred to F.2+):

    - Only strict-ancestor outcomes contribute. Siblings, uncles, and
      cousins are NOT used (would require a second kernel — Option F.2
      multi-source extension).
    - Single global λ. Per-task learned λ is Option F.3.
    - No (WHERE × WHY) cell prior. Option F.2.
    - No trajectory-feature prior. Option F.4.
    """

    def __init__(
        self,
        all_task_ids: Iterable[str],
        *,
        ancestry_lambda: float = 0.7,
        exploration_weight: float = 1.0,
        rng_seed: Optional[int] = None,
    ) -> None:
        task_ids = list(all_task_ids)
        if not task_ids:
            raise ValueError("TreeAwareTaskScheduler requires at least one task id")
        if ancestry_lambda <= 0:
            raise ValueError("ancestry_lambda must be positive")
        if exploration_weight < 0:
            raise ValueError("exploration_weight must be non-negative")

        self._task_ids: list[str] = task_ids
        self._task_id_set: set[str] = set(task_ids)
        self._lambda: float = ancestry_lambda
        self._explore_w: float = exploration_weight
        self._rng = random.Random(rng_seed)
        self._nodes: dict[str, _NodeInfo] = {}

    # ────────────────────────── tree topology ──────────────────────────

    def add_node(self, node_id: str, parent_id: Optional[str]) -> None:
        """Register a node in the evolution tree.

        Root has ``parent_id=None``. Duplicate adds are rejected so the
        tree topology stays acyclic and well-defined.
        """
        if node_id in self._nodes:
            raise ValueError(f"node {node_id!r} already registered")
        if parent_id is not None and parent_id not in self._nodes:
            raise ValueError(
                f"parent {parent_id!r} of node {node_id!r} not registered yet"
            )
        self._nodes[node_id] = _NodeInfo(node_id=node_id, parent_id=parent_id)

    def add_outcome(self, node_id: str, task_id: str, passed: bool) -> None:
        """Record one (node, task) pass/fail observation.

        Multiple calls for the same (node, task) pair are appended —
        natural for a k>1 paired root or any node re-evaluated.
        """
        if node_id not in self._nodes:
            raise ValueError(f"node {node_id!r} not registered")
        if task_id not in self._task_id_set:
            raise ValueError(f"task {task_id!r} not in the registered task pool")
        self._nodes[node_id].outcomes[task_id].append(bool(passed))

    def ancestry_distance(
        self, descendant_id: str, ancestor_id: str
    ) -> Optional[int]:
        """Distance from ``descendant_id`` up to ``ancestor_id``.

        Returns the number of edges if ``ancestor_id`` is on the
        descendant's ancestral chain (including ``descendant_id``
        itself at distance 0), or None otherwise.
        """
        if descendant_id not in self._nodes:
            raise ValueError(f"node {descendant_id!r} not registered")
        if ancestor_id not in self._nodes:
            raise ValueError(f"node {ancestor_id!r} not registered")
        d = 0
        current: Optional[str] = descendant_id
        while current is not None:
            if current == ancestor_id:
                return d
            current = self._nodes[current].parent_id
            d += 1
        return None

    def ancestors_with_distance(
        self, node_id: str, *, include_self: bool = True
    ) -> list[tuple[str, int]]:
        """All ancestors of ``node_id`` with their distances.

        Ordered from nearest (self at d=0 if ``include_self``) outward
        to the root.
        """
        if node_id not in self._nodes:
            raise ValueError(f"node {node_id!r} not registered")
        result: list[tuple[str, int]] = []
        current: Optional[str] = node_id
        d = 0
        while current is not None:
            if include_self or d > 0:
                result.append((current, d))
            current = self._nodes[current].parent_id
            d += 1
        return result

    # ────────────────────────── scoring ──────────────────────────

    def _weight(self, distance: int) -> float:
        """Ancestry kernel ``exp(-λ × distance)``."""
        return math.exp(-self._lambda * distance)

    def weighted_posterior(
        self, task_id: str, v_new_id: str
    ) -> tuple[float, float]:
        """Hierarchical Bernoulli posterior for (task_id, v_new_id).

        Returns ``(p_hat, effective_n)``:

        - ``p_hat`` is the ancestry-weighted mean of observed outcomes
          on ``task_id`` across all of ``v_new_id``'s registered
          ancestors (including itself if it already has outcomes).
        - ``effective_n`` is the sum of weights — proxy for posterior
          tightness. 0 means no informative observations.

        When no ancestor has any outcome on the task, returns the
        uninformative prior ``(0.5, 0.0)`` so cold-start tasks still
        rank highly via exploration bonus.
        """
        if task_id not in self._task_id_set:
            raise ValueError(f"task {task_id!r} not in registered pool")
        if v_new_id not in self._nodes:
            raise ValueError(f"node {v_new_id!r} not registered")

        numerator = 0.0
        denominator = 0.0
        for ancestor_id, distance in self.ancestors_with_distance(v_new_id):
            outcomes = self._nodes[ancestor_id].outcomes.get(task_id, ())
            if not outcomes:
                continue
            w = self._weight(distance)
            for passed in outcomes:
                numerator += w * (1.0 if passed else 0.0)
                denominator += w

        if denominator == 0:
            return 0.5, 0.0
        return numerator / denominator, denominator

    def variance_estimate(self, task_id: str, v_new_id: str) -> float:
        """Bernoulli variance ``p̂(1-p̂)`` under the weighted posterior.

        Cold-start (no informative ancestor) returns the Bernoulli
        maximum 0.25 so unseen tasks compete for exploration slots.
        """
        p, n = self.weighted_posterior(task_id, v_new_id)
        if n == 0:
            return 0.25
        return p * (1.0 - p)

    def exploration_bonus(self, task_id: str) -> float:
        """UCB-style bonus ``1 / √(n + 1)`` for under-observed tasks.

        ``n`` counts ALL observations on the task across the whole
        tree (not ancestry-weighted) — exploration is about "have we
        looked at this task at all", independent of who's asking.
        """
        n = sum(
            len(info.outcomes.get(task_id, ()))
            for info in self._nodes.values()
        )
        return 1.0 / math.sqrt(n + 1)

    def score(self, task_id: str, v_new_id: str) -> float:
        """Composite task selection score: variance + bonus + jitter.

        Higher score = bandit prefers selecting this task for v_new's
        next evaluation subset.
        """
        var = self.variance_estimate(task_id, v_new_id)
        bonus = self._explore_w * self.exploration_bonus(task_id)
        jitter = self._rng.random() * 1e-9
        return var + bonus + jitter

    # ────────────────────────── selection API ──────────────────────────

    def choose(
        self,
        v_new_id: str,
        K: int = 10,
        *,
        exclude_tasks: Optional[Iterable[str]] = None,
    ) -> list[str]:
        """Pick top-``K`` tasks for the new candidate ``v_new_id``.

        ``exclude_tasks`` lets the caller skip tasks already scheduled
        on ``v_new_id`` (e.g., when batching multiple ``choose`` calls
        or recovering from a partial run).

        If fewer than ``K`` tasks remain after exclusion, returns
        whatever is available.
        """
        if K <= 0:
            return []
        if v_new_id not in self._nodes:
            raise ValueError(f"node {v_new_id!r} not registered")

        excluded: set[str] = set(exclude_tasks) if exclude_tasks else set()
        scored: list[tuple[float, str]] = []
        for task_id in self._task_ids:
            if task_id in excluded:
                continue
            scored.append((self.score(task_id, v_new_id), task_id))
        scored.sort(reverse=True)
        return [t for _, t in scored[:K]]

    def choose_from_pool(
        self,
        v_new_id: str,
        pool: Iterable[str],
        K: int = 5,
    ) -> list[str]:
        """Pick top-``K`` tasks from a caller-provided ``pool`` subset.

        Identical scoring to :meth:`choose` (variance + γ × exploration
        bonus + jitter) but restricted to ``pool`` rather than the full
        registered task pool.

        Use case (β+α task selection per
        [[project-task-subset-selection-v2]]):

            anchor_10 = LOCKED_ROUND_2_SET_A
            non_anchor = set(scheduler._task_ids) - set(anchor_10)
            explore_5 = scheduler.choose_from_pool(
                v_new_id="round_N_anchor",
                pool=non_anchor,
                K=5,
            )
            subset = list(anchor_10) + explore_5  # K=15 total

        Behavior on edge cases:
        - Empty pool → ``[]``
        - K=0 → ``[]``
        - K > pool size → returns all of pool, sorted by score
        - Tasks in pool but not in registered ``all_task_ids`` are
          silently skipped (caller should pre-filter for clean semantics)
        """
        if K <= 0:
            return []
        if v_new_id not in self._nodes:
            raise ValueError(f"node {v_new_id!r} not registered")

        pool_set: set[str] = set(pool)
        if not pool_set:
            return []
        # Restrict to tasks the scheduler actually knows about
        registered = set(self._task_ids)
        valid_pool = pool_set & registered
        if not valid_pool:
            return []

        scored: list[tuple[float, str]] = []
        for task_id in valid_pool:
            scored.append((self.score(task_id, v_new_id), task_id))
        scored.sort(reverse=True)
        return [t for _, t in scored[:K]]

    # ────────────────────────── introspection ──────────────────────────

    def n_nodes(self) -> int:
        return len(self._nodes)

    def n_observations(self) -> int:
        return sum(
            sum(len(outs) for outs in info.outcomes.values())
            for info in self._nodes.values()
        )

    def __repr__(self) -> str:
        return (
            f"TreeAwareTaskScheduler(tasks={len(self._task_ids)}, "
            f"nodes={self.n_nodes()}, observations={self.n_observations()}, "
            f"λ={self._lambda})"
        )


__all__ = ["TreeAwareTaskScheduler"]
