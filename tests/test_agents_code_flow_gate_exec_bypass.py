"""Exec classification proven by execution, not by predicate alone.

Ported from the fork's tests/test_gate_exec_bypass.py against the code-flow
plugin's gate. Two halves per bypass family: (1) the raw command REALLY
writes or deletes when run without the gate - in a throwaway directory - so
the case matters; (2) driven the way the tool registry drives it (gate
verdict first, command only on a None verdict), the gate intervenes BEFORE
anything runs and the target files stay untouched. Read-only forms keep
flowing: no allocation record, no pending question, command executes.
"""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow import gate as wg  # noqa: E402

FD = shutil.which("fd")


def make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "input.txt").write_text("b\na\n")
    (path / "victim.txt").write_text("do not delete\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def drive(repo: Path, base: Path, repos: Path, session: str, command: str):
    """The registry contract: the gate rules first; only None lets exec run."""
    gate = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate.cid = lambda: session
    verdict = asyncio.run(gate.adjudicate("exec", {"command": command}, session_workdir=None))
    ran = None
    if verdict is None:
        ran = subprocess.run(command, shell=True, cwd=str(repo), capture_output=True, text=True)
    return verdict, ran


def alloc_record(base: Path, session: str) -> Path:
    return base / "allocations" / f"{wg._safe_segment(wg._identity_for_session(session))}.json"


# --- half 1: the bypass commands really write or delete ----------------------


def test_sort_attached_output_really_writes(tmp_path):
    make_repo(tmp_path / "raw")
    subprocess.run("sort -oout.txt input.txt", shell=True, cwd=str(tmp_path / "raw"), check=True)
    assert (tmp_path / "raw" / "out.txt").read_text() == "a\nb\n"


def test_git_diff_output_really_writes(tmp_path):
    raw = tmp_path / "raw"
    make_repo(raw)
    (raw / "input.txt").write_text("changed\n")
    subprocess.run("git diff --output=diff.txt", shell=True, cwd=str(raw), check=True)
    assert (raw / "diff.txt").stat().st_size > 0


@pytest.mark.skipif(FD is None, reason="fd binary not installed")
@pytest.mark.parametrize("form", ["fd --exec=rm victim.txt .", "fd -xrm victim.txt ."])
def test_fd_exec_really_deletes(tmp_path, form):
    raw = tmp_path / f"raw-{abs(hash(form)) % 1000}"
    make_repo(raw)
    subprocess.run(form, shell=True, cwd=str(raw), check=True)
    assert not (raw / "victim.txt").exists(), form


# --- half 2: the gate intervenes before execution ----------------------------

SORT_MUTATING = [
    "sort -oout.txt input.txt",
    "sort -o out.txt input.txt",
    "sort --output=out.txt input.txt",
    "sort --output out.txt input.txt",
]
FD_MUTATING = [
    "fd -xrm victim.txt .",
    "fd -x rm victim.txt .",
    "fd -Xrm victim.txt .",
    "fd -X rm victim.txt .",
    "fd --exec=rm victim.txt .",
    "fd --exec rm victim.txt .",
    "fd --exec-batch=rm victim.txt .",
    "fd --exec-batch rm victim.txt .",
]
GIT_MUTATING = [
    "git -C {repo} checkout -b test-branch",
    "git -C {repo} commit -m test",
    "git -C {repo} reset --hard HEAD",
    "git -C {repo} diff --output=diff.txt",
    "git -C {repo} diff --output diff.txt",
    "git -C {repo} show -o result.txt HEAD",
]


@pytest.mark.parametrize("command", SORT_MUTATING + FD_MUTATING + GIT_MUTATING)
def test_mutating_forms_are_gated_before_execution(tmp_path, command):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "wip.txt").write_text("dirty\n")  # dirty -> a write must ask first
    command = command.format(repo=str(repo))

    verdict, ran = drive(repo, base, repos, f"acp:mut-{abs(hash(command)) % 99999}", command)
    assert verdict is not None, f"classified read-only: {command}"
    assert ran is None, "the command must not run behind a non-None verdict"
    assert (repo / "victim.txt").exists()
    assert not (repo / "out.txt").exists()
    assert not (repo / "diff.txt").exists()
    assert not (repo / "result.txt").exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "test-branch"], capture_output=True, text=True
    )
    assert not branches.stdout.strip()


GIT_READONLY = [
    "git -C . status",
    "git -C . log --oneline",
    "git -C {repo} diff",
    "git --no-pager log",
    "git -c color.ui=false status",
]


@pytest.mark.parametrize("command", GIT_READONLY)
def test_readonly_git_forms_run_without_allocation(tmp_path, command):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "wip.txt").write_text("dirty\n")  # even dirty: a read never asks
    command = command.format(repo=str(repo))
    session = f"acp:ro-{abs(hash(command)) % 99999}"

    verdict, ran = drive(repo, base, repos, session, command)
    assert verdict is None, f"classified mutating: {command}"
    assert ran is not None and ran.returncode == 0, (command, ran and ran.stderr)
    assert not alloc_record(base, session).exists(), "a read must not allocate"


def test_readonly_git_with_spaced_absolute_path(tmp_path):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo with spaces"
    make_repo(repo)
    session = "acp:ro-spaced"
    command = f'git -C "{repo}" status'

    verdict, ran = drive(repo, base, repos, session, command)
    assert verdict is None
    assert ran is not None and ran.returncode == 0
    assert not alloc_record(base, session).exists()


@pytest.mark.skipif(FD is None, reason="fd binary not installed")
def test_readonly_sort_and_fd_still_flow(tmp_path):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    for session, command in (("acp:ro-sort", "sort input.txt"), ("acp:ro-fd", "fd victim .")):
        verdict, ran = drive(repo, base, repos, session, command)
        assert verdict is None, command
        assert ran is not None and ran.returncode == 0, (command, ran and ran.stderr)
        assert not alloc_record(base, session).exists()
