from __future__ import annotations

from scripts.commit_lint import (
    CommitLintConfig,
    check_commit_message,
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
