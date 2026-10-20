#!/usr/bin/env python3
"""Re-grade the arms a run produced, without re-running the arms.

The arms are expensive: each one is a full research round through the
research flow. The grading is not, and the grader changed while the arms
were running — so the honest thing is to re-grade every arm from the
artefacts it left, with the grader as it is now, rather than to record a
score that a later fix has already made wrong.

    python3 benchmarks/pinchbench/direct/regrade_arms.py --out /tmp/rung-arms5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))

from grade_bridge import automated_grade  # noqa: E402
from market_rung import ArmResult, paired_gate  # noqa: E402
from task_loader import load_all_tasks  # noqa: E402

ARMS = ("market-on", "market-off")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path, help="the arms' root directory")
    parser.add_argument("--task", default="task_18_market_research.md")
    args = parser.parse_args()

    tasks = load_all_tasks(REPO / "benchmarks" / "pinchbench" / "tasks")
    task = next((t for t in tasks if t.file_path and Path(t.file_path).name == args.task), None)
    if task is None:
        print(f"no task file named {args.task}", file=sys.stderr)
        return 2

    results: list[ArmResult] = []
    for arm in ARMS:
        for workspace in sorted(args.out.glob(f"workspace-{arm}-*")):
            repeat = int(workspace.name.rsplit("-", 1)[-1])
            home = args.out / f"home-{arm}-{repeat}"
            report = workspace / "market_research.md"
            if not report.exists():
                results.append(
                    ArmResult(arm=arm, repeat=repeat, score=None, detail="the run produced no report")
                )
                print(f"{arm} {repeat}: UNMEASURED (no report)")
                continue
            score, notes = automated_grade(task, str(workspace), home=home)
            results.append(ArmResult(arm=arm, repeat=repeat, score=score, detail=notes))
            print(f"{arm} {repeat}: {score:.4f}  {notes}")

    verdict = paired_gate(
        [r for r in results if r.arm == "market-on"],
        [r for r in results if r.arm == "market-off"],
    )
    report = {
        "task": {"id": task.task_id, "file": args.task},
        "grader": "the task's automated check (model-free); the LLM-judge component did not run",
        "results": [r.__dict__ for r in results],
        "verdict": verdict.to_dict(),
    }
    (args.out / "arms.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["verdict"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
