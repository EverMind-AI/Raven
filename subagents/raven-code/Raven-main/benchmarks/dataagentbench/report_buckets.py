"""Three-bucket diagnostic report for a scored DataAgentBench run.

Reads the ``reports/benchmark.json`` that ``agent-eval evaluate`` writes and
buckets every query by trial stability:

  stable  -- passed every graded attempt (you already own it)
  flaky   -- passed some attempts (invest in L3: determinism/verification)
  zero    -- passed none (invest in L1 evidence or L2 methodology)

The flaky share, not the headline pass@1, decides where the next unit of
engineering budget goes.

Usage::

    python -m benchmarks.dataagentbench.report_buckets <run_dir>
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    report_path = Path(args[0]) / "reports" / "benchmark.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    metrics = report.get("metrics", {})
    details = report.get("details", {})
    per_task = details.get("per_task", [])
    if not per_task:
        print("no per_task entries; run `agent-eval evaluate` first", file=sys.stderr)
        return 1

    buckets: dict[str, list[dict]] = {"stable": [], "flaky": [], "zero": [], "ungraded": []}
    per_dataset: dict[str, list[dict]] = defaultdict(list)
    for task in per_task:
        graded = task.get("graded_attempts", 0)
        correct = task.get("correct_attempts", 0)
        planned = task.get("planned_attempts", 0)
        # "stable" means every *graded* attempt passed. It deliberately does not
        # require graded == planned: an attempt lost to an infra error (e.g. a
        # provider transport bug) is ungraded, not a capability failure, so it
        # must not demote an otherwise-clean query into the flaky bucket.
        if graded == 0:
            key = "ungraded"
        elif correct == graded:
            key = "stable"
        elif correct == 0:
            key = "zero"
        else:
            key = "flaky"
        if graded and graded < planned:
            task = {**task, "_partial": f"{graded}/{planned} trials graded"}
        buckets[key].append(task)
        per_dataset[task["dataset"]].append(task)

    total = len(per_task)
    print(f"pass@1 (stratified): {metrics.get('pass_at_1')}")
    print(f"use_hints: {metrics.get('use_hints')}  queries: {total}")
    print()
    for key in ("stable", "flaky", "zero", "ungraded"):
        tasks = buckets[key]
        share = 100.0 * len(tasks) / total if total else 0.0
        print(f"{key:9s} {len(tasks):3d}  ({share:.1f}%)")
        for task in tasks:
            if key != "stable":
                print(f"    {task['task_id']}: {task['correct_attempts']}/{task['graded_attempts']}")
    print()
    print(f"{'dataset':20s} {'pass@1':>7s}  per-query (correct/graded)")
    for name, rows in sorted(per_dataset.items()):
        rates = [r["correct_attempts"] / r["graded_attempts"] for r in rows if r.get("graded_attempts")]
        rate = sum(rates) / len(rows) if rows else 0.0
        cells = " ".join(f"{r['correct_attempts']}/{r['graded_attempts']}" for r in rows)
        print(f"{name:20s} {rate:7.3f}  {cells}")
    graded_q = len(buckets["stable"]) + len(buckets["flaky"]) + len(buckets["zero"])
    flaky_share = 100.0 * len(buckets["flaky"]) / graded_q if graded_q else 0.0
    print()
    if buckets["ungraded"]:
        print(f"WARNING: {len(buckets['ungraded'])} queries ungraded (infra failures) -- pass@1 excludes them")
    verdict = "invest in L3 (consistency)" if flaky_share > 15 else "invest in L1/L2 (evidence/methodology)"
    print(f"flaky share among graded queries {flaky_share:.1f}% -> {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
