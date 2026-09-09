"""Lifecycle release and the ACP-path integration manifest.

Everything runs in the multiplexed (ACP) layout: RAVEN_WORKSPACE_ALLOC_BASE +
_REPOS armed via monkeypatch, throwaway git repos under tmp_path.
"""

import asyncio
import json
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


def bind_worktree(repo: Path, repos: Path, session: str) -> dict:
    """Allocate a worktree for `session` the way the gate does."""
    gate = wg.WorkspaceGate(repo, repos, alloc_base=repos.parent / "acp")
    gate.cid = lambda: session

    async def approve(prompt, choices, default):
        return "worktree"

    gate.ask = approve
    # Dirty the repo so the first write asks, and the approval binds a worktree.
    (repo / "wip.txt").write_text("uncommitted\n")
    verdict = asyncio.run(gate("write_file", {"path": "b.py"}))
    assert verdict and "worktree" in verdict.lower()
    alloc = json.loads(
        (
            repos.parent / "acp" / "allocations" / f"{wg._safe_segment(wg._identity_for_session(session))}.json"
        ).read_text()
    )
    assert alloc["mode"] == "worktree"
    return alloc


def test_release_primary_frees_the_owner(tmp_path, armed):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    gate = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate.cid = lambda: "acp:s1"
    assert asyncio.run(gate("write_file", {"path": "b.py"})) is None  # clean -> binds primary

    repo_id, _ = wg.repo_identity(repo)
    assert json.loads((repos / repo_id / "primary.json").read_text())["instance"] == "acp-acp_s1"

    summary = wg.release_for_session("acp:s1")
    assert summary and summary.get("primaryReleased") and summary.get("allocationRemoved")
    assert not (repos / repo_id / "primary.json").exists()
    # The next session binds the primary instead of inheriting a ghost owner.
    gate2 = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate2.cid = lambda: "acp:s2"
    assert asyncio.run(gate2("write_file", {"path": "c.py"})) is None


def test_release_keeps_a_worktree_with_content(tmp_path, armed):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    alloc = bind_worktree(repo, repos, "acp:s3")
    wt = Path(alloc["boundWorkdir"])
    (wt / "work.py").write_text("real work\n")

    summary = wg.release_for_session("acp:s3")
    assert summary and summary.get("worktreeKept")
    assert wt.is_dir() and (wt / "work.py").exists()
    kept = json.loads((base / "allocations" / f"{wg._safe_segment('acp-acp_s3')}.json").read_text())
    assert kept["state"] == "released_kept_worktree"


def test_release_removes_an_empty_handed_worktree(tmp_path, armed):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    alloc = bind_worktree(repo, repos, "acp:s4")
    wt = Path(alloc["boundWorkdir"])

    summary = wg.release_for_session("acp:s4")
    assert summary and summary.get("worktreeRemoved")
    assert not wt.exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", alloc["branch"]], capture_output=True, text=True
    )
    assert not branches.stdout.strip(), "the empty worktree's branch is gone too"


def test_release_keeps_a_worktree_with_ignored_content(tmp_path, armed):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / ".gitignore").write_text("ignored-result.txt\n")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "ignore generated result"], check=True)
    alloc = bind_worktree(repo, repos, "acp:ignored")
    wt = Path(alloc["boundWorkdir"])
    (wt / "ignored-result.txt").write_text("keep me\n")

    summary = wg.release_for_session("acp:ignored")

    assert summary and summary.get("worktreeKept")
    assert wt.exists()
    assert (wt / "ignored-result.txt").read_text() == "keep me\n"


def test_release_is_a_noop_when_unarmed(tmp_path, monkeypatch):
    monkeypatch.delenv(wg.ALLOC_BASE_ENV, raising=False)
    monkeypatch.delenv(wg.REPOS_ENV, raising=False)
    assert wg.release_for_session("acp:whatever") is None


def parse_manifest(text: str) -> dict:
    """Cut the LAST v1 JSON block out of a reply, the way a consumer would."""
    assert wg.MANIFEST_SENTINEL_OPEN in text and wg.MANIFEST_SENTINEL_CLOSE in text
    payload = text.rsplit(wg.MANIFEST_SENTINEL_OPEN, 1)[1].rsplit(wg.MANIFEST_SENTINEL_CLOSE, 1)[0]
    return json.loads(payload.strip())


def test_manifest_reports_the_worktree_facts(tmp_path, armed):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    alloc = bind_worktree(repo, repos, "acp:s5")
    wt = Path(alloc["boundWorkdir"])
    (wt / "feature.py").write_text("def f():\n    return 1\n")

    text = wg.manifest_for_session("acp:s5")
    assert text is not None
    data = parse_manifest(text)
    assert data["manifestVersion"] == 1
    assert data["workspaceKind"] == "worktree"
    assert data["sessionId"] == "acp:s5"
    assert data["branch"] == alloc["branch"]
    assert data["baseCommit"] == alloc["repo"]["baseCommit"] and len(data["baseCommit"]) == 40
    assert len(data["head"]) == 40
    assert data["workdir"] == str(wt)
    assert data["repositoryRoot"] == alloc["repo"]["root"]
    assert data["workingTree"]["uncommittedCount"] == 1
    assert data["workingTree"]["clean"] is False
    assert any("feature.py" in entry for entry in data["workingTree"]["entries"])
    assert data["readyForIntegration"] is False
    assert data["status"] == "needs_commit"
    assert data["blockers"] == ["uncommitted changes must be committed"]
    assert data["tests"]["verifiedByRuntime"] is False


def test_manifest_is_ready_only_after_the_work_is_committed(tmp_path, armed):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    alloc = bind_worktree(repo, repos, "acp:s7")
    wt = Path(alloc["boundWorkdir"])
    (wt / "feature.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "-C", str(wt), "add", "feature.py"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "add feature"], check=True)

    data = parse_manifest(wg.manifest_for_session("acp:s7"))
    assert data["commitsPastBase"]["count"] == 1
    assert data["commitsPastBase"]["items"][0]["subject"] == "add feature"
    assert len(data["commitsPastBase"]["items"][0]["sha"]) == 40
    assert data["workingTree"] == {"clean": True, "uncommittedCount": 0, "entries": []}
    assert data["readyForIntegration"] is True
    assert data["status"] == "ready_for_integration"
    assert data["blockers"] == []


def test_manifest_covers_primary_and_skips_the_unallocated(tmp_path, armed):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    gate = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate.cid = lambda: "acp:s6"
    assert asyncio.run(gate("write_file", {"path": "b.py"})) is None
    data = parse_manifest(wg.manifest_for_session("acp:s6"))
    assert data["workspaceKind"] == "primary"
    assert data["workdir"] == data["repositoryRoot"] == str(repo.resolve())
    assert data["status"] == "no_changes"  # bound, nothing committed past base yet
    assert wg.manifest_for_session("acp:never-existed") is None


def test_worktree_is_dirty_when_a_tracked_dot_raven_file_changed(tmp_path, armed):
    """A project that TRACKS .raven/project.yml: editing it is user work."""
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / ".raven").mkdir()
    (repo / ".raven" / "project.yml").write_text("version: 1\n")
    subprocess.run(["git", "-C", str(repo), "add", ".raven/project.yml"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "track project config"], check=True)
    alloc = bind_worktree(repo, repos, "acp:tracked-raven")
    wt = Path(alloc["boundWorkdir"])
    (wt / ".raven" / "project.yml").write_text("version: 2\n")

    assert wg._worktree_is_clean(wt, alloc["repo"]["baseCommit"]) is False
    summary = wg.release_for_session("acp:tracked-raven")
    assert summary and summary.get("worktreeKept")
    assert (wt / ".raven" / "project.yml").read_text() == "version: 2\n"


def test_release_keeps_untracked_and_ignored_user_dot_raven_files(tmp_path, armed):
    base, repos = armed
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / ".gitignore").write_text(".raven/cache.bin\n")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "ignore cache"], check=True)
    alloc = bind_worktree(repo, repos, "acp:user-raven")
    wt = Path(alloc["boundWorkdir"])
    (wt / ".raven").mkdir()
    (wt / ".raven" / "user.txt").write_text("untracked user file\n")
    (wt / ".raven" / "cache.bin").write_text("ignored user file\n")

    summary = wg.release_for_session("acp:user-raven")
    assert summary and summary.get("worktreeKept")
    assert (wt / ".raven" / "user.txt").exists()
    assert (wt / ".raven" / "cache.bin").exists()
