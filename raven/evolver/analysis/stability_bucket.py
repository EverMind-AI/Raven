"""Per-task k-attempts stability bucketing for paired baseline trial dirs.

Reads a legacy-runner trial dir (e.g. ``data/v7_k3_baseline/<dated>/``) where each
task has up to ``k`` independent attempts and emits a per-task stability
classification used downstream by the cold-start bandit's task-cohort
stratification.

Buckets (for k=3):

- ``STABLE_PASS``      = passed in every attempt (e.g. 3/3)
- ``BORDERLINE_2_3``   = passed in (k-1) of k attempts (e.g. 2/3)
- ``BORDERLINE_1_3``   = passed in 1 attempt
- ``STABLE_FAIL``      = 0 pass in any attempt

Pass criterion: ``verifier/reward.txt`` exists and reads as ``>= 1.0``.
Trials with no reward (e.g. ``RewardFileNotFoundError`` / wall-clock
``AgentTimeoutError`` / ``VerifierTimeoutError``) count as FAIL.

The k value is inferred from the data — a task may have < k attempts if
some failed pre-trial; the bucket label reflects fractional pass count
over the attempts actually observed (so a 1/2 task with k=3 nominal still
gets bucketed as ``BORDERLINE_1_3`` in that grouping).
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class StabilityBucket(str, Enum):
    STABLE_PASS = "stable_pass"
    BORDERLINE_2_3 = "borderline_2_3"
    BORDERLINE_1_3 = "borderline_1_3"
    STABLE_FAIL = "stable_fail"


@dataclass(frozen=True)
class TaskStability:
    task_id: str
    attempts: int
    passes: int
    bucket: StabilityBucket


def _trial_passed(trial_dir: Path) -> bool:
    """Return True iff ``verifier/reward.txt`` exists and reads ``>= 1.0``."""
    reward = trial_dir / "verifier" / "reward.txt"
    if not reward.exists():
        return False
    try:
        return float(reward.read_text().strip()) >= 1.0
    except (ValueError, OSError):
        return False


def _bucket_for(passes: int, attempts: int) -> StabilityBucket:
    """Classify a (passes, attempts) tuple into a StabilityBucket.

    Designed for k in {2, 3}. For arbitrary k, ``passes == 0`` is always
    ``STABLE_FAIL`` and ``passes == attempts`` is always ``STABLE_PASS``;
    in-between gets the BORDERLINE_2_3 / BORDERLINE_1_3 split by which
    side of the midpoint the pass count falls on.
    """
    if passes == 0:
        return StabilityBucket.STABLE_FAIL
    if passes == attempts:
        return StabilityBucket.STABLE_PASS
    # mixed: split between "mostly pass" and "mostly fail"
    if passes * 2 > attempts:
        return StabilityBucket.BORDERLINE_2_3
    return StabilityBucket.BORDERLINE_1_3


def _task_id(trial_name: str) -> str:
    """Extract canonical task id from a legacy trial dir name.

    Layout: ``{task-id}__{8-char-suffix}``. We strip the suffix.
    """
    sep = "__"
    if sep in trial_name:
        return trial_name.rsplit(sep, 1)[0]
    return trial_name


def _looks_like_trial_dir(p: Path) -> bool:
    """A legacy trial dir always carries a top-level ``result.json`` AND
    a ``verifier/`` subdir; the job-level dated dir has ``result.json``
    too but no ``verifier/`` of its own, so the verifier check is what
    discriminates a trial dir from the job dir.
    """
    return (
        p.is_dir()
        and "__" in p.name
        and (p / "result.json").exists()
        and (p / "verifier").is_dir()
    )


def _find_attempt_root(trial_dir: Path) -> Path:
    """Locate the directory whose children are the per-trial dirs.

    Accepts either the legacy jobs_dir (e.g. ``data/v7_k3_baseline/``
    which contains a dated subdir) or the dated subdir itself.
    Both dated dirs and trial dirs use ``__`` in their names, so we
    discriminate by the presence of ``result.json``.
    """
    if not trial_dir.is_dir():
        raise NotADirectoryError(trial_dir)
    if any(_looks_like_trial_dir(p) for p in trial_dir.iterdir()):
        return trial_dir
    nested = [p for p in trial_dir.iterdir() if p.is_dir()]
    if len(nested) == 1:
        return nested[0]
    return trial_dir


def compute_stability(trial_dir: str | Path) -> dict[str, TaskStability]:
    """Aggregate k-attempts pass counts per task and assign a bucket.

    Returns mapping ``{task_id: TaskStability}``.
    """
    root = _find_attempt_root(Path(trial_dir))
    per_task_passes: dict[str, list[bool]] = defaultdict(list)
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        if "__" not in d.name:
            continue
        task_id = _task_id(d.name)
        per_task_passes[task_id].append(_trial_passed(d))

    result: dict[str, TaskStability] = {}
    for task_id, passes in per_task_passes.items():
        n = len(passes)
        k = sum(passes)
        result[task_id] = TaskStability(
            task_id=task_id,
            attempts=n,
            passes=k,
            bucket=_bucket_for(k, n),
        )
    return result


def bucket_counts(stability: Iterable[TaskStability]) -> dict[StabilityBucket, int]:
    """Tally how many tasks fall into each bucket."""
    counts = {b: 0 for b in StabilityBucket}
    for ts in stability:
        counts[ts.bucket] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trial-dir", required=True, help="legacy jobs_dir or dated subdir")
    ap.add_argument("--json", default=None, help="optional JSON dump path")
    args = ap.parse_args(argv)

    stab = compute_stability(args.trial_dir)
    counts = bucket_counts(stab.values())

    print(f"trial_dir: {args.trial_dir}")
    print(f"tasks observed: {len(stab)}")
    for b in StabilityBucket:
        print(f"  {b.value:18s} {counts[b]}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(
                {tid: {"attempts": ts.attempts, "passes": ts.passes, "bucket": ts.bucket.value}
                 for tid, ts in sorted(stab.items())},
                f, indent=2,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
