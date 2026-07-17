"""Unit tests for the git primitives the evolver builds candidates with.

These run against the USER'S real repo in production (the subject checkout),
so the invariants under test are the dangerous ones: the main working tree is
never touched, ephemeral worktrees are cleaned up on every exit path, and
evolver commits stay reachable across git gc via plain refs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from raven.evolver.tree import git_ops

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin"}


@pytest.fixture(autouse=True)
def _reset_ephemeral_root():
    yield
    git_ops.set_ephemeral_root(None)


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "subject"
    (repo / "src").mkdir(parents=True)
    (repo / "src/x.py").write_text("x = 1\n")
    (repo / "old.txt").write_text("legacy\n")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, env=_ENV, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True, env=_ENV).stdout.strip()
    return repo, sha


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, env=_ENV,
                          capture_output=True, text=True).stdout


class TestCommitFilesAsChild:
    def test_edit_delete_and_create_land_in_the_child(self, repo):
        repo_dir, sha = repo
        child, changed = git_ops.commit_files_as_child(
            repo_dir, sha,
            {"src/x.py": b"x = 2\n", "src/new.py": b"y = 1\n"},
            "evolver: candidate",
            deletions=("old.txt",),
        )
        assert child != sha
        assert _git(repo_dir, "rev-parse", f"{child}^").strip() == sha
        assert _git(repo_dir, "show", f"{child}:src/x.py") == "x = 2\n"
        assert _git(repo_dir, "show", f"{child}:src/new.py") == "y = 1\n"
        tree = _git(repo_dir, "ls-tree", "-r", "--name-only", child)
        assert "old.txt" not in tree
        assert sorted(changed) == ["old.txt", "src/new.py", "src/x.py"]

    def test_main_working_tree_and_head_are_untouched(self, repo):
        repo_dir, sha = repo
        git_ops.commit_files_as_child(
            repo_dir, sha, {"src/x.py": b"x = 99\n"}, "evolver: candidate")
        assert (repo_dir / "src/x.py").read_text() == "x = 1\n"
        assert _git(repo_dir, "rev-parse", "HEAD").strip() == sha
        assert _git(repo_dir, "status", "--porcelain") == ""
        assert _git(repo_dir, "worktree", "list").count("\n") == 1  # main only

    def test_unknown_parent_refused(self, repo):
        repo_dir, _ = repo
        with pytest.raises(ValueError, match="not a known commit"):
            git_ops.commit_files_as_child(
                repo_dir, "f" * 40, {"src/x.py": b""}, "msg")


class TestWorktreeAt:
    def test_yields_checkout_and_cleans_up(self, repo):
        repo_dir, sha = repo
        with git_ops.worktree_at(repo_dir, sha) as wt:
            assert (wt / "src/x.py").read_text() == "x = 1\n"
            kept = wt
        assert not kept.exists()
        assert "evolver-wt-" not in _git(repo_dir, "worktree", "list")

    def test_cleans_up_on_exception(self, repo):
        repo_dir, sha = repo
        with pytest.raises(RuntimeError, match="boom"):
            with git_ops.worktree_at(repo_dir, sha) as wt:
                kept = wt
                raise RuntimeError("boom")
        assert not kept.exists()
        assert "evolver-wt-" not in _git(repo_dir, "worktree", "list")

    def test_ephemeral_root_redirects_tempdirs(self, repo, tmp_path):
        repo_dir, sha = repo
        root = tmp_path / "run_tmp"
        git_ops.set_ephemeral_root(root)
        with git_ops.worktree_at(repo_dir, sha) as wt:
            assert str(wt).startswith(str(root))
        git_ops.set_ephemeral_root(None)
        with git_ops.worktree_at(repo_dir, sha) as wt:
            assert not str(wt).startswith(str(root))


class TestRefs:
    def test_ref_anchors_evolver_commit_against_gc(self, repo):
        repo_dir, sha = repo
        child, _ = git_ops.commit_files_as_child(
            repo_dir, sha, {"src/x.py": b"x = 3\n"}, "evolver: candidate")
        git_ops.create_ref(repo_dir, "refs/evolver/n1", child)
        assert _git(repo_dir, "rev-parse", "refs/evolver/n1").strip() == child
        # Overwrite is allowed (re-promotion re-points the ref).
        git_ops.create_ref(repo_dir, "refs/evolver/n1", sha)
        assert _git(repo_dir, "rev-parse", "refs/evolver/n1").strip() == sha
        git_ops.delete_ref(repo_dir, "refs/evolver/n1")
        git_ops.delete_ref(repo_dir, "refs/evolver/n1")  # absent: no-op
        proc = subprocess.run(["git", "rev-parse", "refs/evolver/n1"],
                              cwd=repo_dir, env=_ENV, capture_output=True)
        assert proc.returncode != 0
