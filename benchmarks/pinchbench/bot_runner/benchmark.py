#!/usr/bin/env python3
"""
PinchBench Benchmark Runner — bot-mode variant.

Runs a full Raven bot (AgentLoop run_turn) for each task,
submitting prompts as USER turns through the spine.

Usage:
    python benchmark.py --model anthropic/claude-sonnet-4
    python benchmark.py --suite task_00_sanity
    python benchmark.py --suite automated-only --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# Add project root to path so we can import raven
# Path: benchmarks/pinchbench/bot_runner/benchmark.py
SCRIPT_DIR = Path(__file__).parent
BENCHMARK_ROOT = SCRIPT_DIR.parent  # pinchbench/
BENCHMARKS_ROOT = BENCHMARK_ROOT.parent  # benchmarks/
PROJECT_ROOT = BENCHMARKS_ROOT.parent  # project root
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

from bot_executor import DEFAULT_API_KEY, DEFAULT_MODEL, execute_task  # noqa: E402
from grading import GradeResult, grade_task  # noqa: E402
from task_loader import Task, load_all_tasks  # noqa: E402

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BENCHMARK_ROOT / "bot_runner.log"),
    ],
)
logger = logging.getLogger("benchmark.bot")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PinchBench for Raven (bot mode)")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model identifier (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--api-key",
        default=DEFAULT_API_KEY,
        help="OpenRouter API key (defaults to OPENROUTER_API_KEY)",
    )
    parser.add_argument(
        "--suite",
        default="all",
        help='Tasks: "all", "automated-only", or comma-separated task IDs',
    )
    parser.add_argument(
        "--timeout-multiplier",
        type=float,
        default=1.0,
        help="Scale all task timeouts",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of runs per task for averaging",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--output-dir",
        default=str(BENCHMARK_ROOT / "results"),
        help="Results directory",
    )
    return parser.parse_args()


def _select_tasks(tasks: List[Task], suite: str) -> List[Task]:
    if suite == "all":
        return tasks
    if suite == "automated-only":
        return [t for t in tasks if t.grading_type == "automated"]
    ids = {tid.strip() for tid in suite.split(",") if tid.strip()}
    return [t for t in tasks if t.task_id in ids]


def _slugify(s: str) -> str:
    return s.replace("/", "-").replace(".", "-")


def _next_run_id(run_root: Path) -> str:
    run_root.mkdir(parents=True, exist_ok=True)
    existing = [int(e.name) for e in run_root.iterdir() if e.is_dir() and e.name.isdigit()]
    return f"{(max(existing) + 1) if existing else 1:04d}"


async def run_benchmark(args: argparse.Namespace) -> None:
    tasks_dir = BENCHMARK_ROOT / "tasks"
    assets_dir = BENCHMARK_ROOT / "assets"
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not args.api_key:
        logger.error("No OpenRouter API key configured. Set OPENROUTER_API_KEY or pass --api-key.")
        return

    # Load tasks
    all_tasks = load_all_tasks(tasks_dir)
    tasks_to_run = _select_tasks(all_tasks, args.suite)

    if not tasks_to_run:
        logger.error("No tasks to run for suite: %s", args.suite)
        return

    logger.info("=" * 70)
    logger.info("  PinchBench for Raven (BOT MODE)")
    logger.info("  Model: %s", args.model)
    logger.info("  Tasks: %d / %d", len(tasks_to_run), len(all_tasks))
    logger.info("  Runs per task: %d", args.runs)
    logger.info("=" * 70)

    for t in tasks_to_run:
        logger.info("  [%s] %s (%s, %s, %ds)", t.task_id, t.name, t.category, t.grading_type, t.timeout_seconds)

    model_slug = _slugify(args.model)
    run_root = Path("/tmp/pinchbench-raven-bot")
    run_id = _next_run_id(run_root)

    results: List[Dict[str, Any]] = []
    grades_by_task: Dict[str, Dict[str, Any]] = {}

    for i, task in enumerate(tasks_to_run, 1):
        task_grades: List[GradeResult] = []

        for run_idx in range(args.runs):
            logger.info("")
            logger.info("=" * 70)
            logger.info(
                "  Task %d/%d (Run %d/%d): [%s] %s",
                i,
                len(tasks_to_run),
                run_idx + 1,
                args.runs,
                task.task_id,
                task.name,
            )
            logger.info("=" * 70)

            # Execute via bot mode
            workspace = run_root / run_id / f"{task.task_id}_run{run_idx + 1}"
            execution_error = None
            try:
                result = await execute_task(
                    task=task,
                    workspace=workspace,
                    assets_dir=assets_dir,
                    model=args.model,
                    api_key=args.api_key,
                    timeout_multiplier=args.timeout_multiplier,
                    verbose=args.verbose,
                )
            except Exception as exc:
                execution_error = str(exc)
                logger.warning("Task execution failed for %s: %s", task.task_id, exc)
                result = {
                    "task_id": task.task_id,
                    "status": "error",
                    "transcript": [],
                    "workspace": str(workspace),
                    "execution_time": 0.0,
                    "timed_out": False,
                }

            # Grade
            try:
                grade = grade_task(
                    task=task,
                    execution_result=result,
                    judge_model=args.model,
                    judge_api_key=args.api_key,
                    verbose=args.verbose,
                )
            except Exception as exc:
                note = f"Grading failed: {exc}"
                if execution_error:
                    note = f"Exec error: {execution_error} | {note}"
                logger.warning("Grading failed for %s: %s", task.task_id, exc)
                grade = GradeResult(
                    task_id=task.task_id,
                    score=0.0,
                    max_score=1.0,
                    grading_type=task.grading_type,
                    breakdown={},
                    notes=note,
                )

            task_grades.append(grade)
            results.append(result)

            # Log score
            pct = grade.score / grade.max_score * 100 if grade.max_score > 0 else 0
            emoji = "PASS" if grade.score >= grade.max_score else "PARTIAL" if grade.score > 0 else "FAIL"
            logger.info(
                "  %s %s: %.2f/%.2f (%.0f%%) [%s]",
                emoji,
                task.task_id,
                grade.score,
                grade.max_score,
                pct,
                grade.grading_type,
            )
            if grade.notes:
                logger.info("  Notes: %s", grade.notes[:300])
            if grade.breakdown:
                for k, v in grade.breakdown.items():
                    logger.info("    %s: %.2f", k, v)

        # Aggregate runs
        task_scores = [g.score for g in task_grades]
        grades_by_task[task.task_id] = {
            "runs": [g.to_dict() for g in task_grades],
            "mean": statistics.mean(task_scores),
            "std": statistics.stdev(task_scores) if len(task_scores) > 1 else 0.0,
            "min": min(task_scores),
            "max": max(task_scores),
        }

    # Summary
    logger.info("")
    logger.info("=" * 70)
    logger.info("  BENCHMARK RESULTS SUMMARY (BOT MODE)")
    logger.info("=" * 70)

    all_means = [g["mean"] for g in grades_by_task.values()]
    overall_mean = statistics.mean(all_means) if all_means else 0.0

    for task_id, g in grades_by_task.items():
        pct = g["mean"] * 100
        bar = "#" * int(pct / 5) + "-" * (20 - int(pct / 5))
        logger.info("  %-25s [%s] %.1f%%", task_id, bar, pct)

    logger.info("-" * 70)
    logger.info("  Overall mean score: %.2f (%.1f%%)", overall_mean, overall_mean * 100)
    logger.info("  Tasks run: %d", len(tasks_to_run))

    total_time = sum(r.get("execution_time", 0) for r in results)
    logger.info("  Total execution time: %.1fs", total_time)
    logger.info("=" * 70)

    # Save results
    aggregate = {
        "mode": "bot",
        "model": args.model,
        "run_id": run_id,
        "timestamp": time.time(),
        "suite": args.suite,
        "runs_per_task": args.runs,
        "overall_score": round(overall_mean, 4),
        "tasks": [
            {
                "task_id": r["task_id"],
                "status": r["status"],
                "timed_out": r["timed_out"],
                "execution_time": r.get("execution_time", 0),
                "grading": grades_by_task.get(r["task_id"], {}),
            }
            for r in results
        ],
    }

    output_path = output_dir / f"bot_{run_id}_{model_slug}.json"
    output_path.write_text(json.dumps(aggregate, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Results saved to %s", output_path)


def main():
    args = _parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
