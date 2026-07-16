"""``python -m raven.evolver`` — the unified self-evolution entry point.

    run      cold start -> rounds -> unseal, resumable at any interruption
    check    validate config / models / bench setup without running anything
    status   inspect progress (never reveals sealed test numbers)
    finalize end the run now and unseal (one-way)
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="raven.evolver",
        description="Run the SOP self-evolution loop on a registered benchmark.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", required=True, help="run spec YAML")
        sp.add_argument("--smoke", action="store_true",
                        help="shrunk verification run in <work_dir>_smoke")

    run = sub.add_parser("run", help="start or resume an evolution run")
    common(run)
    run.add_argument("--force", action="store_true",
                     help="override the unseal / config-drift guards")

    check = sub.add_parser("check", help="validate config/models/bench setup, run nothing")
    common(check)

    status = sub.add_parser("status", help="show run progress (sealed-safe)")
    common(status)

    fin = sub.add_parser("finalize", help="terminate now and unseal (one-way)")
    common(fin)
    fin.add_argument("--yes", action="store_true", help="confirm ending the run")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from raven.evolver.launch import runner

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
