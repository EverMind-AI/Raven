"""Lint a PR body read from a file path or the PR_BODY env var; a path takes precedence.

The body becomes the squash commit body, so it must be ASCII-only. With neither
a path nor a non-empty PR_BODY there is nothing to lint, so the script exits 2
instead of passing silently.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Running the file puts scripts/ on sys.path, not the repo root that holds the package.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.commit_lint import check_pr_body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_pr_body.py",
        description=(
            "Lint a PR body, which becomes the squash commit body. The body is read from PATH "
            "when one is given, otherwise from the PR_BODY environment variable. A PATH takes "
            "precedence over PR_BODY, which is then ignored with a warning."
        ),
        epilog=(
            "Exit status: 0 when the body is ASCII-only, 1 when it fails the lint, 2 when no "
            "body is supplied or PATH cannot be read."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="read the PR body from this file instead of the PR_BODY variable",
    )
    ns = parser.parse_args(argv)

    if ns.path is not None:
        if os.environ.get("PR_BODY"):
            print("warning: PR_BODY is set but ignored because a path was given", file=sys.stderr)
        try:
            body = Path(ns.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"cannot read PR body from {ns.path}: {exc}", file=sys.stderr)
            return 2
    else:
        body = os.environ.get("PR_BODY", "")

    if not body.strip():
        print("no PR body supplied: pass a file path or set PR_BODY", file=sys.stderr)
        return 2

    result = check_pr_body(body)
    if result.ok:
        return 0

    for lineno, line in enumerate(body.splitlines(), start=1):
        if any(ord(ch) > 0x7F for ch in line):
            print(f"Invalid PR body line {lineno}: {line}", file=sys.stderr)
    for error in result.errors:
        print(f"- {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
