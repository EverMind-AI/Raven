"""``python -m evolver`` — the unified self-evolution entry point.

run      cold start -> rounds -> unseal, resumable at any interruption
check    validate config / models / bench setup without running anything
status   inspect progress (never reveals sealed test numbers)
finalize end the run now and unseal (one-way)
validate check an agent folder against the agent scope and an acceptance suite
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="evolver",
        description="Run the SOP self-evolution loop on a registered benchmark.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", required=True, help="run spec YAML")
        sp.add_argument("--smoke", action="store_true", help="shrunk verification run in <work_dir>_smoke")

    run = sub.add_parser("run", help="start or resume an evolution run")
    common(run)
    run.add_argument("--force", action="store_true", help="override the unseal / config-drift guards")

    check = sub.add_parser("check", help="validate config/models/bench setup, run nothing")
    common(check)

    status = sub.add_parser("status", help="show run progress (sealed-safe)")
    common(status)

    fin = sub.add_parser("finalize", help="terminate now and unseal (one-way)")
    common(fin)
    fin.add_argument("--yes", action="store_true", help="confirm ending the run")

    val = sub.add_parser("validate", help="check an agent folder against the agent scope and an acceptance suite")
    val.add_argument("folder", help="the agent folder to check")
    val.add_argument("--suite", help="acceptance suite directory (required unless --static-only)")
    val.add_argument("--engine", help="engine project directory for a wheel-shaped agent")
    val.add_argument("--host-config", help="config whose providers the run borrows (default: the active one)")
    val.add_argument("--model", default="deepseek/deepseek-flash", help="model the agent runs on")
    val.add_argument("--judge-model", default="deepseek/deepseek-flash", help="model that judges the results")
    val.add_argument("--judge-repeats", type=int, default=1, help="judge calls per case; majority per criterion")
    val.add_argument("--min-pass-rate", type=float, default=1.0, help="fraction of cases that must pass")
    val.add_argument("--report", help="JSON report path (default: report.json in the run directory)")
    val.add_argument("--keep", action="store_true", help="keep the isolated home for inspection")
    val.add_argument("--static-only", action="store_true", help="run layers F, D, C and A only; never PASS")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        from evolver.validator.runner import cli_main

        return cli_main(args)
    from evolver.launch import runner

    if args.command == "run":
        return runner.cmd_run(args.config, smoke=args.smoke, force=args.force)
    if args.command == "check":
        return runner.cmd_check(args.config, smoke=args.smoke)
    if args.command == "status":
        return runner.cmd_status(args.config, smoke=args.smoke)
    if args.command == "finalize":
        return runner.cmd_finalize(args.config, smoke=args.smoke, yes=args.yes)
    return 2


if __name__ == "__main__":
    sys.exit(main())
