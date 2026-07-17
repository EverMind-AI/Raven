"""Unit tests for the design-step sandbox (benchmarks.appworld.evolve.sandbox).

The sandbox is the boundary between an LLM editing freely and the candidate
that actually gets committed: whitelist refusal, out-of-whitelist reversion,
and exact change/deletion capture. A hole here means a candidate silently
carries (or drops) edits the designer never verified.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.appworld.evolve.sandbox import Sandbox  # noqa: E402

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin"}


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
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, env=_ENV, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True, env=_ENV).stdout.strip()
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
    def test_command_naming_origin_repo_is_refused(self, sandbox):
        out = sandbox.bash(f"cat {sandbox.repo_root.resolve()}/grader.py")
        assert "refused" in out and "origin repo" in out

    def test_command_runs_inside_worktree(self, sandbox):
        out = sandbox.bash("pwd")
        assert str(sandbox.root) in out and "[exit 0]" in out


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
