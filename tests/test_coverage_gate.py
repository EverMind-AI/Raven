"""Unit tests for coverage summaries, diff selection, and ratchet enforcement."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.coverage_gate import (
    OMITTED_PATHS,
    calculate_diff_coverage,
    check_baseline_update,
    check_ratchet,
    create_baseline,
    git_diff_inputs,
    load_coverage,
    parse_changed_lines,
)


def _file_summary(
    *, statements: int, covered_lines: int, branches: int = 0, covered_branches: int = 0
) -> dict[str, int | float]:
    denominator = statements + branches
    numerator = covered_lines + covered_branches
    return {
        "covered_lines": covered_lines,
        "num_statements": statements,
        "percent_covered": 100.0 if denominator == 0 else numerator * 100.0 / denominator,
        "percent_covered_display": "0",
        "missing_lines": statements - covered_lines,
        "excluded_lines": 0,
        "num_branches": branches,
        "num_partial_branches": 0,
        "covered_branches": covered_branches,
        "missing_branches": branches - covered_branches,
    }


def _report(
    *,
    statements: int = 10,
    covered_lines: int = 8,
    branches: int = 4,
    covered_branches: int = 3,
) -> dict:
    summary = _file_summary(
        statements=statements,
        covered_lines=covered_lines,
        branches=branches,
        covered_branches=covered_branches,
    )
    return {
        "meta": {"branch_coverage": True},
        "files": {
            "raven/example.py": {
                "executed_lines": list(range(1, covered_lines + 1)),
                "missing_lines": list(range(covered_lines + 1, statements + 1)),
                "excluded_lines": [],
                "executed_branches": [],
                "missing_branches": [],
                "summary": summary,
            }
        },
        "totals": summary,
    }


def test_load_coverage_rejects_non_coverage_json(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"files": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="not a coverage.py JSON report"):
        load_coverage(path)


def test_documented_omissions_match_run_and_report_configuration() -> None:
    with Path("pyproject.toml").open("rb") as handle:
        coverage_config = tomllib.load(handle)["tool"]["coverage"]

    assert set(coverage_config["run"]["omit"]) == OMITTED_PATHS
    assert set(coverage_config["report"]["omit"]) == OMITTED_PATHS


def test_parse_changed_lines_uses_only_new_hunk_ranges() -> None:
    diff = """diff --git a/raven/example.py b/raven/example.py
--- a/raven/example.py
+++ b/raven/example.py
@@ -2,3 +2,4 @@
+replacement
@@ -20,0 +22,2 @@
+first
+second
diff --git a/tests/test_example.py b/tests/test_example.py
--- a/tests/test_example.py
+++ b/tests/test_example.py
@@ -1 +1 @@
-old
+new
"""

    assert parse_changed_lines(diff) == {
        "raven/example.py": {2, 3, 4, 5, 22, 23},
        "tests/test_example.py": {1},
    }


def test_diff_coverage_ignores_comments_deletions_and_non_executable_lines() -> None:
    report = _report()
    changed = {"raven/example.py": {2, 9, 10, 11, 12}}

    result = calculate_diff_coverage(report, changed)

    assert result.executable == {("raven/example.py", 2), ("raven/example.py", 9), ("raven/example.py", 10)}
    assert result.uncovered == {("raven/example.py", 9), ("raven/example.py", 10)}
    assert result.percent == pytest.approx(100 / 3)


def test_diff_coverage_flags_new_production_file_at_zero_percent() -> None:
    report = _report(covered_lines=0)

    result = calculate_diff_coverage(report, {"raven/example.py": set(range(1, 11))}, ["raven/example.py"])

    assert result.zero_coverage_files == ("raven/example.py",)
    assert result.percent == 0.0


def test_diff_coverage_fails_closed_for_unmeasured_production_file() -> None:
    result = calculate_diff_coverage(_report(), {"raven/new_module.py": {1}})

    assert result.unmeasured_files == ("raven/new_module.py",)


def test_diff_coverage_allows_documented_omitted_entry_point() -> None:
    result = calculate_diff_coverage(_report(), {"raven/__main__.py": {1}})

    assert result.unmeasured_files == ()


def test_git_diff_inputs_includes_top_level_raven_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    (tmp_path / "raven").mkdir()
    (tmp_path / "raven" / "__init__.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "raven" / "pkg").mkdir()
    (tmp_path / "raven" / "pkg" / "mod.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "add raven sources")

    monkeypatch.chdir(tmp_path)
    changed, added = git_diff_inputs(base, "HEAD")

    assert "raven/__init__.py" in changed
    assert "raven/pkg/mod.py" in changed
    assert "raven/__init__.py" in added


def test_create_baseline_records_separate_line_and_branch_metrics() -> None:
    baseline = create_baseline(_report(), "abc123")

    assert baseline["reference_commit"] == "abc123"
    assert baseline["python"] == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert baseline["totals"]["line_percent"] == 80.0
    assert baseline["totals"]["branch_percent"] == 75.0
    assert baseline["files"]["raven/example.py"]["covered_lines"] == 8


def test_baseline_update_cannot_lower_either_metric(capsys: pytest.CaptureFixture[str]) -> None:
    previous = create_baseline(_report(), "base")
    proposed = create_baseline(_report(covered_lines=9, covered_branches=2), "head")

    assert check_baseline_update(proposed, previous) is False
    output = capsys.readouterr().out
    assert "line_percent: proposed=90.000000% target=80.000000%" in output
    assert "branch_percent: proposed=50.000000% target=75.000000%" in output
    assert "may not lower" in output


def test_baseline_update_accepts_equal_or_higher_metrics() -> None:
    previous = create_baseline(_report(), "base")
    proposed = create_baseline(_report(covered_lines=9, covered_branches=4), "head")

    assert check_baseline_update(proposed, previous) is True


def test_ratchet_allows_small_tolerance(capsys: pytest.CaptureFixture[str]) -> None:
    baseline = create_baseline(_report(statements=10_000, covered_lines=8_001), "abc123")
    current = _report(statements=10_000, covered_lines=8_000)

    assert check_ratchet(current, baseline, tolerance=0.05, limit=10) is True
    assert "delta=-0.01pp" in capsys.readouterr().out


def test_ratchet_reports_total_and_largest_file_decline(capsys: pytest.CaptureFixture[str]) -> None:
    baseline = create_baseline(_report(), "abc123")
    current = _report(covered_lines=7, covered_branches=2)

    assert check_ratchet(current, baseline, tolerance=0.05, limit=10) is False
    output = capsys.readouterr().out
    assert "Ratchet line: current=70.00% baseline=80.00% delta=-10.00pp" in output
    assert "Ratchet branch: current=50.00% baseline=75.00% delta=-25.00pp" in output
    assert "raven/example.py" in output
