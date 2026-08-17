"""CLI entry — ``python -m tests.tui.autotest smoke "<command>" ...``.

The primary test path = pytest (``uv run pytest tests/tui/autotest/tests/``).
This CLI is the ad-hoc smoke fallback for ``Bash()``-driven verification
without writing a pytest file. Subcommand: ``smoke <command>``.

Exit code semantics:
- 0 = spawn ok + readiness matched (or skipped) + subprocess clean exit 0
- 1 = spawn ok but readiness timed out OR subprocess exit != 0
- 2 = harness self-error (extras missing / spawn fail / unknown subcommand)
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from typing import Optional, Sequence

from .runner import (
    BackendError,
    ExtrasMissingError,
    Harness,
    HarnessError,
    SpawnError,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.tui.autotest",
        description="TUI autotest harness CLI (ad-hoc smoke entry; primary test driver is pytest).",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True, metavar="SUBCOMMAND")

    smoke = subparsers.add_parser(
        "smoke",
        help="Spawn <command>, wait readiness, then terminate; print trace + exit code",
    )
    smoke.add_argument("command", help="Command to spawn (e.g. 'uv run raven tui --check')")
    smoke.add_argument("--cols", type=int, default=120, help="Terminal cols (default 120)")
    smoke.add_argument("--rows", type=int, default=40, help="Terminal rows (default 40)")
    smoke.add_argument(
        "--wait-readiness",
        default=r"Raven",
        help='Regex to await as readiness (default "Raven")',
    )
    smoke.add_argument(
        "--wait-timeout",
        type=float,
        default=10.0,
        help="Readiness wait timeout in seconds (default 10.0)",
    )
    smoke.add_argument(
        "--exit-timeout",
        type=float,
        default=5.0,
        help="Final exit wait timeout in seconds (default 5.0)",
    )
    smoke.add_argument("--cwd", default=None, help="Subprocess working directory")
    smoke.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="Inject env var (repeatable)",
    )
    smoke.add_argument("--verbose", action="store_true", help="Echo timing detail")

    return parser


def _parse_env(items: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--env entry must be KEY=VAL, got: {item!r}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def _run_smoke(args: argparse.Namespace) -> int:
    env_overrides = _parse_env(args.env)
    h = Harness(cols=args.cols, rows=args.rows, env=env_overrides, cwd=args.cwd)

    backend_name = "tui-use"
    print(f"[smoke] backend={backend_name} command={args.command!r}")

    try:
        t0 = time.monotonic()
        h.spawn(args.command)
        print(f"[ok] spawn ({int((time.monotonic() - t0) * 1000)}ms)")

        readiness_pattern = re.compile(args.wait_readiness)
        t0 = time.monotonic()
        ready = h.wait(readiness_pattern, timeout=args.wait_timeout)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if ready:
            print(f"[ok] readiness {args.wait_readiness!r} ({elapsed_ms}ms)")
        else:
            print(f"[fail] readiness {args.wait_readiness!r} timed out ({elapsed_ms}ms)")

        t0 = time.monotonic()
        # Polite Ctrl+C; some TUIs need two presses (cancel-then-exit).
        try:
            h.press("ctrl+c")
            time.sleep(0.3)
            h.press("ctrl+c")
        except HarnessError:
            pass  # subprocess may have already exited
        exit_ok = h.expect_exit(0, timeout=args.exit_timeout)
        actual = h._cached_exit_code if h._killed else h._poll_exit_code(timeout=0.0)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if exit_ok:
            print(f"[ok] terminate ({elapsed_ms}ms)")
        else:
            print(f"[fail] terminate exit={actual!r} ({elapsed_ms}ms)")

        final_screen = h.dump()
        print(f"=== final screen ({args.cols}x{args.rows}) ===")
        for row in final_screen:
            print(row)
        print(f"=== exit code: {0 if (ready and exit_ok) else 1} ===")
        return 0 if (ready and exit_ok) else 1
    finally:
        # Idempotent — safe even if subprocess already exited.
        try:
            h.kill()
        except Exception:
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse exits 2 on bad args / unknown subcommands; let it propagate.
        return int(e.code) if e.code is not None else 2

    if args.subcommand == "smoke":
        try:
            return _run_smoke(args)
        except ExtrasMissingError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except SpawnError as e:
            print(f"error: spawn failed: {e}", file=sys.stderr)
            return 2
        except BackendError as e:
            print(f"error: backend failure: {e}", file=sys.stderr)
            return 2
        except HarnessError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    print(f"error: unknown subcommand {args.subcommand!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
