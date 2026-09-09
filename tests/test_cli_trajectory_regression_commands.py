"""Tests for the ``raven trajectory regression`` CLI subapp."""

from __future__ import annotations

import shutil
from pathlib import Path

from typer.testing import CliRunner

from raven.cli.trajectory_commands import trajectory_app

runner = CliRunner()

SAMPLE_CASE = Path(__file__).parent / "trajectories" / "sample_reproduces_recording"


def _case_copy(root: Path, name: str = "case_a") -> Path:
    case = root / name
    shutil.copytree(SAMPLE_CASE, case)
    return case


def test_registered_under_trajectory() -> None:
    """Invocations go through trajectory_app on purpose: a bare single-command
    sub-app promotes its one command to the entry point, which is not how the
    installed CLI is called."""
    r = runner.invoke(trajectory_app, ["regression", "validate", "--help"])
    assert r.exit_code == 0
    assert "validate" in r.stdout


def test_validate_single_passing_case_exits_0(tmp_path) -> None:
    case = _case_copy(tmp_path)

    r = runner.invoke(trajectory_app, ["regression", "validate", str(case)])

    assert r.exit_code == 0, r.output
    assert "case_a" in r.stdout
    assert "0 problem(s)" in r.stdout


def test_validate_single_broken_case_exits_1_and_names_the_problem(tmp_path) -> None:
    case = _case_copy(tmp_path)
    (case / "case.yaml").unlink()

    r = runner.invoke(trajectory_app, ["regression", "validate", str(case)])

    assert r.exit_code == 1
    assert "case.yaml is missing" in r.stdout


def test_validate_all_flags_the_broken_directory_among_valid_ones(tmp_path) -> None:
    _case_copy(tmp_path, "case_a")
    _case_copy(tmp_path, "case_b")
    (tmp_path / "broken").mkdir()

    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path)])

    assert r.exit_code == 1
    assert "broken" in r.stdout
    assert "expect.yaml is missing" in r.stdout
    assert "3 case(s)" in r.stdout


def test_validate_all_passing_cases_exit_0(tmp_path) -> None:
    _case_copy(tmp_path, "case_a")
    _case_copy(tmp_path, "case_b")

    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path)])

    assert r.exit_code == 0, r.output
    assert "2 case(s), 0 problem(s)" in r.stdout


def test_validate_all_missing_root_exits_1(tmp_path) -> None:
    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path / "nope")])
    assert r.exit_code == 1
    assert "is not a directory" in r.stdout


def test_validate_all_empty_root_exits_1(tmp_path) -> None:
    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path)])
    assert r.exit_code == 1
    assert "no case directories" in r.stdout


def test_validate_requires_exactly_one_of_case_dir_or_all(tmp_path) -> None:
    case = _case_copy(tmp_path)

    both = runner.invoke(trajectory_app, ["regression", "validate", str(case), "--all"])
    neither = runner.invoke(trajectory_app, ["regression", "validate"])

    assert both.exit_code == 1
    assert neither.exit_code == 1
    assert "exactly one of CASE_DIR or --all" in both.stdout
