from __future__ import annotations

import os
import sys

from scripts.commit_lint import check_pr_body


def main() -> int:
    body = os.environ.get("PR_BODY", "")
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
