"""CI gate: a release that changes SOUL.md must keep the outgoing text under legacy/."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ZERO_SHA = "0" * 40
SOUL = "raven/templates/SOUL.md"
LEGACY_DIR = "raven/templates/legacy"


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=root)


def _default_range() -> str | None:
    pr_base = os.environ.get("GITHUB_PR_BASE_SHA", "").strip()
    if pr_base:
        return f"{pr_base}..HEAD"

    before = os.environ.get("GITHUB_EVENT_BEFORE", "").strip()
    if before and before != ZERO_SHA:
        return f"{before}..HEAD"

    return os.environ.get("RANGE", "").strip() or None


def check(revision_range: str, root: Path | None = None) -> list[str]:
    """Return one problem when SOUL.md changed without its outgoing text in legacy/."""
    root = root or Path.cwd()
    base = revision_range.split("..", 1)[0]
    if not base:
        return []
    try:
        merge_base = _git(root, "merge-base", base, "HEAD").decode("ascii").strip()
    except subprocess.CalledProcessError:
        return []
    if not merge_base:
        return []
    try:
        changed = _git(root, "diff", "--name-only", f"{merge_base}..HEAD", "--", SOUL)
    except subprocess.CalledProcessError:
        return []
    if not changed.strip():
        return []
    try:
        previous = _git(root, "show", f"{merge_base}:{SOUL}")
    except subprocess.CalledProcessError:
        return []
    try:
        current = (root / SOUL).read_bytes()
    except OSError:
        return []
    previous_digest = _digest(previous)
    if previous_digest == _digest(current):
        return []
    legacy = root / LEGACY_DIR
    if legacy.is_dir():
        for entry in legacy.iterdir():
            if entry.is_file() and entry.name.endswith(".md") and _digest(entry.read_bytes()) == previous_digest:
                return []
    return [
        f"{SOUL} changed, but the outgoing text is not under {LEGACY_DIR}/. "
        "A release that changes SOUL.md must copy the old text there so an older "
        "install still has a merge base."
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when SOUL.md changes without its outgoing text in legacy/.")
    parser.add_argument("revision_range", nargs="?", default=_default_range())
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.revision_range:
        print("No commit range detected; skipping SOUL legacy check")
        return 0
    problems = check(args.revision_range)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
