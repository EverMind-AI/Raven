"""The PR-body gate CLI: the file argument, the PR_BODY fallback, and a missing body."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_pr_body

# Escapes, not literals: this file is under the repo's English-source rule and
# the case needs a character that breaks the ASCII rule at runtime.
_NON_ASCII = "drops the hint only — empty submits still ignored"
_ASCII = "## Summary\n\nPlain ASCII body.\n"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "body.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_file_argument_is_linted_and_rejects_non_ascii(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PR_BODY", raising=False)

    assert check_pr_body.main([str(_write(tmp_path, _NON_ASCII))]) == 1


def test_a_file_argument_is_linted_and_accepts_ascii(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PR_BODY", raising=False)

    assert check_pr_body.main([str(_write(tmp_path, _ASCII))]) == 0


def test_the_pr_body_env_var_still_rejects_non_ascii(monkeypatch) -> None:
    monkeypatch.setenv("PR_BODY", _NON_ASCII)

    assert check_pr_body.main([]) == 1


def test_the_pr_body_env_var_still_accepts_ascii(monkeypatch) -> None:
    monkeypatch.setenv("PR_BODY", _ASCII)

    assert check_pr_body.main([]) == 0


def test_no_body_supplied_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("PR_BODY", raising=False)

    assert check_pr_body.main([]) == 2
    assert "no PR body supplied" in capsys.readouterr().err


def test_an_empty_file_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PR_BODY", raising=False)

    assert check_pr_body.main([str(_write(tmp_path, ""))]) == 2


def test_a_missing_file_fails_naming_the_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("PR_BODY", raising=False)
    missing = tmp_path / "does-not-exist.md"

    assert check_pr_body.main([str(missing)]) == 2
    err = capsys.readouterr().err
    assert "cannot read PR body" in err
    assert str(missing) in err


def test_a_file_argument_wins_over_pr_body(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PR_BODY", _NON_ASCII)

    assert check_pr_body.main([str(_write(tmp_path, _ASCII))]) == 0
    assert "PR_BODY is set but ignored" in capsys.readouterr().err


def test_a_failing_file_argument_still_wins_over_a_good_pr_body(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PR_BODY", _ASCII)

    assert check_pr_body.main([str(_write(tmp_path, _NON_ASCII))]) == 1


def test_help_documents_both_sources_and_the_precedence(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        check_pr_body.main(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "PR_BODY" in out
    assert "precedence" in out


def test_the_module_entry_point_lints_a_file(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key != "PR_BODY"}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.check_pr_body", str(_write(tmp_path, _NON_ASCII))],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stderr


def test_the_script_entry_point_runs_outside_the_repo_root(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key not in {"PR_BODY", "PYTHONPATH"}}
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "check_pr_body.py"), str(_write(tmp_path, _NON_ASCII))],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stderr
