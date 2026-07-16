"""Cold-start coverage bandit (5th orchestration mechanism).

Selects which trials a judge (claude in Phase 1 bootstrap, Qwen in
later phases) should label, optimizing for K=7 WHY pathology class
coverage at minimum judge calls.

v2 design (post v7 k=3 paired baseline, 2026-06-05) — uses k=3
stability bucket as **dominant proxy feature**:

- ``STABLE_PASS`` (3/3): skip entirely (no pathology to discover)
- ``BORDERLINE_2_3`` / ``BORDERLINE_1_3``: high signal density — Phase 1
  drains these first (every borderline trial has near-guaranteed
  informative content because the task itself is at the discrimination
  edge)
- ``STABLE_FAIL`` (0/3): persistent pathology — Phase 2 runs UCB on
  K-means sub-strata of cheap metadata (turn count / exit status /
  ``has_tool_calls`` / docker_error etc.), reward = "new WHY discovered"

Stops when ``n_why_classes`` covered or ``budget`` exhausted.

Theoretical complexity:

- Phase 1: O(|borderline|), deterministic (no posterior to converge)
- Phase 2: O(K log K) expected for UCB best-arm identification on
  Beta-Bernoulli posterior over the sub-strata reward signal
- Total: O(|borderline| + K log K) — typically ~22 trials for K=7
  on TB2 v7 k=3 baseline (11 borderline tasks × 3 attempts max)

Algorithmic notes for paper §13:

- "5th mechanism" alongside spec §0.2's four (bandit-on-WHY /
  bandit-on-nodes / bandit-on-tasks / prefix-replay)
- Same UCB family as ``BanditTaskScheduler`` (A1, ``bandit_tasks.py``)
  — Beta-Bernoulli + UCB uncertainty bonus + deterministic jitter for
  tie-break — so code structure mirrors that module
- v2 vs v1 (uniform K-means K'=10 on cheap proxy without stability
  bucket): v2 leverages k=3 stability as a strong cheap signal,
  reduces typical budget from 30 to ~22 (~25% saving) by skipping
  stable_pass entirely and saturating borderline first
- For paper §15 Must-nail #1 ablation, compare:
  * uniform random
  * stratified random (γ baseline)
  * v1 UCB on proxy strata (no stability bucket)
  * v2 (this implementation)
  * DPP-based diverse subset (optional δ baseline)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Sequence

from raven.evolver.analysis.stability_bucket import StabilityBucket


@dataclass(frozen=True)
class Trial:
    """A single (task, attempt) trial — the unit the bandit samples.

    Caller is responsible for constructing this from raw legacy trial
    dirs + the outputs of ``stability_bucket.compute_stability`` (task
    level) and ``proxy_features.extract`` (trial level).

    ``stability`` is the task-level bucket (all attempts of one task
    share the same value). ``proxy_features`` is the per-trial cheap
    metadata used for Phase 2 K-means sub-strata.
    """

    trial_id: str
    task_id: str
    attempt: int
    passed: bool
    stability: StabilityBucket
    proxy_features: dict = field(default_factory=dict)

    @property
    def is_borderline(self) -> bool:
        return self.stability in (
            StabilityBucket.BORDERLINE_1_3,
            StabilityBucket.BORDERLINE_2_3,
        )

    @property
    def is_stable_fail(self) -> bool:
        return self.stability == StabilityBucket.STABLE_FAIL

    @property
    def is_stable_pass(self) -> bool:
        return self.stability == StabilityBucket.STABLE_PASS


@dataclass
class StratumStats:
    """Per-sub-stratum bandit state (Beta-Bernoulli over reward).

    Reward is binary ("did judging a trial from this stratum reveal a
    WHY class not previously covered"). We accumulate raw counts; the
    UCB score is computed lazily from them.
    """

    stratum_id: str
    n_pulls: int = 0
    n_new_why: int = 0

    @property
    def empirical_rate(self) -> float:
        if self.n_pulls == 0:
            return 0.0
        return self.n_new_why / self.n_pulls

    def ucb_score(self, T: int, exploration_weight: float = 1.0) -> float:
        """UCB1 score: empirical rate + exploration bonus.

        Returns +inf for never-pulled strata so each is visited at
        least once before exploitation takes over.
        """
        if self.n_pulls == 0:
            return float("inf")
        bonus = exploration_weight * math.sqrt(2 * math.log(max(T, 1)) / self.n_pulls)
        return self.empirical_rate + bonus


def _kmeans_strata(
    trials: Sequence[Trial],
    n_strata: int,
    rng: random.Random,
    max_iter: int = 20,
) -> dict[str, list[Trial]]:
    """Cluster trials into ``n_strata`` sub-strata by their proxy_features.

    Simple Lloyd's K-means on numeric features (sorted keys for
    determinism, min-max normalized for fair Euclidean distance). Pool
    sizes here are tiny (~60 trials, K=5) so a custom impl beats
    pulling in scikit-learn. Returns ``{stratum_id: [trials]}``.

    Returns ``{}`` for empty input. Caps ``n_strata`` at len(trials).
    """
    if not trials:
        return {}
    n_strata = min(n_strata, len(trials))
    if n_strata == 0:
        return {}

    feature_keys = sorted(trials[0].proxy_features.keys())
    if not feature_keys:
        return {"stratum_0": list(trials)}

    vectors = [
        [float(t.proxy_features.get(k, 0.0)) for k in feature_keys] for t in trials
    ]
    mins = [min(v[i] for v in vectors) for i in range(len(feature_keys))]
    maxs = [max(v[i] for v in vectors) for i in range(len(feature_keys))]
    spans = [(maxs[i] - mins[i]) or 1.0 for i in range(len(feature_keys))]
    norm = [
        [(v[i] - mins[i]) / spans[i] for i in range(len(feature_keys))]
        for v in vectors
    ]

    centroid_indices = rng.sample(range(len(norm)), n_strata)
    centroids = [norm[i][:] for i in centroid_indices]

    labels = [-1] * len(trials)
    for _ in range(max_iter):
        new_labels = []
        for v in norm:
            best_idx = 0
            best_dist = float("inf")
            for k, c in enumerate(centroids):
                dist = sum((v[i] - c[i]) ** 2 for i in range(len(feature_keys)))
                if dist < best_dist:
                    best_dist = dist
                    best_idx = k
            new_labels.append(best_idx)
        if new_labels == labels:
            break
        labels = new_labels
        for k in range(n_strata):
            members = [v for v, l in zip(norm, labels) if l == k]
            if members:
                centroids[k] = [
                    sum(v[i] for v in members) / len(members)
                    for i in range(len(feature_keys))
                ]

    result: dict[str, list[Trial]] = {f"stratum_{k}": [] for k in range(n_strata)}
    for trial, label in zip(trials, labels):
        result[f"stratum_{label}"].append(trial)
    return {sid: ts for sid, ts in result.items() if ts}


class ColdStartCoverageBandit:
    """5th mechanism — cold-start coverage sampling.

    Typical use::

        bandit = ColdStartCoverageBandit(
            trials=all_267_trials_from_v7_k3,
            n_why_classes=7,
            budget=25,
            rng_seed=42,
        )
        while not bandit.done():
            trial = bandit.next_trial()
            result = claude_judge(trial)  # JudgeResult JSON
            bandit.update(trial, why=result.why)
        sampled = bandit.sampled_trials()
        covered = bandit.covered_why_classes()

    Parameters
    ----------
    trials
        Full pool of trials with ``stability`` and ``proxy_features``
        populated. ``STABLE_PASS`` trials are automatically excluded.
    n_why_classes
        K — number of pathology classes to cover (spec §12.5: 7).
    budget
        Max number of trials to judge. Default 25 (v2 design).
    n_stable_fail_strata
        Number of K-means sub-strata for Phase 2. Default 5.
    exploration_weight
        UCB bonus scaler. Default 1.0 (matches UCB1 constant).
    rng_seed
        Seed for shuffles, K-means init, and tie-breaking jitter
        (paper reproducibility requirement).
    """

    def __init__(
        self,
        trials: Sequence[Trial],
        *,
        n_why_classes: int = 7,
        budget: int = 25,
        n_stable_fail_strata: int = 5,
        exploration_weight: float = 1.0,
        rng_seed: int | None = None,
    ) -> None:
        if not trials:
            raise ValueError("ColdStartCoverageBandit requires at least one trial")
        if budget <= 0:
            raise ValueError("budget must be positive")
        if n_why_classes <= 0:
            raise ValueError("n_why_classes must be positive")
        if n_stable_fail_strata <= 0:
            raise ValueError("n_stable_fail_strata must be positive")

        self._n_why_classes = n_why_classes
        self._budget = budget
        self._exploration_weight = exploration_weight
        self._rng = random.Random(rng_seed)

        # Partition by stability; stable_pass is dropped (no pathology)
        self._borderline_pool: list[Trial] = [t for t in trials if t.is_borderline]
        stable_fail_pool: list[Trial] = [t for t in trials if t.is_stable_fail]

        # Phase 1 ordering: shuffle deterministically (every borderline
        # trial is high-signal; order matters only for variety across
        # different runs with different seeds)
        self._rng.shuffle(self._borderline_pool)

        # Phase 2 K-means strata over stable_fail pool
        self._stable_fail_strata: dict[str, list[Trial]] = _kmeans_strata(
            stable_fail_pool, n_stable_fail_strata, self._rng,
        )
        # Record which stratum each trial originated from, so update()
        # can find the right StratumStats after a trial has been popped
        self._trial_to_stratum: dict[str, str] = {}
        for stratum_id, ts in self._stable_fail_strata.items():
            self._rng.shuffle(ts)
            for t in ts:
                self._trial_to_stratum[t.trial_id] = stratum_id
        self._strata_bandit: dict[str, StratumStats] = {
            sid: StratumStats(stratum_id=sid) for sid in self._stable_fail_strata
        }

        # Mutable state
        self._sampled: list[Trial] = []
        self._covered_why: set[str] = set()
        self._borderline_idx = 0

    # ────────────────────── public API ──────────────────────

    def done(self) -> bool:
        """Stop conditions: budget hit, coverage met, or pool exhausted."""
        if len(self._sampled) >= self._budget:
            return True
        if len(self._covered_why) >= self._n_why_classes:
            return True
        return not self._has_unsampled_trial()

    def next_trial(self) -> Trial:
        """Pick next trial to judge.

        Phase 1 (borderline drain) always takes priority. Phase 2 (UCB
        on stable_fail strata) kicks in only when borderline is empty.
        """
        if self.done():
            raise RuntimeError("ColdStartCoverageBandit is done; cannot sample more")

        # Phase 1: borderline pool, sequential drain after init shuffle
        if self._borderline_idx < len(self._borderline_pool):
            t = self._borderline_pool[self._borderline_idx]
            self._borderline_idx += 1
            return t

        # Phase 2: UCB pick on stable_fail strata
        T = len(self._sampled)
        scored: list[tuple[float, str]] = []
        for sid, stats in self._strata_bandit.items():
            if not self._stable_fail_strata.get(sid):
                continue
            score = stats.ucb_score(T, self._exploration_weight)
            # tiny jitter so equal scores are broken deterministically
            score = score + self._rng.random() * 1e-9
            scored.append((score, sid))
        if not scored:
            raise RuntimeError("done() lied: no remaining strata but not stopped")
        scored.sort(reverse=True)
        chosen_sid = scored[0][1]
        return self._stable_fail_strata[chosen_sid].pop(0)

    def update(self, trial: Trial, *, why: str) -> None:
        """Record judge result for a sampled trial.

        ``why`` is the WHY class label assigned (e.g. ``"budget_awareness"``).
        Reward = "discovered new WHY class" — credits the originating
        stratum's bandit posterior when applicable.
        """
        is_new = why not in self._covered_why
        self._covered_why.add(why)
        self._sampled.append(trial)

        if trial.is_stable_fail:
            stratum_id = self._trial_to_stratum.get(trial.trial_id)
            if stratum_id is not None:
                stats = self._strata_bandit[stratum_id]
                stats.n_pulls += 1
                if is_new:
                    stats.n_new_why += 1

    def sampled_trials(self) -> list[Trial]:
        return list(self._sampled)

    def covered_why_classes(self) -> set[str]:
        return set(self._covered_why)

    def borderline_pool_size(self) -> int:
        """Initial borderline pool size (for diagnostic / paper reporting)."""
        return len(self._borderline_pool)

    def stable_fail_strata_sizes(self) -> dict[str, int]:
        """Current size of each stable_fail sub-stratum (post-popping)."""
        return {sid: len(ts) for sid, ts in self._stable_fail_strata.items()}

    # ────────────────────── helpers ──────────────────────

    def _has_unsampled_trial(self) -> bool:
        if self._borderline_idx < len(self._borderline_pool):
            return True
        return any(self._stable_fail_strata.values())

    def __repr__(self) -> str:
        n_borderline = len(self._borderline_pool) - self._borderline_idx
        n_stable_fail = sum(len(ts) for ts in self._stable_fail_strata.values())
        return (
            f"ColdStartCoverageBandit("
            f"sampled={len(self._sampled)}/{self._budget}, "
            f"covered={len(self._covered_why)}/{self._n_why_classes} WHY, "
            f"borderline_remaining={n_borderline}, "
            f"stable_fail_remaining={n_stable_fail})"
        )
