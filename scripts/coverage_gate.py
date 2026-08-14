"""Coverage reporting and governance gates shared by CI and local development."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
DEFAULT_TOLERANCE = 0.05
# Glob magic so ** matches zero-or-more path components; the default pathspec
# raven/**/*.py drops top-level files like raven/__init__.py because plain * spans
# slashes and the middle separator then has nothing to match.
PRODUCTION_PATHSPEC = ":(glob)raven/**/*.py"
DEFAULT_DIFF_THRESHOLD = 90.0
OMITTED_PATHS = {
    "raven/__main__.py",
    "raven/evolver/__main__.py",
    "raven/utils/win_fcntl_shim.py",
}


@dataclass(frozen=True)
class Metrics:
    statements: int
    covered_lines: int
    branches: int
    covered_branches: int

    @property
    def line_percent(self) -> float:
        return _percent(self.covered_lines, self.statements)

    @property
    def branch_percent(self) -> float:
        return _percent(self.covered_branches, self.branches)


@dataclass(frozen=True)
class DiffCoverage:
    executable: frozenset[tuple[str, int]]
    uncovered: frozenset[tuple[str, int]]
    zero_coverage_files: tuple[str, ...]
    unmeasured_files: tuple[str, ...]

    @property
    def percent(self) -> float:
        return _percent(len(self.executable) - len(self.uncovered), len(self.executable))


def _percent(covered: int, total: int) -> float:
    return 100.0 if total == 0 else covered * 100.0 / total


def _metrics(summary: dict[str, Any]) -> Metrics:
    return Metrics(
        statements=int(summary["num_statements"]),
        covered_lines=int(summary["covered_lines"]),
        branches=int(summary["num_branches"]),
        covered_branches=int(summary["covered_branches"]),
    )


def load_coverage(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        report = json.load(handle)
    if not isinstance(report.get("files"), dict) or not isinstance(report.get("totals"), dict):
        raise ValueError(f"{path} is not a coverage.py JSON report")
    return report


def print_summary(report: dict[str, Any], limit: int) -> None:
    totals = _metrics(report["totals"])
    print(f"Line coverage:   {totals.line_percent:.2f}% ({totals.covered_lines}/{totals.statements})")
    print(f"Branch coverage: {totals.branch_percent:.2f}% ({totals.covered_branches}/{totals.branches})")
    print(f"Lowest {limit} production files by combined coverage:")
    ranked = sorted(
        (
            (float(data["summary"]["percent_covered"]), name, _metrics(data["summary"]))
            for name, data in report["files"].items()
            if int(data["summary"]["num_statements"]) > 0
        ),
        key=lambda item: (item[0], item[1]),
    )
    for combined, name, metrics in ranked[:limit]:
        print(f"  {combined:6.2f}%  line {metrics.line_percent:6.2f}%  branch {metrics.branch_percent:6.2f}%  {name}")


def create_baseline(report: dict[str, Any], commit: str) -> dict[str, Any]:
    files = {
        name: _metrics_payload(_metrics(data["summary"]))
        for name, data in sorted(report["files"].items())
        if int(data["summary"]["num_statements"]) > 0
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "raven",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "reference_commit": commit,
        "totals": _metrics_payload(_metrics(report["totals"])),
        "files": files,
    }


def _metrics_payload(metrics: Metrics) -> dict[str, int | float]:
    return {
        "statements": metrics.statements,
        "covered_lines": metrics.covered_lines,
        "line_percent": round(metrics.line_percent, 6),
        "branches": metrics.branches,
        "covered_branches": metrics.covered_branches,
        "branch_percent": round(metrics.branch_percent, 6),
    }


def write_baseline(report: dict[str, Any], output: Path, commit: str) -> None:
    output.write_text(json.dumps(create_baseline(report, commit), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote coverage baseline candidate to {output}")


def check_baseline_update(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    if current.get("schema_version") != SCHEMA_VERSION or previous.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("current and target-branch baselines must use the supported schema")
    passed = True
    for metric in ("line_percent", "branch_percent"):
        value = float(current["totals"][metric])
        target = float(previous["totals"][metric])
        delta = value - target
        print(f"Baseline {metric}: proposed={value:.6f}% target={target:.6f}% delta={delta:+.6f}pp")
        if value < target:
            passed = False
    if passed:
        print("Coverage baseline update is monotonic.")
    else:
        print("Coverage baseline files may not lower line or branch coverage.")
    return passed


def check_ratchet(report: dict[str, Any], baseline: dict[str, Any], tolerance: float, limit: int) -> bool:
    if baseline.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported baseline schema: {baseline.get('schema_version')!r}")
    current = _metrics(report["totals"])
    expected = baseline["totals"]
    checks = (
        ("line", current.line_percent, float(expected["line_percent"])),
        ("branch", current.branch_percent, float(expected["branch_percent"])),
    )
    failed = [(name, value, target) for name, value, target in checks if value + tolerance < target]
    for name, value, target in checks:
        delta = value - target
        print(f"Ratchet {name}: current={value:.2f}% baseline={target:.2f}% delta={delta:+.2f}pp")
    if not failed:
        print(f"Coverage ratchet passed with {tolerance:.2f}pp tolerance.")
        return True

    print("Largest per-file coverage declines:")
    declines = _file_declines(report, baseline)
    for decline, name, line_delta, branch_delta in declines[:limit]:
        print(f"  {decline:6.2f}pp  line {line_delta:+7.2f}pp  branch {branch_delta:+7.2f}pp  {name}")
    return False


def _file_declines(report: dict[str, Any], baseline: dict[str, Any]) -> list[tuple[float, str, float, float]]:
    declines: list[tuple[float, str, float, float]] = []
    current_files = report["files"]
    for name, expected in baseline["files"].items():
        data = current_files.get(name)
        if data is None:
            continue
        current = _metrics(data["summary"])
        line_delta = current.line_percent - float(expected["line_percent"])
        branch_delta = current.branch_percent - float(expected["branch_percent"])
        decline = max(-line_delta, -branch_delta, 0.0)
        if decline > 0:
            declines.append((decline, name, line_delta, branch_delta))
    return sorted(declines, key=lambda item: (-item[0], item[1]))


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_changed_lines(diff: str) -> dict[str, set[int]]:
    """Return new-side hunk lines; deleted lines never enter the denominator."""
    changed: dict[str, set[int]] = {}
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            changed.setdefault(path, set())
            continue
        match = _HUNK_RE.match(line)
        if path is None or match is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        changed[path].update(range(start, start + count))
    return changed


def calculate_diff_coverage(
    report: dict[str, Any], changed: dict[str, set[int]], added_files: Iterable[str] = ()
) -> DiffCoverage:
    """Intersect changed lines with coverage.py statements to ignore comments and blanks."""
    executable: set[tuple[str, int]] = set()
    uncovered: set[tuple[str, int]] = set()
    zero_coverage_files: list[str] = []
    unmeasured_files: list[str] = []
    added = set(added_files)
    for name, lines in changed.items():
        data = report["files"].get(name)
        if data is None:
            if name not in OMITTED_PATHS:
                unmeasured_files.append(name)
            continue
        executed = set(data["executed_lines"])
        missing = set(data["missing_lines"])
        statements = executed | missing
        for line in lines & statements:
            item = (name, line)
            executable.add(item)
            if line in missing:
                uncovered.add(item)
        if name in added and statements and not executed:
            zero_coverage_files.append(name)
    return DiffCoverage(
        frozenset(executable),
        frozenset(uncovered),
        tuple(sorted(zero_coverage_files)),
        tuple(sorted(unmeasured_files)),
    )


def _git(*args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise OSError("git is required for coverage diff checks")
    completed = subprocess.run([executable, *args], check=True, capture_output=True, text=True)
    return completed.stdout


def load_baseline_from_ref(base_ref: str, path: Path) -> dict[str, Any] | None:
    _git("rev-parse", "--verify", f"{base_ref}^{{commit}}")
    try:
        content = _git("show", f"{base_ref}:{path.as_posix()}")
    except subprocess.CalledProcessError:
        return None
    return json.loads(content)


def git_diff_inputs(base_ref: str, head: str | None) -> tuple[dict[str, set[int]], set[str]]:
    """Resolve the merge base and collect Python changes for CI or the local worktree."""
    merge_base = _git("merge-base", base_ref, "HEAD").strip()
    end = head or ""
    range_args = [merge_base, end] if end else [merge_base]
    diff = _git("diff", "--unified=0", "--no-color", "--diff-filter=ACMR", *range_args, "--", PRODUCTION_PATHSPEC)
    names = _git("diff", "--name-only", "--diff-filter=A", *range_args, "--", PRODUCTION_PATHSPEC)
    return parse_changed_lines(diff), {name for name in names.splitlines() if name}


def check_diff(report: dict[str, Any], base_ref: str, head: str | None, threshold: float) -> bool:
    changed, added = git_diff_inputs(base_ref, head)
    result = calculate_diff_coverage(report, changed, added)
    covered = len(result.executable) - len(result.uncovered)
    print(f"Diff coverage: {result.percent:.2f}% ({covered}/{len(result.executable)} executable changed lines)")
    if result.uncovered:
        print("Uncovered changed lines:")
        for name, line in sorted(result.uncovered):
            print(f"  {name}:{line}")
    if result.zero_coverage_files:
        print("New production files with 0% line coverage:")
        for name in result.zero_coverage_files:
            print(f"  {name}")
    if result.unmeasured_files:
        print("Changed production files missing from the coverage report:")
        for name in result.unmeasured_files:
            print(f"  {name}")
    passed = result.percent + 1e-9 >= threshold and not result.zero_coverage_files and not result.unmeasured_files
    if passed:
        print(f"Diff coverage passed the {threshold:.2f}% threshold.")
    else:
        print(f"Diff coverage failed the {threshold:.2f}% threshold.")
    return passed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-json", type=Path, default=Path("coverage.json"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary = subparsers.add_parser("summary")
    summary.add_argument("--limit", type=int, default=10)

    baseline = subparsers.add_parser("baseline")
    baseline.add_argument("--output", type=Path, default=Path("coverage-baseline-candidate.json"))
    baseline.add_argument("--commit", default="HEAD")

    baseline_check = subparsers.add_parser("baseline-check")
    baseline_check.add_argument("--baseline", type=Path, default=Path(".github/coverage-baseline.json"))
    baseline_check.add_argument("--base-ref", required=True)

    ratchet = subparsers.add_parser("ratchet")
    ratchet.add_argument("--baseline", type=Path, default=Path(".github/coverage-baseline.json"))
    ratchet.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    ratchet.add_argument("--limit", type=int, default=10)

    diff = subparsers.add_parser("diff")
    diff.add_argument("--base-ref", required=True)
    diff.add_argument("--head", default=None)
    diff.add_argument("--threshold", type=float, default=DEFAULT_DIFF_THRESHOLD)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "baseline-check":
            with args.baseline.open(encoding="utf-8") as handle:
                current_baseline = json.load(handle)
            previous_baseline = load_baseline_from_ref(args.base_ref, args.baseline)
            if previous_baseline is None:
                print(f"No coverage baseline exists on {args.base_ref}; accepting the initial bootstrap baseline.")
                return 0
            return 0 if check_baseline_update(current_baseline, previous_baseline) else 1

        report = load_coverage(args.coverage_json)
        if args.command == "summary":
            print_summary(report, args.limit)
            return 0
        if args.command == "baseline":
            commit = _git("rev-parse", args.commit).strip()
            write_baseline(report, args.output, commit)
            return 0
        if args.command == "ratchet":
            with args.baseline.open(encoding="utf-8") as handle:
                baseline = json.load(handle)
            return 0 if check_ratchet(report, baseline, args.tolerance, args.limit) else 1
        if args.command == "diff":
            return 0 if check_diff(report, args.base_ref, args.head, args.threshold) else 1
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"coverage gate error: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
