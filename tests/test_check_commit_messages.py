"""The commit-range linter over a real git repo, merge commits and exclusions included."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from scripts import check_commit_messages


def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ["PATH"],
    }
    return subprocess.check_output(["git", *args], cwd=cwd, text=True, env=env).strip()


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "feat: seed initial file")


def test_range_with_merge_commit_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "feat: add feature file")

    _git(repo, "checkout", "-q", "main")
    (repo / "c.txt").write_text("c\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "feat: add main file")

    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "--no-ff", "-m", "Merge branch 'main' into feature", "main")

    cwd = Path.cwd()
    try:
        os.chdir(repo)
        exit_code = check_commit_messages.main([f"{base}..HEAD"])
    finally:
        os.chdir(cwd)

    assert exit_code == 0


# Not a literal: this file is under the repo's English-source rule, and the case
# needs a message that breaks the ASCII rule at runtime.
_NON_ASCII = "feat: add a file named \u2014 the dash"


def _merge_history(repo: Path) -> str:
    """main gains one message that fails the gate; feature merges main in."""
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "feat: add feature file")

    _git(repo, "checkout", "-q", "main")
    (repo / "c.txt").write_text("c\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", _NON_ASCII)

    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "--no-ff", "-m", "Merge branch 'main' into feature", "main")
    return base


def _run(repo: Path, *revisions: str) -> int:
    cwd = Path.cwd()
    try:
        os.chdir(repo)
        return check_commit_messages.main(list(revisions))
    finally:
        os.chdir(cwd)


def test_the_default_branch_history_is_linted_without_an_exclusion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _merge_history(repo)

    assert _run(repo, f"{base}..HEAD") == 1


def test_excluding_the_default_branch_skips_the_history_it_carries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _merge_history(repo)

    assert _run(repo, f"{base}..HEAD", "^main") == 0


def test_the_exclusion_still_lints_the_branch_its_own_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _merge_history(repo)

    (repo / "d.txt").write_text("d\n")
    _git(repo, "add", "d.txt")
    _git(repo, "commit", "-qm", _NON_ASCII)

    assert _run(repo, f"{base}..HEAD", "^main") == 1
