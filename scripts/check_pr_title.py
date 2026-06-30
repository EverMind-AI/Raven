from __future__ import annotations

import os
import sys

from scripts.commit_lint import check_pr_title


def main() -> int:
    title = os.environ.get("PR_TITLE", "").strip()
    if not title:
        print("PR_TITLE is required", file=sys.stderr)
        return 2

    result = check_pr_title(title)
    if result.ok:
        return 0

    print(f"Invalid PR title: {title}", file=sys.stderr)
    for error in result.errors:
        print(f"- {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
