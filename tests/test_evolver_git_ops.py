"""Unit tests for the git primitives the evolver builds candidates with.

These run against the USER'S real repo in production (the subject checkout),
so the invariants under test are the dangerous ones: the main working tree is
never touched, ephemeral worktrees are cleaned up on every exit path, and
evolver commits stay reachable across git gc via plain refs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolver.applier.path_guard import ImmutablePathError, UnsafePathError
from evolver.tree import git_ops

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


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
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, env=_ENV, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True, env=_ENV
    ).stdout.strip()
    return repo, sha


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, env=_ENV, capture_output=True, text=True).stdout


class TestCommitFilesAsChild:
    def test_edit_delete_and_create_land_in_the_child(self, repo):
        repo_dir, sha = repo
        child, changed = git_ops.commit_files_as_child(
            repo_dir,
            sha,
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
        git_ops.commit_files_as_child(repo_dir, sha, {"src/x.py": b"x = 99\n"}, "evolver: candidate")
        assert (repo_dir / "src/x.py").read_text() == "x = 1\n"
        assert _git(repo_dir, "rev-parse", "HEAD").strip() == sha
        assert _git(repo_dir, "status", "--porcelain") == ""
        assert _git(repo_dir, "worktree", "list").count("\n") == 1  # main only

    def test_unknown_parent_refused(self, repo):
        repo_dir, _ = repo
        with pytest.raises(ValueError, match="not a known commit"):
            git_ops.commit_files_as_child(repo_dir, "f" * 40, {"src/x.py": b""}, "msg")


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


class TestCaptureFiles:
    def test_temp_files_follow_the_ephemeral_root(self, tmp_path, monkeypatch):
        root = tmp_path / "run_tmp"
        git_ops.set_ephemeral_root(root)
        seen: list = []
        real_temporary_file = git_ops.tempfile.TemporaryFile

        def recording_temporary_file(*args, **kwargs):
            seen.append(kwargs.get("dir"))
            return real_temporary_file(*args, **kwargs)

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(git_ops.tempfile, "TemporaryFile", recording_temporary_file)
        monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
        git_ops.run_capture(["git"], cwd=tmp_path)

        assert seen == [str(root), str(root)]


class TestGitTimeouts:
    def test_run_wraps_a_timeout_as_git_op_error(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(git_ops.subprocess, "run", boom)
        with pytest.raises(git_ops.GitOpError, match="timed out"):
            git_ops._run(tmp_path, "status")

    def test_run_capture_returns_after_the_timeout(self, tmp_path):
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            git_ops.run_capture(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                cwd=tmp_path,
                timeout=0.3,
            )
        assert time.monotonic() - started < 2.0

    def test_run_capture_uses_files_not_pipes(self, tmp_path, monkeypatch):
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            kwargs["stdout"].write(b"out")
            kwargs["stderr"].write(b"err")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
        code, out, err = git_ops.run_capture(["git"], cwd=tmp_path)

        assert code == 0
        assert out == "out"
        assert err == "err"
        assert seen["stdout"] is not subprocess.PIPE
        assert hasattr(seen["stdout"], "write")
        assert seen["stdin"] == subprocess.DEVNULL
        assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"

    def test_run_forces_git_terminal_prompt_over_caller_env(self, tmp_path, monkeypatch):
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
        git_ops._run(tmp_path, "status", env={"GIT_TERMINAL_PROMPT": "1"})

        assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"

    def test_run_capture_does_not_wait_for_a_grandchild(self, tmp_path):
        # Only Windows can fail this: subprocess.run waits for the grandchild's
        # pipe handles there, while POSIX waits for the direct child alone, so
        # both the pipe and the file shape return at the timeout.
        code = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); "
            "print('parent', flush=True); time.sleep(5)"
        )
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            git_ops.run_capture([sys.executable, "-c", code], cwd=tmp_path, timeout=0.3)
        assert time.monotonic() - started < 3.0
        assert b"parent" in (exc.value.output or b"")

    def test_run_wraps_timeout_with_partial_stderr(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1, output=b"stdout-part", stderr=b"stderr-part")

        monkeypatch.setattr(git_ops.subprocess, "run", boom)
        with pytest.raises(git_ops.GitOpError) as exc:
            git_ops._run(tmp_path, "status")
        assert "stderr-part" in str(exc.value)
        assert str(exc.value).count("stdout-part") == 1

    def test_commit_exists_propagates_a_timeout(self, repo, monkeypatch):
        repo_dir, _sha = repo

        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(git_ops.subprocess, "run", boom)
        with pytest.raises(git_ops.GitOpError, match="timed out"):
            git_ops.commit_exists(repo_dir, "deadbeef")

    def test_read_file_at_wraps_a_timeout(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(git_ops.subprocess, "run", boom)
        with pytest.raises(git_ops.GitOpError) as exc:
            git_ops.read_file_at(tmp_path, "0" * 40, "src/x.py")
        assert exc.value.returncode == -1
        assert "timed out" in str(exc.value)

    def test_branch_exists_wraps_a_timeout(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(git_ops.subprocess, "run", boom)
        with pytest.raises(git_ops.GitOpError) as exc:
            git_ops.branch_exists(tmp_path, "main")
        assert exc.value.returncode == -1
        assert "timed out" in str(exc.value)

    def test_branch_exists_reports_local_branches(self, repo):
        repo_dir, sha = repo
        git_ops.create_branch(repo_dir, "evolver-test", sha)
        assert git_ops.branch_exists(repo_dir, "evolver-test")
        assert not git_ops.branch_exists(repo_dir, "evolver-missing")


class TestRecombineTimeout:
    def test_timeout_is_re_raised_but_other_git_errors_return_none(self, tmp_path, monkeypatch):
        from evolver.orchestrator.production import make_git_recombine_fn

        recombine = make_git_recombine_fn(tmp_path)
        parent = SimpleNamespace(node_id="n0")
        elite = SimpleNamespace(files=("src/x.py",), git_commit_sha="deadbeef")

        def timed_out(*args, **kwargs):
            raise git_ops.GitOpError(cmd=["git"], returncode=-1, stderr="timed out after 60.0s")

        monkeypatch.setattr(git_ops, "read_file_at", timed_out)
        with pytest.raises(git_ops.GitOpError):
            recombine(parent, elite)

        def missing_path(*args, **kwargs):
            raise git_ops.GitOpError(cmd=["git"], returncode=128, stderr="path not found")

        monkeypatch.setattr(git_ops, "read_file_at", missing_path)
        assert recombine(parent, elite) is None


class TestRefs:
    def test_ref_anchors_evolver_commit_against_gc(self, repo):
        repo_dir, sha = repo
        child, _ = git_ops.commit_files_as_child(repo_dir, sha, {"src/x.py": b"x = 3\n"}, "evolver: candidate")
        git_ops.create_ref(repo_dir, "refs/evolver/n1", child)
        assert _git(repo_dir, "rev-parse", "refs/evolver/n1").strip() == child
        # Overwrite is allowed (re-promotion re-points the ref).
        git_ops.create_ref(repo_dir, "refs/evolver/n1", sha)
        assert _git(repo_dir, "rev-parse", "refs/evolver/n1").strip() == sha
        git_ops.delete_ref(repo_dir, "refs/evolver/n1")
        git_ops.delete_ref(repo_dir, "refs/evolver/n1")  # absent: no-op
        proc = subprocess.run(["git", "rev-parse", "refs/evolver/n1"], cwd=repo_dir, env=_ENV, capture_output=True)
        assert proc.returncode != 0


def _commit_symlink(repo_dir: Path, link: str, target: str) -> str:
    path = repo_dir / link
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, path)
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-qm", "add symlink")
    return _git(repo_dir, "rev-parse", "HEAD").strip()


class TestCommitFilesAsChildPathEscape:
    @pytest.mark.parametrize("rel", ["/tmp/evolver-escape.txt", "../outside.txt"])
    def test_refuses_unsafe_paths(self, repo, rel):
        repo_dir, sha = repo
        with pytest.raises(ValueError, match="unsafe path"):
            git_ops.commit_files_as_child(repo_dir, sha, {rel: b"pwned"}, "bad path")

    @pytest.mark.skipif(os.name == "nt", reason="creating symlinks may need privileges")
    def test_refuses_to_write_through_a_link_to_an_immutable_file(self, repo):
        repo_dir, _sha = repo
        sha = _commit_symlink(repo_dir, "link_kernel", "raven/agent/loop/main.py")
        with pytest.raises(Exception, match="immutable"):
            git_ops.commit_files_as_child(repo_dir, sha, {"link_kernel": b"PWNED"}, "bad link")

    @pytest.mark.skipif(os.name == "nt", reason="creating symlinks may need privileges")
    def test_refuses_to_write_through_a_link_to_outside(self, repo, tmp_path):
        repo_dir, _sha = repo
        outside = tmp_path / "outside.txt"
        outside.write_text("keep")
        sha = _commit_symlink(repo_dir, "link_out", str(outside))
        with pytest.raises(ValueError, match="refusing to write outside the worktree"):
            git_ops.commit_files_as_child(repo_dir, sha, {"link_out": b"PWNED"}, "bad link")
        assert outside.read_text() == "keep"

    @pytest.mark.skipif(os.name == "nt", reason="creating symlinks may need privileges")
    def test_refuses_to_delete_a_link_to_an_immutable_file(self, repo):
        repo_dir, _sha = repo
        sha = _commit_symlink(repo_dir, "link_kernel", "raven/agent/loop/main.py")
        with pytest.raises(ImmutablePathError, match="immutable"):
            git_ops.commit_files_as_child(repo_dir, sha, {}, "bad link", deletions=("link_kernel",))
        child, _ = git_ops.commit_files_as_child(repo_dir, sha, {"src/x.py": b"x = 2\n"}, "after refusal")
        assert _git(repo_dir, "show", f"{child}:link_kernel") == "raven/agent/loop/main.py"

    @pytest.mark.skipif(os.name == "nt", reason="creating symlinks may need privileges")
    def test_refuses_to_delete_through_a_link_to_an_immutable_dir(self, repo):
        repo_dir, _sha = repo
        (repo_dir / "raven/agent/loop").mkdir(parents=True)
        (repo_dir / "raven/agent/loop/main.py").write_text("KERNEL\n")
        sha = _commit_symlink(repo_dir, "linkdir", "raven/agent/loop")
        with pytest.raises(ImmutablePathError, match="immutable"):
            git_ops.commit_files_as_child(repo_dir, sha, {}, "bad link", deletions=("linkdir/main.py",))
        child, _ = git_ops.commit_files_as_child(repo_dir, sha, {"src/x.py": b"x = 2\n"}, "after refusal")
        assert _git(repo_dir, "show", f"{child}:raven/agent/loop/main.py") == "KERNEL\n"

    @pytest.mark.skipif(os.name == "nt", reason="creating symlinks may need privileges")
    def test_allows_deleting_through_a_link_to_a_mutable_dir(self, repo):
        repo_dir, _sha = repo
        (repo_dir / "mutable").mkdir()
        (repo_dir / "mutable/file.txt").write_text("gone\n")
        sha = _commit_symlink(repo_dir, "mutabledir", "mutable")
        child, changed = git_ops.commit_files_as_child(
            repo_dir, sha, {}, "delete through mutable dir link", deletions=("mutabledir/file.txt",)
        )
        assert changed == ["mutable/file.txt"]
        assert "mutable/file.txt" not in _git(repo_dir, "ls-tree", "-r", "--name-only", child)
        assert _git(repo_dir, "show", f"{child}:mutabledir") == "mutable"

    @pytest.mark.skipif(os.name == "nt", reason="creating symlinks may need privileges")
    def test_refuses_to_delete_through_a_link_to_outside(self, repo, tmp_path):
        repo_dir, _sha = repo
        outside = tmp_path / "outside_dir"
        outside.mkdir()
        (outside / "victim").write_text("keep")
        sha = _commit_symlink(repo_dir, "linkdir", str(outside))
        with pytest.raises(ValueError, match="refusing to delete outside the worktree"):
            git_ops.commit_files_as_child(repo_dir, sha, {}, "bad link", deletions=("linkdir/victim",))
        assert (outside / "victim").read_text() == "keep"

    def test_refuses_unsafe_deletions(self, repo):
        repo_dir, sha = repo
        with pytest.raises(UnsafePathError, match="unsafe path"):
            git_ops.commit_files_as_child(repo_dir, sha, {}, "bad path", deletions=("../outside.txt",))

    @pytest.mark.skipif(os.name == "nt", reason="creating symlinks may need privileges")
    def test_deletes_the_link_not_its_mutable_target(self, repo):
        repo_dir, _sha = repo
        (repo_dir / "src/target.txt").write_text("keep")
        sha = _commit_symlink(repo_dir, "link_ok", "src/target.txt")
        child, changed = git_ops.commit_files_as_child(repo_dir, sha, {}, "delete link", deletions=("link_ok",))
        assert changed == ["link_ok"]
        assert _git(repo_dir, "show", f"{child}:src/target.txt") == "keep"
        assert "link_ok" not in _git(repo_dir, "ls-tree", "-r", "--name-only", child)

    @pytest.mark.skipif(os.name == "nt", reason="creating symlinks may need privileges")
    def test_deletes_a_link_that_points_outside_the_worktree(self, repo, tmp_path):
        repo_dir, _sha = repo
        outside = tmp_path / "outside.txt"
        outside.write_text("keep")
        sha = _commit_symlink(repo_dir, "link_out", str(outside))
        child, changed = git_ops.commit_files_as_child(repo_dir, sha, {}, "delete link", deletions=("link_out",))
        assert changed == ["link_out"]
        assert outside.read_text() == "keep"

    @pytest.mark.skipif(os.name == "nt", reason="creating symlinks may need privileges")
    def test_reports_a_symlink_loop_cleanly(self, repo):
        repo_dir, _sha = repo
        sha = _commit_symlink(repo_dir, "loop", "loop")
        with pytest.raises(ValueError, match="symlink loop"):
            git_ops.commit_files_as_child(repo_dir, sha, {"loop": b"x"}, "loop")

    @pytest.mark.skipif(os.name == "nt", reason="creating symlinks may need privileges")
    def test_reports_a_symlink_loop_on_delete_cleanly(self, repo):
        repo_dir, _sha = repo
        sha = _commit_symlink(repo_dir, "loop", "loop")
        with pytest.raises(ValueError, match="symlink loop"):
            git_ops.commit_files_as_child(repo_dir, sha, {}, "loop", deletions=("loop",))

    @pytest.mark.skipif(os.name == "nt", reason="creating symlinks may need privileges")
    def test_reports_a_symlink_loop_in_a_parent_component_cleanly(self, repo):
        repo_dir, _sha = repo
        sha = _commit_symlink(repo_dir, "loop", "loop")
        with pytest.raises(ValueError, match="symlink loop"):
            git_ops.commit_files_as_child(repo_dir, sha, {}, "loop", deletions=("loop/x",))
