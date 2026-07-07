from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import check_commit_file
from scripts.commit_lint import (
    CommitLintConfig,
    check_commit_message,
    check_pr_body,
    check_pr_title,
)


def test_accepts_valid_conventional_commit_with_scope() -> None:
    result = check_commit_message("docs(readme): sharpen launch positioning")

    assert result.ok
    assert result.errors == []


def test_rejects_missing_conventional_commit_type() -> None:
    result = check_commit_message("update README")

    assert not result.ok
    assert "must match <type>(<scope>): <subject>" in result.errors[0]


def test_rejects_cjk_and_full_width_punctuation() -> None:
    result = check_commit_message("docs: 更新 README。")

    assert not result.ok
    assert "must be ASCII-only English" in result.errors


def test_rejects_uppercase_subject_and_trailing_period() -> None:
    result = check_commit_message("docs: Update README.")

    assert not result.ok
    assert "subject must start lowercase" in result.errors
    assert "subject must not end with punctuation" in result.errors


def test_rejects_subject_over_commit_limit() -> None:
    subject = "a" * 73
    result = check_commit_message(f"docs: {subject}")

    assert not result.ok
    assert "subject must be 72 characters or fewer" in result.errors


def test_rejects_fixup_and_merge_subjects() -> None:
    fixup = check_commit_message("fixup! docs: sharpen launch positioning")
    merge = check_commit_message("Merge branch 'main' into feature")

    assert not fixup.ok
    assert not merge.ok
    assert "fixup/squash commits are not allowed" in fixup.errors
    assert "merge commits are not allowed in PR ranges" in merge.errors


def test_pr_title_allows_longer_subject_limit() -> None:
    subject = "add launch-ready readme and contributor checks"
    result = check_pr_title(f"docs: {subject}", CommitLintConfig(pr_title_subject_limit=90))

    assert result.ok


def test_pr_title_rejects_subject_over_pr_limit() -> None:
    subject = "a" * 91
    result = check_pr_title(f"docs: {subject}", CommitLintConfig(pr_title_subject_limit=90))

    assert not result.ok
    assert "subject must be 90 characters or fewer" in result.errors


def test_pr_body_accepts_ascii_markdown() -> None:
    result = check_pr_body("## Summary\n\nPlain ASCII body - fine.\n")

    assert result.ok


def test_pr_body_rejects_em_dash() -> None:
    # The em-dash (U+2014) is the char that slipped past the CJK-only grep and
    # failed the post-merge commit lint once the PR body became the squash body.
    result = check_pr_body("drops the hint only — empty submits still ignored")

    assert not result.ok
    assert "must be ASCII-only English" in result.errors


def test_pr_body_allows_empty() -> None:
    assert check_pr_body("").ok


def test_commit_msg_hook_runs_commitlint_before_python_checker(monkeypatch, tmp_path: Path) -> None:
    message_file = tmp_path / "COMMIT_EDITMSG"
    message_file.write_text("docs: add launch notes\n")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], check: bool) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(check_commit_file.subprocess, "run", fake_run)

    assert check_commit_file.main([str(message_file)]) == 0
    assert calls == [
        [
            "npx",
            "--no-install",
            "commitlint",
            "--edit",
            str(message_file),
            "--config",
            "commitlint.config.cjs",
        ]
    ]
