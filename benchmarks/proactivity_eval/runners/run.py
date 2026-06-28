#!/usr/bin/env python3
"""
run.py — Unified entry point for every (agent × benchmark) combination.

    uv run python proactivity-eval/runners/run.py \\
        --agent raven --benchmark pbench --n 10 \\
        --context-mode cold --output proactivity-eval/output/ec-pbench-10.json

    uv run python proactivity-eval/runners/run.py \\
        --agent raven --benchmark longrun --case parent-01 --day-limit 3

Backend selection:
  --agent raven  [--mode planner | agent | sentinel]   (default: agent)
  --agent hermes
  --agent openclaw

Sample filters:
  --case PERSONA_ID   run one persona (longrun only)
  --all               run every sample
  --n INT             stratified sample of N records (pbench)
  --limit INT         first N samples (smoke)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from _common import (  # noqa: E402
    BenchmarkDriver,
    Sample,
    get_backend,
    get_driver,
    load_dotenvs,
)

# Load .env files (project root, proactivity-eval/, ~/.hermes/) so that
# VLLM_BASE_URL / VLLM_MODEL_ID / JUDGE_* etc. don't need manual export.
# Must happen before any get_config() call downstream.
load_dotenvs()


def _backend_overrides(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    # OpenClaw-specific
    if args.thinking: out["thinking"] = args.thinking
    if args.openclaw_cmd: out["openclaw_cmd"] = args.openclaw_cmd
    if args.cli_timeout: out["cli_timeout_s"] = args.cli_timeout
    if args.subprocess_timeout: out["subprocess_timeout_s"] = args.subprocess_timeout
    # Hermes-specific
    if args.hermes_src: out["hermes_src"] = args.hermes_src
    if args.inherit_home is not None: out["inherit_home"] = args.inherit_home
    # Raven-specific
    if args.max_iter: out["max_iterations"] = args.max_iter
    if args.agent_timeout: out["agent_timeout_s"] = args.agent_timeout
    # Shared memory toggle (honored by Hermes + OpenClaw backends)
    if args.with_memory: out["with_memory"] = True
    # Longrun-specific
    if getattr(args, "day_limit", None): out["day_limit"] = args.day_limit
    if getattr(args, "output_dir", None): out["output_dir"] = args.output_dir
    if getattr(args, "simulator_model", None): out["simulator_model"] = args.simulator_model
    if getattr(args, "planner_model", None): out["planner_model"] = args.planner_model
    if getattr(args, "resume_from", None): out["resume_from"] = args.resume_from
    # Shared timeout override from the legacy --timeout flag maps to Hermes
    # subprocess timeout (its per-record wall).
    if args.timeout:
        out.setdefault("subprocess_timeout_s", args.timeout)
        out.setdefault("agent_timeout_s", args.timeout)
    return out


def _build_driver(args: argparse.Namespace) -> BenchmarkDriver:
    if args.benchmark == "pbench":
        from _common.drivers.pbench import PbenchDriver
        return PbenchDriver(
            agent_name=args.agent,
            context_mode=args.context_mode,
            synthesizer_name=args.synthesizer,
        )
    if args.benchmark == "cases":
        return get_driver("cases")
    if args.benchmark == "timeline":
        return get_driver("timeline")
    if args.benchmark == "simulation":
        return get_driver("simulation")
    if args.benchmark == "longrun":
        return get_driver("longrun")
    raise ValueError(f"unknown benchmark: {args.benchmark}")


def _filter_for_driver(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if args.case:
        out["filter_id"] = args.case
    if args.n is not None:
        out["n"] = args.n
    elif args.limit is not None:
        out["n"] = args.limit
    # --all: no n filter (default)
    return out


async def _run(args: argparse.Namespace) -> tuple[list[dict], dict[str, Any]]:
    overrides = _backend_overrides(args)
    driver = _build_driver(args)

    samples = driver.load_samples(**_filter_for_driver(args))
    if not samples:
        sys.exit(f"No samples matched (benchmark={args.benchmark}).")

    system_label = f"{args.agent}"
    if args.mode and args.agent == "raven":
        system_label = f"raven-{args.mode}"
    elif args.agent == "hermes" and args.benchmark == "pbench":
        system_label = "hermes-agent"

    runtime_meta = {
        "agent": args.agent,
        "mode": args.mode,
        "runtime": args.mode if args.agent == "raven" else None,
        "benchmark": args.benchmark,
        "system_label": system_label,
    }

    concurrency = max(1, int(args.concurrency or 1))
    print(f"[run] agent={args.agent} mode={args.mode or '-'} "
          f"benchmark={args.benchmark} samples={len(samples)} "
          f"concurrency={concurrency}",
          file=sys.stderr, flush=True)
    print(f"[run] dataset: {driver.dataset_description()}",
          file=sys.stderr, flush=True)

    # Multi-tick drivers (timeline / simulation) own scenario lifecycle
    # themselves — they iterate ticks internally with FakeClock + persistent
    # NudgePolicy state. Bypass the standard backend.run_one loop.
    use_run_scenario = hasattr(driver, "run_scenario")

    sem = asyncio.Semaphore(concurrency)
    done_counter = {"n": 0}

    if use_run_scenario:
        async def _run_one(i: int, sample) -> dict:
            sid = sample.raw.get("id") or f"#{i}"
            async with sem:
                print(f"[{i+1}/{len(samples)}] START {sid}",
                      file=sys.stderr, flush=True)
                row = await driver.run_scenario(
                    sample, args.agent, args.mode, overrides,
                )
                done_counter["n"] += 1
                passed = row.get("passed")
                cat = row.get("category", "?")
                tickn = row.get("tick_count", "?")
                print(f"[{done_counter['n']}/{len(samples)}] DONE  {sid} "
                      f"category={cat} ticks={tickn} "
                      f"passed={passed}",
                      file=sys.stderr, flush=True)
                return row
    else:
        backend = get_backend(args.agent, mode=args.mode, overrides=overrides)

        async def _run_one(i: int, sample) -> dict:
            cat_hint = sample.raw.get("category") or sample.raw.get("id") or f"#{i}"
            async with sem:
                print(f"[{i+1}/{len(samples)}] START {cat_hint}",
                      file=sys.stderr, flush=True)
                outcome = await backend.run_one(
                    sample, driver, session_id=sample.session_hint,
                )
                if outcome.meta:
                    runtime_meta_i = {**runtime_meta, **{
                        k: v for k, v in outcome.meta.items()
                        if k in ("model", "route", "delivered", "fake_now",
                                 "full_doc", "cron_prompt", "plausibility_note")
                    }}
                else:
                    runtime_meta_i = runtime_meta
                row = driver.make_row(sample, outcome, runtime_meta_i)
                done_counter["n"] += 1
                print(f"[{done_counter['n']}/{len(samples)}] DONE  {cat_hint}",
                      file=sys.stderr, flush=True)
                _print_row_summary(row, outcome)
                return row

    tasks = [asyncio.create_task(_run_one(i, s)) for i, s in enumerate(samples)]
    rows: list[dict] = await asyncio.gather(*tasks)

    if not use_run_scenario:
        try:
            backend.close()
        except Exception:
            pass

    meta = {
        "run_at": datetime.now().isoformat(),
        "agent": args.agent,
        "mode": args.mode,
        "benchmark": args.benchmark,
        "system_label": system_label,
        "context_mode": args.context_mode if args.benchmark == "pbench" else None,
        "synthesizer": args.synthesizer if args.benchmark == "pbench" else None,
    }
    return rows, meta


def _print_row_summary(row: dict[str, Any], outcome) -> None:
    # Compact per-record line for stderr progress.
    status = row.get("status") or row.get("agent", {}).get("status")
    if "help_match" in row:
        match = "OK" if row["help_match"] else "MISS"
        pred = row.get("predicted_help")
        truth = row.get("truth_help_needed")
        print(f"  {match}  pred={pred} truth={truth} "
              f"status={status} elapsed={outcome.elapsed_s}s",
              file=sys.stderr, flush=True)
    else:
        act = row.get("action", "?")
        note = ""
        fr = row.get("final_response") or row.get("reason") or ""
        if fr:
            note = f" | {fr[:80].replace(chr(10), ' ')!r}"
        print(f"  status={status} action={act} elapsed={outcome.elapsed_s}s{note}",
              file=sys.stderr, flush=True)


def _write_output(rows: list[dict], meta: dict[str, Any],
                  args: argparse.Namespace) -> None:
    # The cases benchmark historically used {run_at, system, mode, results}.
    # The pbench benchmark historically used a bare list of row dicts (so
    # pa_scorecard can json.load it directly). Preserve both shapes.
    if args.benchmark == "cases":
        payload: Any = {
            "run_at": meta["run_at"],
            "system": meta["system_label"],
            "mode": meta["mode"],
            "results": rows,
        }
    else:
        payload = rows

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
        print(f"Results written to {out}", file=sys.stderr)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--agent", required=True,
                    choices=["raven", "hermes", "openclaw"])
    ap.add_argument("--mode", default=None,
                    choices=["planner", "agent", "sentinel"],
                    help="Raven only: planner (decision layer) | "
                         "agent (AgentLoop, default) | sentinel (full Phase 2).")
    ap.add_argument("--benchmark", required=True,
                    choices=["pbench", "longrun"])

    # sample filters
    ap.add_argument("--case",
                    help="Run a single persona by id (longrun)")
    ap.add_argument("--all", action="store_true",
                    help="Run every sample (default if neither --n nor --limit set)")
    ap.add_argument("--n", type=int, default=None,
                    help="Total samples (stratified for pbench)")
    ap.add_argument("--limit", type=int, default=None,
                    help="First N samples (smoke runs)")

    # pbench-specific
    ap.add_argument("--context-mode", choices=["cold", "warm"], default="cold")
    ap.add_argument("--synthesizer", default="keyword")

    # openclaw-specific
    ap.add_argument("--thinking", default=None,
                    choices=["off", "minimal", "low", "medium", "high", "xhigh"])
    ap.add_argument("--openclaw-cmd", default=None)
    ap.add_argument("--cli-timeout", type=int, default=None,
                    help="OpenClaw: --timeout passed to `openclaw agent`")
    ap.add_argument("--subprocess-timeout", type=int, default=None,
                    help="OpenClaw outer subprocess timeout")

    # hermes-specific
    ap.add_argument("--hermes-src", default=None,
                    help="Override systems.hermes_src from runners config")
    ap.add_argument("--inherit-home", dest="inherit_home",
                    action=argparse.BooleanOptionalAction, default=None,
                    help="Copy ~/.hermes/{config.yaml,.env,auth.json} into tmpdir")

    # raven-specific
    ap.add_argument("--max-iter", type=int, default=None,
                    help="Raven AgentLoop max_iterations")
    ap.add_argument("--agent-timeout", type=int, default=None,
                    help="Raven agent per-sample wall timeout (s)")

    # shared
    ap.add_argument("--with-memory", action="store_true",
                    help="Enable agent memory access. Hermes: prepends a "
                         "<memory-context> block to cron prompt (simulates "
                         "skip_memory=False). OpenClaw: plants memory/MEMORY.md "
                         "+ memory/HISTORY.md in a seeded workspace with "
                         "bootstrap raised so OpenClaw injects it into system "
                         "prompt. No-op for Raven (AgentLoop already seeds).")
    ap.add_argument("--timeout", type=int, default=None,
                    help="Generic per-sample timeout; sets hermes subprocess "
                         "+ raven agent timeout if either is unset")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="Number of samples to run in parallel. Default 1 "
                         "(sequential). For openclaw/hermes each parallel slot "
                         "spawns its own subprocess; for raven in-process "
                         "it multiplexes the provider. LAN vLLM handles "
                         "batching well — 4-8 is usually safe.")
    ap.add_argument("--output", default=None,
                    help="Write JSON report here (required for downstream tools)")

    # longrun-specific (LLM-simulator × 30-day trajectories)
    ap.add_argument("--day-limit", type=int, default=None,
                    help="longrun: stop after N simulated days (default 30). "
                         "Use 1-3 for smoke tests.")
    ap.add_argument("--output-dir", default=None,
                    help="longrun: where to write trajectory/checkpoint/scorecard "
                         "(default: proactivity-eval/output/longrun/)")
    ap.add_argument("--simulator-model", default=None,
                    help="longrun: override user-simulator model "
                         "(default: openrouter/anthropic/claude-sonnet-4.5)")
    ap.add_argument("--planner-model", default=None,
                    help="longrun: override Sentinel ProactivePlanner model. "
                         "Useful for ablating Planner LLM quality (e.g. "
                         "openrouter/anthropic/claude-sonnet-4.5). When this "
                         "is an OpenRouter model id, the driver builds a "
                         "separate provider for the Planner; the Agent loop "
                         "still uses the local qwen endpoint. Default: same "
                         "model as the Agent (sentinel.evaluator_model or "
                         "agents.defaults.model).")
    ap.add_argument("--resume-from", default=None,
                    help="longrun: resume from checkpoint tar "
                         "(e.g. output/longrun/ckpt-dev-01-raven/day03.tar)")
    args = ap.parse_args()

    # Validation
    if args.mode and args.agent != "raven":
        ap.error("--mode only applies to --agent raven")
    if args.case and args.benchmark != "longrun":
        ap.error("--case only applies to --benchmark longrun")
    if not args.case and not args.all and args.n is None and args.limit is None:
        ap.error("specify one of --case, --all, --n, --limit")
    if args.day_limit and args.benchmark != "longrun":
        ap.error("--day-limit only applies to --benchmark longrun")

    rows, meta = asyncio.run(_run(args))
    print("\n" + "=" * 60, file=sys.stderr)
    print(_build_summary_header(args, meta), file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    driver = _build_driver(args)
    print(driver.summarize(rows), file=sys.stderr)

    _write_output(rows, meta, args)


def _build_summary_header(args: argparse.Namespace, meta: dict) -> str:
    parts = [f"{meta['system_label']} × {args.benchmark}"]
    if args.benchmark == "pbench":
        parts.append(f"context={args.context_mode}")
    return " — ".join(parts)


if __name__ == "__main__":
    main()
