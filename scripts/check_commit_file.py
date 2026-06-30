from __future__ import annotations

import sys
from pathlib import Path

from scripts.commit_lint import check_commit_message


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage: check_commit_file.py <commit-msg-file>", file=sys.stderr)
        return 2

    message = Path(args[0]).read_text()
    result = check_commit_message(message)
    if result.ok:
        return 0

    print("Invalid commit message:", file=sys.stderr)
    for error in result.errors:
        print(f"- {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
