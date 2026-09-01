"""The mutating gate fails CLOSED.

Ported from the fork's tests/test_gate_fail_closed.py against the code-flow
plugin's gate (the fork armed the multiplexed layout through env; the plugin
takes the same roots as constructor facts from its config slice). A broken
allocation path must never fall through to the workspace: reads keep working,
writes are refused with a recoverable error, nothing is written to the primary
or to a wrong directory, and a later retry re-runs allocation.
"""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow import gate as wg  # noqa: E402


def make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


@pytest.fixture
def roots(tmp_path):
    return tmp_path / "acp", tmp_path / "repos"


def gate_for(repo: Path, base: Path, repos: Path, session: str) -> wg.WorkspaceGate:
    gate = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate.cid = lambda: session
    return gate


def call(gate, name, params):
    return asyncio.run(gate.adjudicate(name, params, session_workdir=None))


def snapshot(repo: Path) -> dict:
    return {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file() and ".git" not in p.parts}


def test_corrupt_allocation_blocks_writes_allows_reads(tmp_path, roots):
    base, repos = roots
    repo = tmp_path / "repo"
    make_repo(repo)
    session = "acp:corrupt"
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session(session))}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{ this is not json")
    before = snapshot(repo)

    gate = gate_for(repo, base, repos, session)
    verdict = call(gate, "write_file", {"path": "b.py"})
    assert verdict is not None and "workspace allocation failed" in verdict
    assert "NOT executed" in verdict
    assert snapshot(repo) == before, "no file changed behind the block"
    # Reads keep working through the same broken record.
    assert call(gate, "read_file", {"path": "a.py"}) is None
    assert call(gate, "exec", {"command": "ls"}) is None
    # Recoverable: fix the record, the SAME write now allocates and passes.
    record.unlink()
    assert call(gate, "write_file", {"path": "b.py"}) is None


def test_git_failure_blocks_writes_allows_reads(tmp_path, roots, monkeypatch):
    base, repos = roots
    repo = tmp_path / "repo"
    make_repo(repo)

    def broken_git(workspace, *args):
        raise OSError("git binary vanished")

    monkeypatch.setattr(wg, "_git", broken_git)
    gate = gate_for(repo, base, repos, "acp:gitless")
    verdict = call(gate, "write_file", {"path": "b.py"})
    assert verdict is not None and "workspace allocation failed" in verdict
    assert call(gate, "read_file", {"path": "a.py"}) is None


def test_lock_failure_blocks_writes(tmp_path, roots, monkeypatch):
    base, repos = roots
    repo = tmp_path / "repo"
    make_repo(repo)

    def broken_flock(fh, op):
        raise OSError("flock refused")

    monkeypatch.setattr(wg.fcntl, "flock", broken_flock)
    gate = gate_for(repo, base, repos, "acp:lockless")
    verdict = call(gate, "write_file", {"path": "b.py"})
    assert verdict is not None and "workspace allocation failed" in verdict


def test_worktree_creation_failure_blocks_and_does_not_bind(tmp_path, roots, monkeypatch):
    base, repos = roots
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
    verdict = call(gate, "write_file", {"path": "b.py"})
    assert verdict is not None and "workspace allocation failed" in verdict
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session('acp:wtfail'))}.json"
    assert not record.exists() or "working" not in record.read_text(), "no phantom binding"


def test_verdict_is_not_cached_so_retry_reallocates(tmp_path, roots, monkeypatch):
    base, repos = roots
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
    assert "workspace allocation failed" in call(gate, "write_file", {"path": "b.py"})
    # The failure healed; the same write must now succeed without a new gate.
    assert call(gate, "write_file", {"path": "b.py"}) is None


@pytest.mark.parametrize("payload", ["[]", '"a string"', "7", "null"])
def test_non_object_allocation_roots_fail_closed(tmp_path, roots, payload):
    """Valid JSON whose root is not an object is a corrupt record, not "no
    allocation": writes block, nothing on disk changes, reads keep working."""
    base, repos = roots
    repo = tmp_path / "repo"
    make_repo(repo)
    session = f"acp:root-{payload[:2]}"
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session(session))}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(payload)
    before = snapshot(repo)

    gate = gate_for(repo, base, repos, session)
    verdict = call(gate, "write_file", {"path": "b.py"})
    assert verdict is not None and "workspace allocation failed" in verdict
    assert "not an object" in verdict
    assert snapshot(repo) == before
    assert record.read_text() == payload, "the corrupt record itself is untouched"
    assert call(gate, "read_file", {"path": "a.py"}) is None
