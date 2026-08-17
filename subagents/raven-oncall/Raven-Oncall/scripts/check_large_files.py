from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ZERO_SHA = "0" * 40
DEFAULT_MAX_BYTES = 1024 * 1024
MAX_BYTES_ENV = "CHECK_LARGE_FILES_MAX_BYTES"
BLOCKED_ASSET_EXTENSIONS = {
    ".apng",
    ".avi",
    ".avif",
    ".bmp",
    ".aac",
    ".flv",
    ".flac",
    ".gif",
    ".heic",
    ".heif",
    ".htm",
    ".html",
    ".ico",
    ".jfif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".oga",
    ".ogg",
    ".ogv",
    ".opus",
    ".pdf",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".wasm",
    ".webm",
    ".webmanifest",
    ".webp",
    ".wmv",
    ".wav",
}


@dataclass(frozen=True)
class FileSizeViolation:
    path: str
    size: int
    limit: int


@dataclass(frozen=True)
class BlockedAssetViolation:
    path: str
    extension: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when a PR adds oversized files or blocked report assets.")
    parser.add_argument("revision_range", nargs="?", default=_default_range())
    parser.add_argument("--max-bytes", type=_positive_int, default=_default_max_bytes())
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not args.revision_range:
        print("No commit range detected; skipping large file check")
        return 0

    paths = changed_paths(args.revision_range)
    size_violations = find_oversized_files(
        paths,
        max_bytes=args.max_bytes,
        root=args.root,
    )
    asset_violations = find_blocked_asset_files(paths, root=args.root)
    if not size_violations and not asset_violations:
        return 0

    if size_violations:
        print("Oversized files are not allowed in PRs:", file=sys.stderr)
        for violation in size_violations:
            print(
                f"- {violation.path}: {format_bytes(violation.size)} > {format_bytes(violation.limit)}",
                file=sys.stderr,
            )
    if asset_violations:
        print("Blocked asset files are not allowed in PRs:", file=sys.stderr)
        for violation in asset_violations:
            print(f"- {violation.path}: {violation.extension} files should be stored outside git", file=sys.stderr)
    return 1


def find_oversized_files(paths: list[str], *, max_bytes: int, root: Path) -> list[FileSizeViolation]:
    violations: list[FileSizeViolation] = []
    seen: set[str] = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        candidate = root / path
        if not candidate.is_file():
            continue
        size = candidate.stat().st_size
        if size > max_bytes:
            violations.append(FileSizeViolation(path=path, size=size, limit=max_bytes))
    return violations


def find_blocked_asset_files(paths: list[str], *, root: Path) -> list[BlockedAssetViolation]:
    violations: list[BlockedAssetViolation] = []
    seen: set[str] = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        candidate = root / path
        if not candidate.is_file():
            continue
        extension = candidate.suffix.lower()
        if extension in BLOCKED_ASSET_EXTENSIONS:
            violations.append(BlockedAssetViolation(path=path, extension=extension))
    return violations


def changed_paths(revision_range: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=AM", revision_range],
        text=True,
    )
    return [line for line in output.splitlines() if line]


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            if unit == "bytes":
                return f"{size} byte" if size == 1 else f"{size} bytes"
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _default_range() -> str | None:
    pr_base = os.environ.get("GITHUB_PR_BASE_SHA", "").strip()
    if pr_base:
        return f"{pr_base}..HEAD"

    before = os.environ.get("GITHUB_EVENT_BEFORE", "").strip()
    if before and before != ZERO_SHA:
        return f"{before}..HEAD"

    return os.environ.get("RANGE", "").strip() or None


def _default_max_bytes() -> int:
    raw = os.environ.get(MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    return _positive_int(raw)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
