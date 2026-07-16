"""Trial pool construction — adapter between trial dir + analysis modules.

Wires together the per-task stability output from
:func:`raven.evolver.analysis.stability_bucket.compute_stability` and
the per-trial features from
:func:`raven.evolver.analysis.proxy_features.extract_trial_dir`
into the unified :class:`Trial` view consumed by
:class:`raven.evolver.scheduler.cold_start_bandit.ColdStartCoverageBandit`.

A trial dir layout (k=3 paired baseline) maps to **89 task × 3 attempts
≈ 267 Trial objects**. All attempts of a single task share the same
task-level ``stability`` bucket; each trial carries its own per-trial
``proxy_features`` dict, projected to numeric-only entries so the
bandit's K-means sub-strata clustering can compute Euclidean distance
without categorical-encoding overhead.

Categorical ``ExitStatus`` is mapped through a stable ordinal table
(``_EXIT_STATUS_ORDINAL``) so K-means distance is deterministic across
Python runs (Python's built-in ``hash()`` honours ``PYTHONHASHSEED``
and isn't reliable for paper reproducibility).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from raven.evolver.analysis.proxy_features import (
    ExitStatus,
    ProxyFeatures,
    extract_trial_dir,
)
from raven.evolver.analysis.stability_bucket import (
    StabilityBucket,
    compute_stability,
)
from raven.evolver.scheduler.cold_start_bandit import Trial


_EXIT_STATUS_ORDINAL: dict[ExitStatus, int] = {
    ExitStatus.PASSED: 0,
    ExitStatus.FAILED_VERIFIER: 1,
    ExitStatus.AGENT_TIMEOUT: 2,
    ExitStatus.VERIFIER_TIMEOUT: 3,
    ExitStatus.REWARD_FILE_NOT_FOUND: 4,
    ExitStatus.RUNTIME_ERROR: 5,
    ExitStatus.NO_SESSION: 6,
    ExitStatus.OTHER: 7,
}


def proxy_features_to_kmeans_dict(pf: ProxyFeatures) -> dict[str, float]:
    """Project a :class:`ProxyFeatures` into the numeric dict the bandit
    feeds into K-means for stable_fail sub-strata clustering.

    All entries are floats so the K-means impl in
    ``cold_start_bandit._kmeans_strata`` can min-max normalise them
    uniformly. The ``exit_status_ordinal`` slot maps the categorical
    :class:`ExitStatus` through :data:`_EXIT_STATUS_ORDINAL` — distance
    isn't semantically meaningful across enum values, but it's stable
    and surfaces "this trial behaves like that one" via co-occurrence
    in cluster centroids.
    """
    return {
        "turn_count": float(pf.turn_count),
        "has_tool_calls_ever": 1.0 if pf.has_tool_calls_ever else 0.0,
        "assistant_text_length_avg": float(pf.assistant_text_length_avg),
        "docker_error_count": float(pf.docker_error_count),
        "exit_status_ordinal": float(
            _EXIT_STATUS_ORDINAL.get(pf.final_exit_status, 99)
        ),
    }


def build_trial_pool(trial_dir: str | Path) -> list[Trial]:
    """Construct a list of :class:`Trial` from a legacy-runner trial dir.

    Walks the trial dir once via the existing analysis modules,
    groups trials by task to assign per-task stability + deterministic
    attempt indices (1..k), then materialises each trial with both
    its task-level stability bucket and its per-trial proxy-feature
    K-means dict.

    Returns the list sorted by ``(task_id, attempt)`` for
    repr-friendliness; ``ColdStartCoverageBandit`` shuffles the
    borderline pool internally under its own RNG seed regardless.
    """
    trial_dir = Path(trial_dir)
    stability_map = compute_stability(trial_dir)
    features_map = extract_trial_dir(trial_dir)

    # Group ProxyFeatures by task for attempt numbering
    by_task: dict[str, list[ProxyFeatures]] = defaultdict(list)
    for pf in features_map.values():
        by_task[pf.task_id].append(pf)
    for tid in by_task:
        by_task[tid].sort(key=lambda p: p.trial_id)

    trials: list[Trial] = []
    for task_id in sorted(by_task.keys()):
        task_stability = stability_map.get(task_id)
        if task_stability is None:
            # Defensive: shouldn't happen since both modules walk the
            # same dir, but skip silently rather than crash on edge case
            continue
        for attempt_idx, pf in enumerate(by_task[task_id], start=1):
            passed = pf.final_exit_status == ExitStatus.PASSED
            trials.append(Trial(
                trial_id=pf.trial_id,
                task_id=task_id,
                attempt=attempt_idx,
                passed=passed,
                stability=task_stability.bucket,
                proxy_features=proxy_features_to_kmeans_dict(pf),
            ))
    return trials


__all__ = [
    "build_trial_pool",
    "proxy_features_to_kmeans_dict",
]
