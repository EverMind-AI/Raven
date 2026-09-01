"""Integration manifest v1: machine-read JSON between sentinels, both modes.

Ported from the fork's tests/test_manifest_v1.py against the code-flow
plugin's gate (the manifest functions take the allocation base explicitly
where the fork read its arming environment). Covers the state matrix
(clean+committed / uncommitted / no change / git failure), JSON robustness
against quotes and Unicode in subjects and paths, and the negative
guarantees: no state-directory content, no credentials.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow import gate as wg  # noqa: E402

# Quotes, a backslash, an accented word and an emoji: enough to break naive
# JSON framing. Spelled in escapes so the test file itself stays ASCII.
UNICODE_SUBJECT = 'said "hello" \\ caf\u00e9 \U0001f389'


def make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def parse(text: str) -> dict:
    payload = text.rsplit(wg.MANIFEST_SENTINEL_OPEN, 1)[1].rsplit(wg.MANIFEST_SENTINEL_CLOSE, 1)[0]
    return json.loads(payload.strip())


def bind_primary(repo: Path, base: Path, repos: Path, session: str) -> None:
    gate = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate.cid = lambda: session
    assert asyncio.run(gate.adjudicate("write_file", {"path": "b.py"}, session_workdir=None)) is None


def bind_worktree(repo: Path, base: Path, repos: Path, session: str) -> dict:
    gate = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate.cid = lambda: session

    async def approve(prompt, choices, default):
        return "worktree"

    gate.ask = approve
    (repo / "wip.txt").write_text("uncommitted host WIP\n")
    verdict = asyncio.run(gate.adjudicate("write_file", {"path": "b.py"}, session_workdir=None))
    assert verdict and "worktree" in verdict.lower()
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session(session))}.json"
    return json.loads(record.read_text())


def commit(cwd: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(cwd), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(cwd), "commit", "-qm", message], check=True)


# -- primary ---------------------------------------------------------------


def test_primary_clean_with_one_commit_is_ready(tmp_path):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    bind_primary(repo, base, repos, "acp:p1")
    baseline = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "b.py").write_text("y = 2\n")
    commit(repo, "primary work")

    data = parse(wg.manifest_for_session("acp:p1", alloc_base=base))
    assert data["workspaceKind"] == "primary"
    assert data["repositoryRoot"] == data["workdir"]
    assert data["baseCommit"] == baseline
    assert data["head"] != baseline and len(data["head"]) == 40
    assert data["commitsPastBase"]["count"] == 1
    assert data["commitsPastBase"]["items"][0]["subject"] == "primary work"
    assert data["workingTree"]["clean"] is True
    assert data["readyForIntegration"] is True
    assert data["status"] == "ready_for_integration"
    assert data["blockers"] == []
    assert "changed" in data["diffStat"]


def test_primary_uncommitted_needs_commit(tmp_path):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    bind_primary(repo, base, repos, "acp:p2")
    (repo / "b.py").write_text("y = 2\n")

    data = parse(wg.manifest_for_session("acp:p2", alloc_base=base))
    assert data["workspaceKind"] == "primary"
    assert data["workingTree"]["uncommittedCount"] == 1
    assert data["workingTree"]["entries"] and "b.py" in data["workingTree"]["entries"][0]
    assert data["readyForIntegration"] is False
    assert data["status"] == "needs_commit"


def test_primary_no_change(tmp_path):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    bind_primary(repo, base, repos, "acp:p3")

    data = parse(wg.manifest_for_session("acp:p3", alloc_base=base))
    assert data["status"] == "no_changes"
    assert data["readyForIntegration"] is False
    assert data["blockers"] == ["no commits past base"]
    assert data["diffStat"] == "no diff vs base"


# -- worktree ----------------------------------------------------------------


def test_worktree_committed_is_ready_and_base_is_the_fork_point(tmp_path):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    alloc = bind_worktree(repo, base, repos, "acp:w1")
    wt = Path(alloc["boundWorkdir"])
    (wt / "feature.py").write_text("def f():\n    return 1\n")
    commit(wt, "worktree feature")

    data = parse(wg.manifest_for_session("acp:w1", alloc_base=base))
    assert data["workspaceKind"] == "worktree"
    assert data["workdir"] == str(wt) != data["repositoryRoot"]
    assert data["repositoryRoot"] == alloc["repo"]["root"]
    assert data["baseCommit"] == alloc["repo"]["baseCommit"]
    assert data["branch"] == alloc["branch"]
    assert data["readyForIntegration"] is True
    # The host's uncommitted WIP (wip.txt) must not appear in the worktree view.
    assert not any("wip.txt" in e for e in data["workingTree"]["entries"])


def test_worktree_git_failure_degrades_to_unknown(tmp_path):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    alloc = bind_worktree(repo, base, repos, "acp:w2")
    wt = Path(alloc["boundWorkdir"])
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], check=True)

    data = parse(wg.manifest_for_session("acp:w2", alloc_base=base))
    assert data["status"] == "unknown"
    assert data["readyForIntegration"] is False
    assert data["blockers"] and "git state could not be read" in data["blockers"][0]


# -- robustness ---------------------------------------------------------------


def test_quotes_and_unicode_survive_json(tmp_path):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo dir with spaces"
    make_repo(repo)
    bind_primary(repo, base, repos, "acp:u1")
    (repo / "b.py").write_text("y = 2\n")
    commit(repo, UNICODE_SUBJECT)

    text = wg.manifest_for_session("acp:u1", alloc_base=base)
    assert text.count("\n") == 3  # blank, open sentinel, payload, close sentinel
    data = parse(text)
    assert data["commitsPastBase"]["items"][0]["subject"] == UNICODE_SUBJECT
    assert "repo dir with spaces" in data["workdir"]


def test_manifest_carries_no_state_dir_or_credentials(tmp_path):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    # A state partition the way the launcher lays it out: rendered config with
    # a key, next to the allocations. None of it may reach the manifest.
    (base / "allocations").mkdir(parents=True, exist_ok=True)
    (base / ".config.rendered.json").write_text('{"api_key": "sk-or-v1-SECRETSECRET"}')
    bind_primary(repo, base, repos, "acp:sec1")
    (repo / "b.py").write_text("y = 2\n")
    commit(repo, "work")

    text = wg.manifest_for_session("acp:sec1", alloc_base=base)
    assert "sk-or-v1" not in text
    assert ".config.rendered" not in text
    data = parse(text)
    assert str(base) not in json.dumps(data["workingTree"]) + data["diffStat"]


def test_render_and_data_share_one_object(tmp_path):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    bind_primary(repo, base, repos, "acp:one")
    data = wg.manifest_data_for_session("acp:one", alloc_base=base)
    assert parse(wg.render_manifest(data)) == data


def test_user_dot_raven_content_is_user_content(tmp_path):
    """.raven/** belonging to the USER is dirt like any other: it blocks
    readiness and shows up in entries - the runtime keeps its own state out
    of the checkout instead of filtering the name."""
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    bind_primary(repo, base, repos, "acp:uraven")
    (repo / ".raven").mkdir()
    (repo / ".raven" / "notes.txt").write_text("the user's own notes\n")

    data = parse(wg.manifest_for_session("acp:uraven", alloc_base=base))
    assert data["workingTree"]["uncommittedCount"] == 1
    assert any(".raven" in e for e in data["workingTree"]["entries"])
    assert data["status"] == "needs_commit"
    assert data["readyForIntegration"] is False


def _failing(real, marker):
    import subprocess as sp

    def flaky(workspace, *args):
        if marker(args):
            return sp.CompletedProcess(args, 128, "", "synthetic failure")
        return real(workspace, *args)

    return flaky


def test_branch_query_failure_degrades_to_unknown(tmp_path, monkeypatch):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    bind_primary(repo, base, repos, "acp:nobranch")
    (repo / "b.py").write_text("y = 2\n")
    commit(repo, "work")
    monkeypatch.setattr(wg, "_git", _failing(wg._git, lambda a: "--abbrev-ref" in a))

    data = parse(wg.manifest_for_session("acp:nobranch", alloc_base=base))
    assert data["status"] == "unknown"
    assert data["readyForIntegration"] is False
    assert any("branch" in b for b in data["blockers"])


def test_diff_stat_query_failure_degrades_to_unknown(tmp_path, monkeypatch):
    base, repos = tmp_path / "acp", tmp_path / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    bind_primary(repo, base, repos, "acp:nostat")
    (repo / "b.py").write_text("y = 2\n")
    commit(repo, "work")
    monkeypatch.setattr(wg, "_git", _failing(wg._git, lambda a: a and a[0] == "diff"))

    data = parse(wg.manifest_for_session("acp:nostat", alloc_base=base))
    assert data["status"] == "unknown"
    assert data["readyForIntegration"] is False
    assert any("diff stat" in b for b in data["blockers"])
