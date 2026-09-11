"""CI gate against oversized files and report-asset file types in a revision range."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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

# The asset ban targets report assets and standalone web artifacts. A product
# frontend legitimately carries its own entry HTML and icon SVGs as source, so
# these trees are exempt from the extension list. The size limit still applies.
APP_SOURCE_PREFIXES = (
    "bridge/",
    # "ui-web/" is not a prefix of "ui-tui/" -- the slash ends it -- so each
    # entry matches its own tree and nothing else.
    "ui-web/",
    "ui-tui/",
)
ALLOWED_SKILL_REFERENCE_IMAGE_EXTENSIONS = frozenset({".jpg"})
# The one home of the raven-design skill plates: the design-engine wheel they
# migrated to (verdict C4; the frozen fork seat retired with the tree). The
# rule is unchanged: .jpg only, only under a skill's references/, and the
# 1 MiB ceiling still applies.
RAVEN_DESIGN_SKILL_PREFIXES = (
    (
        "plugins-dist",
        "design-engine",
        "raven_design",
        "skills",
    ),
)

# The ten bundled deck templates the ppt engine offers a task that brings no
# template of its own. They are tracked because a fresh clone that cannot build
# a deck is not an install, and seven of them clear the 1 MiB ceiling; the
# maintainer approved that exception when they entered git.
#
# The roster is written out here rather than read from the engine's manifest.
# A gate that takes its own exception list from a file in the tree it is
# checking grants exceptions to whoever edits that file, which is the change
# under review -- adding an oversized file plus a manifest entry would pass.
# AGENTS.md section 7 wants a per-file maintainer decision, so an eleventh
# template has to be added here, in the diff a reviewer reads, and the manifest
# goes on verifying that the bytes are the ones that were approved.
BUNDLED_DECK_TEMPLATE_DIR = "plugins-dist/ppt-engine/raven_ppt/assets/templates"
BUNDLED_DECK_TEMPLATE_MANIFEST = "plugins-dist/ppt-engine/templates.manifest.json"
PINNED_DECK_TEMPLATES = frozenset(
    {
        "amber_wave_quarterly_summary.pptx",
        "beige_geometric_general_report.pptx",
        "black_circuit_tech_launch.pptx",
        "blue_minimal_general_analysis.pptx",
        "gold_panel_year_end_summary.pptx",
        "green_aurora_tech_trends.pptx",
        "mint_memphis_thesis_defense.pptx",
        "red_chinese_traditional_culture.pptx",
        "teal_illustrated_work_analysis.pptx",
        "warm_bauhaus_quarterly_review.pptx",
    }
)


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
        if _is_pinned_deck_template(path):
            continue
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
        if path.startswith(APP_SOURCE_PREFIXES):
            continue
        candidate = root / path
        if not candidate.is_file():
            continue
        extension = candidate.suffix.lower()
        if extension in BLOCKED_ASSET_EXTENSIONS and not _is_allowed_skill_reference_image(path, extension):
            violations.append(BlockedAssetViolation(path=path, extension=extension))
    return violations


def manifest_deck_template_names(root: Path) -> frozenset[str]:
    """The template filenames the ppt engine's manifest pins, empty when it cannot be read.

    Not what the gate allows -- `PINNED_DECK_TEMPLATES` is that, and says why.
    This reads the other side so a test can hold the two together and fail when
    the manifest and the approved roster drift apart.
    """
    try:
        manifest = json.loads((root / BUNDLED_DECK_TEMPLATE_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    files = manifest.get("files")
    if not isinstance(files, list):
        return frozenset()
    return frozenset(str(entry["name"]) for entry in files if isinstance(entry, dict) and "name" in entry)


def _is_pinned_deck_template(path: str) -> bool:
    parent, _, name = path.rpartition("/")
    return parent == BUNDLED_DECK_TEMPLATE_DIR and name in PINNED_DECK_TEMPLATES


def _is_allowed_skill_reference_image(path: str, extension: str) -> bool:
    if extension not in ALLOWED_SKILL_REFERENCE_IMAGE_EXTENSIONS:
        return False
    parts = PurePosixPath(path).parts
    if ".." in parts:
        return False
    for prefix in RAVEN_DESIGN_SKILL_PREFIXES:
        prefix_length = len(prefix)
        if (
            len(parts) >= prefix_length + 3
            and parts[:prefix_length] == prefix
            and parts[prefix_length + 1] == "references"
        ):
            return True
    return False


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
