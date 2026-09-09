"""Tests for the ``raven trajectory regression`` CLI subapp."""

from __future__ import annotations

import json
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


def _plain(output: str) -> str:
    """Output with whitespace collapsed: Rich wraps long problem lines (they
    embed tmp paths) at the console width, splitting asserted phrases."""
    return " ".join(output.split())


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
    assert "case.yaml is missing" in _plain(r.stdout)


def test_validate_all_flags_the_broken_directory_among_valid_ones(tmp_path) -> None:
    _case_copy(tmp_path, "case_a")
    _case_copy(tmp_path, "case_b")
    (tmp_path / "broken").mkdir()

    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path)])

    assert r.exit_code == 1
    assert "broken" in r.stdout
    assert "expect.yaml is missing" in _plain(r.stdout)
    assert "3 case(s)" in r.stdout


def test_validate_all_passing_cases_exit_0(tmp_path) -> None:
    _case_copy(tmp_path, "case_a")
    _case_copy(tmp_path, "case_b")

    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path)])

    assert r.exit_code == 0, r.output
    assert "2 case(s), 0 problem(s)" in r.stdout


def _mutate_spans(case: Path, mutate) -> None:
    spans_path = case / "cassette" / "spans.jsonl"
    spans = [json.loads(x) for x in spans_path.read_text(encoding="utf-8").splitlines()]
    mutate(spans)
    spans_path.write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


def _first_ref(case: Path, suffix: str) -> str:
    spans_path = case / "cassette" / "spans.jsonl"
    for line in spans_path.read_text(encoding="utf-8").splitlines():
        for key, ref in (json.loads(line).get("attributes") or {}).items():
            if key.endswith(suffix):
                return ref
    raise AssertionError(f"no reference for {suffix}")


def test_validate_all_survives_malformed_cases_and_still_checks_the_rest(tmp_path) -> None:
    """Structurally broken cases of every known shape must be reported as
    problems, not raise and cut the --all sweep short of the last, valid
    directory."""

    def bad_attrs(spans):
        spans[0]["attributes"] = ["not", "a", "mapping"]

    def bad_span_id(spans):
        spans[0]["spanId"] = ["bad"]

    bad_yaml = _case_copy(tmp_path, "a_bad_yaml")
    (bad_yaml / "case.yaml").write_text("issue: [\n", encoding="utf-8")
    _mutate_spans(_case_copy(tmp_path, "b_bad_attrs"), bad_attrs)
    bad_payload = _case_copy(tmp_path, "c_bad_payload")
    (bad_payload / "cassette" / _first_ref(bad_payload, "tool.output.artifact_path")).write_text(
        "[1]", encoding="utf-8"
    )
    _mutate_spans(_case_copy(tmp_path, "d_bad_span_id"), bad_span_id)
    int_keys = _case_copy(tmp_path, "e_int_keys")
    (int_keys / "case.yaml").write_text("42: foo\nbadkey: bar\n", encoding="utf-8")
    _case_copy(tmp_path, "z_good")

    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path)])

    assert r.exit_code == 1
    plain = _plain(r.stdout)
    assert "cannot be parsed as YAML" in plain
    assert "attributes must be a mapping" in plain
    assert "must hold a JSON object" in plain
    assert "spanId must be a string" in plain
    assert "keys must be strings" in plain
    assert "z_good" in plain
    assert "6 case(s)" in plain


def test_validate_all_missing_root_exits_1(tmp_path) -> None:
    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path / "nope")])
    assert r.exit_code == 1
    assert "is not a directory" in _plain(r.stdout)


def test_validate_all_empty_root_exits_1(tmp_path) -> None:
    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path)])
    assert r.exit_code == 1
    assert "no case directories" in _plain(r.stdout)


def test_validate_requires_exactly_one_of_case_dir_or_all(tmp_path) -> None:
    case = _case_copy(tmp_path)

    both = runner.invoke(trajectory_app, ["regression", "validate", str(case), "--all"])
    neither = runner.invoke(trajectory_app, ["regression", "validate"])

    assert both.exit_code == 1
    assert neither.exit_code == 1
    assert "exactly one of CASE_DIR or --all" in _plain(both.stdout)
