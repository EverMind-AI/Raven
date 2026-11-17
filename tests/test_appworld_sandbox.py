"""Unit tests for the design-step sandbox (benchmarks.appworld.evolve.sandbox).

The sandbox is the boundary between an LLM editing freely and the candidate
that actually gets committed: whitelist refusal, out-of-whitelist reversion,
and exact change/deletion capture. A hole here means a candidate silently
carries (or drops) edits the designer never verified.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.appworld.evolve import sandbox as sandbox_module  # noqa: E402
from benchmarks.appworld.evolve.sandbox import Sandbox  # noqa: E402

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


@pytest.fixture()
def subject(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "subject"
    (repo / "benchmarks/appworld").mkdir(parents=True)
    (repo / "benchmarks/__init__.py").touch()
    (repo / "benchmarks/appworld/__init__.py").touch()
    (repo / "benchmarks/appworld/agent_cli.py").write_text("PROMPT = 'v1'\n")
    (repo / "raven/agent").mkdir(parents=True)
    (repo / "raven/agent/loop.py").write_text("x = 1\n")
    (repo / "grader.py").write_text("score = 1\n")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, env=_ENV, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True, env=_ENV
    ).stdout.strip()
    return repo, sha


@pytest.fixture()
def sandbox(subject, tmp_path):
    repo, sha = subject
    sb = Sandbox(repo, tmp_path / "wt", sha)
    yield sb
    sb.close()


class TestWriteRefusal:
    def test_out_of_whitelist_write_is_refused(self, sandbox):
        msg = sandbox.write_text("grader.py", "score = 999\n")
        assert "refused" in msg and "whitelist" in msg
        assert (sandbox.root / "grader.py").read_text() == "score = 1\n"

    def test_path_escape_is_refused(self, sandbox):
        assert "escapes" in sandbox.write_text("benchmarks/../../etc/x", "")

    def test_whitelist_write_lands(self, sandbox):
        msg = sandbox.write_text("benchmarks/appworld/agent_cli.py", "PROMPT = 'v2'\n")
        assert msg.startswith("[wrote")
        assert (sandbox.root / "benchmarks/appworld/agent_cli.py").read_text() == "PROMPT = 'v2'\n"


class TestBashBoundary:
    def test_command_naming_subject_checkout_is_refused(self, sandbox):
        out = sandbox.bash(f"cat {sandbox.repo_root.resolve()}/grader.py")
        assert "refused" in out and "subject checkout" in out

    def test_command_runs_inside_worktree(self, sandbox):
        out = sandbox.bash("pwd")
        assert str(sandbox.root) in out and "[exit 0]" in out

    def test_alternately_spelled_subject_path_is_refused(self, sandbox):
        alt = str(sandbox.repo_root.resolve()).replace("\\", "/").upper()
        out = sandbox.bash(f"ls {alt}")
        assert "refused" in out and "subject checkout" in out

    @pytest.mark.slow
    def test_timeout_after_subject_write_is_detected(self, sandbox):
        cmd = "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && touch timeout-poison && sleep 5"
        try:
            with pytest.raises(RuntimeError, match="subject checkout"):
                sandbox.bash(cmd, timeout=1)
            # The guard undoes what the command wrote, so nothing is left behind.
            assert not (sandbox.repo_root / "timeout-poison").exists()
        finally:
            (sandbox.repo_root / "timeout-poison").unlink(missing_ok=True)

    @pytest.mark.slow
    def test_timeout_kills_a_late_origin_writer(self, sandbox):
        cmd = "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && sleep 2 && touch late-poison"
        poison = sandbox.repo_root / "late-poison"
        try:
            out = sandbox.bash(cmd, timeout=0.5)
            assert "TIMEOUT" in out
            time.sleep(2.5)
            assert not poison.exists()
        finally:
            poison.unlink(missing_ok=True)

    @pytest.mark.slow
    def test_background_writer_is_killed_before_snapshot(self, sandbox):
        if sys.platform == "win32":
            pytest.skip("process-group cleanup is POSIX-only")
        cmd = "(sleep 2; cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\"; touch bg-poison) >/dev/null 2>&1 &"
        poison = sandbox.repo_root / "bg-poison"
        try:
            out = sandbox.bash(cmd)
            assert "[exit 0]" in out
            time.sleep(2.5)
            assert not poison.exists()
        finally:
            poison.unlink(missing_ok=True)

    def test_assume_unchanged_index_flag_is_detected(self, sandbox):
        cmd = (
            "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && "
            "printf 'score = 999\\n' > grader.py && git update-index --assume-unchanged grader.py"
        )
        with pytest.raises(RuntimeError, match="subject checkout"):
            sandbox.bash(cmd)
        # The restore puts the file and the index back the way they were.
        assert (sandbox.repo_root / "grader.py").read_text() == "score = 1\n"
        flags = subprocess.run(
            ["git", "ls-files", "-v", "-z", "grader.py"],
            cwd=sandbox.repo_root,
            check=True,
            capture_output=True,
            text=True,
            env=_ENV,
        ).stdout
        assert flags.startswith("H ")

    @pytest.mark.parametrize(
        ("rel", "index_flag"),
        [
            ("grader.py", None),
            ("existing-untracked.py", None),
            ("grader.py", "--assume-unchanged"),
            ("grader.py", "--skip-worktree"),
        ],
    )
    def test_existing_file_content_write_is_detected(self, sandbox, rel, index_flag):
        (sandbox.repo_root / rel).write_text("score = 2\n")
        if index_flag:
            subprocess.run(["git", "update-index", index_flag, rel], cwd=sandbox.repo_root, check=True, env=_ENV)
        command = f"cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && printf 'score = 999\\n' > {rel}"
        with pytest.raises(RuntimeError, match="subject checkout"):
            sandbox.bash(command)
        # The write is undone: the file is back to what it was before the command.
        assert (sandbox.repo_root / rel).read_text() == "score = 2\n"

    def test_failed_status_probe_refuses_command(self, sandbox):
        index = sandbox.repo_root / ".git/index"
        original = index.read_bytes()
        try:
            index.write_bytes(b"invalid index\n")
            out = sandbox.bash("touch command-was-run")
            assert "refused" in out and "state unavailable" in out
            assert not (sandbox.root / "command-was-run").exists()
        finally:
            index.write_bytes(original)

    @pytest.mark.parametrize("probe", ["for-each-ref", "ls-files"])
    def test_failed_internal_probe_refuses_command(self, sandbox, monkeypatch, probe):
        run_git = sandbox._git

        def fail_probe(*args):
            if args[0] == probe:
                return run_git("rev-parse", "--verify", "refs/heads/nonexistent-probe-ref")
            return run_git(*args)

        monkeypatch.setattr(sandbox, "_git", fail_probe)
        out = sandbox.bash("touch command-was-run")
        assert "refused" in out and "state unavailable" in out
        assert not (sandbox.root / "command-was-run").exists()

    def test_failed_after_probe_aborts_round(self, sandbox):
        index = sandbox.repo_root / ".git/index"
        original = index.read_bytes()
        command = "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && printf 'invalid index\\n' > .git/index"
        try:
            with pytest.raises(RuntimeError, match="subject checkout unverifiable"):
                sandbox.bash(command)
        finally:
            index.write_bytes(original)

    def test_hook_executable_mode_change_is_detected(self, sandbox):
        if sys.platform == "win32":
            pytest.skip("executable hook permissions require POSIX")
        hook = sandbox.repo_root / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o644)
        command = "d=$(sed -n 's/^gitdir: //p' .git); chmod +x \"$d/../../hooks/post-checkout\""
        with pytest.raises(RuntimeError, match="subject checkout"):
            sandbox.bash(command)
        # The restore puts the hook's mode back: it is not executable again.
        assert not hook.stat().st_mode & 0o111

    def test_detached_origin_head_allows_commands(self, sandbox):
        subprocess.run(["git", "checkout", "--detach", "-q"], cwd=sandbox.repo_root, check=True, env=_ENV)
        assert "[exit 0]" in sandbox.bash("echo healthy")

    def test_missing_tracked_and_unusual_untracked_paths_allow_commands(self, sandbox):
        (sandbox.repo_root / "grader.py").unlink()
        name = "untracked file.txt" if sys.platform == "win32" else "untracked file\nname.txt"
        (sandbox.repo_root / name).write_text("unchanged\n")
        assert "[exit 0]" in sandbox.bash("echo healthy")

    @pytest.mark.skipif(sys.platform == "win32", reason="non-UTF-8 filenames require POSIX")
    def test_non_utf8_untracked_file_write_is_detected(self, sandbox):
        name = "untracked-\udcff.txt"
        (sandbox.repo_root / name).write_text("original\n")
        command = f"cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && printf changed > {name}"
        with pytest.raises(RuntimeError, match="subject checkout"):
            sandbox.bash(command)

    def test_missing_bash_is_reported_not_raised(self, sandbox, monkeypatch):
        state = sandbox._subject_state(capture=False)
        monkeypatch.setattr(sandbox, "_subject_state", lambda capture=False: state)

        def _raise(*_args, **_kwargs):
            raise FileNotFoundError("bash")

        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", _raise)
            out = sandbox.bash("echo hi")
        assert "bash error" in out and "FileNotFoundError" in out

    def test_ref_deletion_in_common_dir_is_detected(self, sandbox, subject):
        repo, _sha = subject
        subprocess.run(["git", "update-ref", "refs/evolver/probe", "HEAD"], cwd=repo, check=True, env=_ENV)
        with pytest.raises(RuntimeError, match="subject checkout"):
            sandbox.bash("git update-ref -d refs/evolver/probe")
        # The restore puts the ref back.
        assert (
            subprocess.run(
                ["git", "rev-parse", "--verify", "refs/evolver/probe"],
                cwd=repo,
                capture_output=True,
                env=_ENV,
            ).returncode
            == 0
        )

    def test_symbolic_head_change_to_another_branch_is_detected(self, sandbox, subject):
        repo, _sha = subject
        subprocess.run(["git", "branch", "other"], cwd=repo, check=True, env=_ENV)
        command = "d=$(sed -n 's/^gitdir: //p' .git); git -C \"$d/../../..\" checkout -q other"
        with pytest.raises(RuntimeError, match="subject checkout"):
            sandbox.bash(command)

    def test_relative_gitdir_resolves_against_worktree(self, subject, tmp_path):
        repo, sha = subject
        subprocess.run(["git", "config", "worktree.useRelativePaths", "true"], cwd=repo, check=True, env=_ENV)
        sb = Sandbox(repo, tmp_path / "wt-rel", sha)
        try:
            gitdir = sb._subject_gitdir()
            assert gitdir is not None and gitdir.exists()
            assert str(repo.resolve()) in str(gitdir)
        finally:
            sb.close()

    def test_gitdir_command_substitution_is_detected(self, sandbox):
        poison = sandbox.repo_root / "sandbox-escape"
        command = "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && touch sandbox-escape"
        try:
            with pytest.raises(RuntimeError, match="subject checkout"):
                sandbox.bash(command)
            # The guard undoes the write, so the escape leaves nothing behind.
            assert not poison.exists()
        finally:
            poison.unlink(missing_ok=True)

    def test_refs_created_in_the_worktree_do_not_abort_the_round(self, sandbox):
        # A branch, a tag and a stash the driver creates in its own worktree
        # land in the shared ref namespace; they say nothing about a write to
        # the subject checkout, so they must not cost the round.
        assert "[exit 0]" in sandbox.bash("git checkout -b feature")
        assert "[exit 0]" in sandbox.bash("git tag probe-tag")
        assert "[exit 0]" in sandbox.bash("git stash")
        assert "[exit 0]" in sandbox.bash("echo healthy")

    def test_detected_write_is_undone(self, sandbox):
        hook = sandbox.repo_root / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o644)
        command = (
            "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && "
            "printf 'score = 999\\n' > grader.py && touch escape-poison && "
            "chmod +x .git/hooks/post-checkout"
        )
        with pytest.raises(RuntimeError, match="subject checkout"):
            sandbox.bash(command)
        assert (sandbox.repo_root / "grader.py").read_text() == "score = 1\n"
        assert not (sandbox.repo_root / "escape-poison").exists()
        assert not hook.stat().st_mode & 0o111

    def test_detected_write_restores_git_config(self, sandbox):
        config = sandbox.repo_root / ".git/config"
        original = config.read_bytes()
        command = "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && git config core.pager evil-pager"
        with pytest.raises(RuntimeError, match="subject checkout"):
            sandbox.bash(command)
        assert config.read_bytes() == original

    def test_same_size_rewrite_of_a_dirty_file_is_detected(self, sandbox):
        # git diff names the path that changed but not what it changed to, so
        # a rewrite that keeps the size has to be caught by the content.
        (sandbox.repo_root / "grader.py").write_text("score = 2\n")
        command = "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && printf 'score = 3\\n' > grader.py"
        with pytest.raises(RuntimeError, match="subject checkout"):
            sandbox.bash(command)
        assert (sandbox.repo_root / "grader.py").read_text() == "score = 2\n"

    def test_staged_new_file_is_undone(self, sandbox):
        command = (
            "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && printf 'x = 1\\n' > staged.py && git add staged.py"
        )
        with pytest.raises(RuntimeError, match="subject checkout"):
            sandbox.bash(command)
        assert not (sandbox.repo_root / "staged.py").exists()

    def test_a_discarded_edit_is_put_back(self, sandbox):
        # A command that throws the checkout's own edit away must not be able
        # to lose it: the guard undoes what the command did.
        (sandbox.repo_root / "grader.py").write_text("score = 2\n")
        command = "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && git checkout -- ."
        with pytest.raises(RuntimeError, match="subject checkout"):
            sandbox.bash(command)
        assert (sandbox.repo_root / "grader.py").read_text() == "score = 2\n"

    def test_the_capture_budget_names_the_paths_it_cannot_restore(self, sandbox, monkeypatch):
        # Over the capture budget the guard has no content to put back, so it
        # names the path instead of replacing the checkout's own edit with the
        # index version.
        monkeypatch.setattr(sandbox_module, "_RESTORE_CAPTURE_FILES", 0)
        (sandbox.repo_root / "grader.py").write_text("score = 2\n")
        (sandbox.repo_root / "raven/agent/loop.py").write_text("x = 2\n")
        command = "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && printf 'score = 999\\n' > grader.py"
        with pytest.raises(RuntimeError, match="could not undo") as excinfo:
            sandbox.bash(command)
        assert "grader.py" in str(excinfo.value)
        # The path the command changed is named; the path it did not touch
        # keeps the checkout's own edit.
        assert (sandbox.repo_root / "grader.py").read_text() == "score = 999\n"
        assert (sandbox.repo_root / "raven/agent/loop.py").read_text() == "x = 2\n"

    @pytest.mark.skipif(sys.platform == "win32", reason="file permissions are POSIX-only")
    def test_an_unreadable_file_is_not_emptied(self, sandbox):
        # A file this process may not read must not be recorded as an empty
        # one: the restore would write that emptiness back over it.
        target = sandbox.repo_root / "grader.py"
        target.write_text("score = 2\n")
        target.chmod(0o000)
        try:
            token, content = sandbox._path_state("grader.py")
            before = sandbox._subject_state(capture=True)
            after = sandbox._subject_state(capture=True)
        finally:
            target.chmod(0o644)
        assert token == "unreadable"
        assert content is None
        if before is not None and after is not None:
            problems: list[str] = []
            sandbox._restore_worktree(before, after, problems, [])
            assert not problems
        assert target.read_text() == "score = 2\n"

    def test_a_file_created_during_the_command_is_named(self, sandbox):
        command = "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && touch appeared.txt"
        with pytest.raises(RuntimeError, match="removed: appeared.txt"):
            sandbox.bash(command)

    def test_a_stat_preserving_write_is_undone(self, sandbox):
        # A copy that keeps the file's size and mtime leaves git's stat cache
        # agreeing with the index. The restore has to act on the paths the
        # guard caught, not on the paths git still calls dirty, and it has to
        # write the index version itself: a checkout does nothing when git
        # believes the file is unchanged.
        target = sandbox.repo_root / "grader.py"
        original = target.read_bytes()
        payload = original.replace(b"1", b"2")
        assert len(payload) == len(original)
        index = sandbox.repo_root / ".git/index"
        stat = target.stat()
        # The index looks as old as the file, so git re-reads the content of
        # the file instead of trusting its cached stat.
        os.utime(index, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        before = sandbox._subject_state(capture=True)
        # The command's write: the same size, the same mtime.
        target.write_bytes(payload)
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        after = sandbox._subject_state(capture=False)
        assert before is not None and after is not None
        assert before.differences(after) == ["tracked files"]
        problems, removed = sandbox._restore_subject(before, after)
        assert not problems
        assert target.read_bytes() == original

    def test_git_dir_in_the_environment_does_not_redirect_the_probe(self, sandbox, tmp_path, monkeypatch):
        # GIT_DIR and its friends override -C: a harness that exports one of
        # them would make every probe read a different repository than the
        # subject checkout.
        other = tmp_path / "other-repo"
        other.mkdir()
        (other / "other.py").write_text("value = 1\n")
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
            subprocess.run(cmd, cwd=other, check=True, env=_ENV, capture_output=True)
        # The context ends before the fixtures are torn down, so the teardown
        # does not run with the variable still exported.
        with monkeypatch.context() as patch:
            patch.setenv("GIT_DIR", str(other / ".git"))
            common = Path(sandbox._git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
            assert common.resolve() == (sandbox.repo_root / ".git").resolve()
            # A write to the subject's own configuration is still caught.
            command = "cd \"$(sed -n 's/^gitdir: //p' .git)/../../..\" && git config core.pager evil-pager"
            with pytest.raises(RuntimeError, match="subject checkout"):
                sandbox.bash(command)


class TestScopeRestore:
    def test_out_of_whitelist_edits_reverted_whitelist_kept(self, sandbox):
        # Simulate the driver escaping via bash: edit the grader (outside the
        # whitelist), create an outside file, and make a legit whitelist edit.
        (sandbox.root / "grader.py").write_text("score = 999\n")
        (sandbox.root / "stray.txt").write_text("junk\n")
        sandbox.write_text("raven/agent/loop.py", "x = 2\n")

        reverted = sandbox.scope_restore()

        assert sorted(reverted) == ["grader.py", "stray.txt"]
        assert (sandbox.root / "grader.py").read_text() == "score = 1\n"
        assert not (sandbox.root / "stray.txt").exists()
        assert (sandbox.root / "raven/agent/loop.py").read_text() == "x = 2\n"


class TestChangeCapture:
    def test_changed_and_deleted_whitelist_files(self, sandbox):
        sandbox.write_text("benchmarks/appworld/agent_cli.py", "PROMPT = 'v2'\n")
        sandbox.write_text("benchmarks/appworld/hints.md", "new file\n")
        (sandbox.root / "raven/agent/loop.py").unlink()

        changed = sandbox.changed_whitelist()
        assert changed == {
            "benchmarks/appworld/agent_cli.py": b"PROMPT = 'v2'\n",
            "benchmarks/appworld/hints.md": b"new file\n",
        }
        assert sandbox.deleted_whitelist() == ["raven/agent/loop.py"]
        assert sandbox.original("raven/agent/loop.py") == b"x = 1\n"

    def test_build_artifacts_are_not_captured(self, sandbox):
        pyc = sandbox.root / "benchmarks/appworld/__pycache__"
        pyc.mkdir()
        (pyc / "agent_cli.cpython-313.pyc").write_bytes(b"\x00")
        (sandbox.root / "benchmarks/appworld/x.pyc").write_bytes(b"\x00")
        assert sandbox.changed_whitelist() == {}


class TestImportCheck:
    def test_import_failure_surfaces_before_an_eval_is_burned(self, sandbox):
        sandbox.write_text("benchmarks/appworld/broken.py", "import missing_dep_xyz\n")
        ok, err = sandbox.import_check("benchmarks.appworld.broken")
        assert not ok and "missing_dep_xyz" in err

    def test_import_resolves_from_the_edited_worktree(self, sandbox):
        sandbox.write_text("benchmarks/appworld/probe.py", "VALUE = 42\n")
        ok, err = sandbox.import_check("benchmarks.appworld.probe")
        assert ok, err
