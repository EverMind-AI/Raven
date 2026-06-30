from __future__ import annotations

import os
import subprocess
import sys

from scripts.commit_lint import check_commit_message

ZERO_SHA = "0" * 40


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    revision_range = args[0] if args else _default_range()
    if not revision_range:
        print("No commit range detected; skipping commit message lint")
        return 0

    messages = _commit_messages(revision_range)
    failed = False
    for sha, message in messages:
        result = check_commit_message(message)
        if result.ok:
            continue
        failed = True
        header = message.strip().splitlines()[0] if message.strip() else "<empty>"
        print(f"Invalid commit message {sha}: {header}", file=sys.stderr)
        for error in result.errors:
            print(f"- {error}", file=sys.stderr)

    return 1 if failed else 0


def _default_range() -> str | None:
    pr_base = os.environ.get("GITHUB_PR_BASE_SHA", "").strip()
    if pr_base:
        return f"{pr_base}..HEAD"

    before = os.environ.get("GITHUB_EVENT_BEFORE", "").strip()
    if before and before != ZERO_SHA:
        return f"{before}..HEAD"

    return os.environ.get("RANGE", "").strip() or None


def _commit_messages(revision_range: str) -> list[tuple[str, str]]:
    output = subprocess.check_output(
        ["git", "log", "--format=%H%x00%B%x00", revision_range],
        text=True,
    )
    chunks = output.rstrip("\0").split("\0")
    return list(zip(chunks[0::2], chunks[1::2], strict=False))


if __name__ == "__main__":
    raise SystemExit(main())
