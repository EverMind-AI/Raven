"""The mutating gate fails CLOSED.

A broken allocation path must never fall through to the workspace: reads keep
working, writes are refused with a recoverable error, nothing is written to
the primary or to a wrong directory, and a later retry re-runs allocation.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from raven.agent import workspace_gate as wg


def make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


@pytest.fixture
def armed(tmp_path, monkeypatch):
    base = tmp_path / "acp"
    repos = tmp_path / "repos"
    monkeypatch.setenv(wg.ALLOC_BASE_ENV, str(base))
    monkeypatch.setenv(wg.REPOS_ENV, str(repos))
    monkeypatch.delenv(wg.ALLOC_DIR_ENV, raising=False)
    return base, repos


def gate_for(repo: Path, base: Path, repos: Path, session: str) -> wg.WorkspaceGate:
    gate = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate.cid = lambda: session
    return gate


def snapshot(repo: Path) -> dict:
    return {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file() and ".git" not in p.parts}


def test_corrupt_allocation_blocks_writes_allows_reads(tmp_path, armed):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    session = "acp:corrupt"
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session(session))}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{ this is not json")
    before = snapshot(repo)

    gate = gate_for(repo, base, repos, session)
    verdict = asyncio.run(gate("write_file", {"path": "b.py"}))
    assert verdict is not None and "workspace allocation failed" in verdict
    assert "NOT executed" in verdict
    assert snapshot(repo) == before, "no file changed behind the block"
    # Reads keep working through the same broken record.
    assert asyncio.run(gate("read_file", {"path": "a.py"})) is None
    assert asyncio.run(gate("exec", {"command": "ls"})) is None
    # Recoverable: fix the record, the SAME write now allocates and passes.
    record.unlink()
    assert asyncio.run(gate("write_file", {"path": "b.py"})) is None


def test_git_failure_blocks_writes_allows_reads(tmp_path, armed, monkeypatch):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)

    def broken_git(workspace, *args):
        raise OSError("git binary vanished")

    monkeypatch.setattr(wg, "_git", broken_git)
    gate = gate_for(repo, base, repos, "acp:gitless")
    verdict = asyncio.run(gate("write_file", {"path": "b.py"}))
    assert verdict is not None and "workspace allocation failed" in verdict
    assert asyncio.run(gate("read_file", {"path": "a.py"})) is None


def test_lock_failure_blocks_writes(tmp_path, armed, monkeypatch):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)

    def broken_flock(fh, op):
        raise OSError("flock refused")

    monkeypatch.setattr(wg.fcntl, "flock", broken_flock)
    gate = gate_for(repo, base, repos, "acp:lockless")
    verdict = asyncio.run(gate("write_file", {"path": "b.py"}))
    assert verdict is not None and "workspace allocation failed" in verdict


def test_worktree_creation_failure_blocks_and_does_not_bind(tmp_path, armed, monkeypatch):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "wip.txt").write_text("dirty\n")  # force the ask path

    async def approve(prompt, choices, default):
        return "worktree"

    def broken_create(repo_facts, repos_root, repo_id, instance):
        raise OSError("disk full")

    monkeypatch.setattr(wg, "create_worktree", broken_create)
    gate = gate_for(repo, base, repos, "acp:wtfail")
    gate.ask = approve
    verdict = asyncio.run(gate("write_file", {"path": "b.py"}))
    assert verdict is not None and "workspace allocation failed" in verdict
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session('acp:wtfail'))}.json"
    assert not record.exists() or "working" not in record.read_text(), "no phantom binding"


def test_verdict_is_not_cached_so_retry_reallocates(tmp_path, armed, monkeypatch):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    calls = {"n": 0}
    real_git = wg._git

    def flaky_git(workspace, *args):
        calls["n"] += 1
        if calls["n"] <= 1:
            raise OSError("transient")
        return real_git(workspace, *args)

    monkeypatch.setattr(wg, "_git", flaky_git)
    gate = gate_for(repo, base, repos, "acp:flaky")
    assert "workspace allocation failed" in asyncio.run(gate("write_file", {"path": "b.py"}))
    # The failure healed; the same write must now succeed without a new gate.
    assert asyncio.run(gate("write_file", {"path": "b.py"})) is None


@pytest.mark.parametrize("payload", ["[]", '"a string"', "7", "null"])
def test_non_object_allocation_roots_fail_closed(tmp_path, armed, payload):
    """Valid JSON whose root is not an object is a corrupt record, not "no
    allocation": writes block, nothing on disk changes, reads keep working."""
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    session = f"acp:root-{payload[:2]}"
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session(session))}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(payload)
    before = snapshot(repo)

    gate = gate_for(repo, base, repos, session)
    verdict = asyncio.run(gate("write_file", {"path": "b.py"}))
    assert verdict is not None and "workspace allocation failed" in verdict
    assert "not an object" in verdict
    assert snapshot(repo) == before
    assert record.read_text() == payload, "the corrupt record itself is untouched"
    assert asyncio.run(gate("read_file", {"path": "a.py"})) is None
