"""A bridged MCP tool is opaque to this agent, so the gate has to read it as a write.

The other bypass families in ``test_gate_exec_bypass.py`` are commands this agent
can parse. An MCP tool is not: the name and the schema come from somebody else's
server, reached through a socket, so a filesystem or database tool arrives here
looking exactly like a lookup. Classifying by name is the only thing available,
and the gate's own asymmetry decides which way to fail -- reading a write as a
read is what defeats it.

Driven the way the tool registry drives it: the gate rules first, and only a
``None`` verdict lets the call run.
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
    (path / "victim.txt").write_text("do not delete\n")
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


def alloc_record(base: Path, session: str) -> Path:
    return base / "allocations" / f"{wg._safe_segment(wg._identity_for_session(session))}.json"


@pytest.mark.parametrize(
    "tool",
    [
        # A filesystem server. Nothing in the name says so to this agent.
        "mcp_files_delete_file",
        # A database server, whose write is not even on this machine.
        "mcp_pg_execute",
        # And the one that reads: it is classified the same way, because this
        # agent cannot tell it apart from the two above.
        "mcp_deepwiki_read_wiki_contents",
    ],
)
def test_a_bridged_tool_is_gated_like_a_write(tmp_path, armed, tool):
    base, repos = armed
    repo = tmp_path / "primary"
    make_repo(repo)
    gate = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate.cid = lambda: "acp:s1"

    verdict = asyncio.run(gate(tool, {"path": str(repo / "victim.txt")}))

    # Either it is refused, or it allocated first -- what must not happen is the
    # call flowing through with no record, which is the state a second session
    # would read as "nobody owns the primary checkout".
    assert verdict is not None or alloc_record(base, "acp:s1").exists(), (
        f"{tool} reached execution without an allocation; a filesystem tool behind that "
        f"name could then write the primary checkout while another session owns it"
    )


def test_a_read_only_builtin_still_flows(tmp_path, armed):
    """The gate did not become a blanket. Only the opaque family moved."""
    base, repos = armed
    repo = tmp_path / "primary"
    make_repo(repo)
    gate = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate.cid = lambda: "acp:s2"

    assert asyncio.run(gate("read_file", {"path": str(repo / "victim.txt")})) is None
    assert not alloc_record(base, "acp:s2").exists()


def test_the_classification_is_the_prefix_not_a_list(tmp_path):
    """The names past ``mcp_`` come from somebody else's server, so there is no
    list of them to keep -- which is exactly why the whole family is classified
    together rather than enumerated."""
    gate = object.__new__(wg.WorkspaceGate)
    assert wg.WorkspaceGate._mutates(gate, "mcp_anything_at_all", {}) is True
    assert wg.WorkspaceGate._mutates(gate, "mcp_", {}) is True
    # Not a bridged tool, and not renamed into one by accident.
    assert wg.WorkspaceGate._mutates(gate, "read_file", {}) is False
    assert wg.WorkspaceGate._mutates(gate, "not_mcp_shaped", {}) is False


# --- worktree mode: fail closed ---------------------------------------------


def _worktree_alloc(primary: Path, worktree: Path) -> dict:
    return {
        "mode": "worktree",
        "boundWorkdir": str(worktree),
        "repo": {"root": str(primary)},
    }


@pytest.mark.parametrize(
    "tool",
    [
        # Resolves a relative path against wherever the host started it.
        "mcp_files_write_file",
        # Holds an absolute root, which may be the primary checkout itself. This
        # one needs no cwd at all, so knowing where the process started would not
        # settle it.
        "mcp_fsroot_put",
        # Touches no filesystem -- and is refused with the rest, because this
        # agent is handed one stdio stanza naming a socket and cannot tell it
        # apart from the two above.
        "mcp_deepwiki_read_wiki_contents",
    ],
)
def test_a_bridged_tool_is_refused_once_the_session_is_in_a_worktree(tmp_path, tool):
    """The verdict this session was given says "inside the worktree only", and a
    tool it can still call must not be able to contradict it.

    The path checks below this branch are only possible for a tool whose
    arguments name what it will touch. An MCP call's arguments belong to somebody
    else's schema, and the work happens in a process the host started before this
    session moved -- it never heard about the rebind and has no channel to be
    told. So there is nothing to inspect, and nothing to inspect means refuse.
    """
    gate = object.__new__(wg.WorkspaceGate)
    alloc = _worktree_alloc(tmp_path / "primary", tmp_path / "wt")

    blocked = wg.WorkspaceGate._escape_blocked(gate, tool, {"path": "notes.md"}, alloc)

    assert blocked is not None, f"{tool} could act outside the worktree this session was bound to"
    assert "worktree" in blocked
    assert str(tmp_path / "wt") in blocked


def test_the_refusal_is_worktree_mode_only(tmp_path):
    """A session that bound the primary checkout is not in this situation: its
    own verdict is that the primary is what it may write, so an MCP call agreeing
    with that contradicts nothing."""
    gate = object.__new__(wg.WorkspaceGate)
    primary_alloc = {"mode": "primary", "boundWorkdir": str(tmp_path), "repo": {"root": str(tmp_path)}}

    assert wg.WorkspaceGate._escape_blocked(gate, "mcp_files_write_file", {}, primary_alloc) is None


def test_the_built_in_tools_keep_their_path_check_in_a_worktree(tmp_path):
    """The MCP branch returns before the path checks, so it must not have taken
    them out from under the tools that can still be checked."""
    gate = object.__new__(wg.WorkspaceGate)
    primary, wt = tmp_path / "primary", tmp_path / "wt"
    primary.mkdir()
    wt.mkdir()
    alloc = _worktree_alloc(primary, wt)

    inside = wg.WorkspaceGate._escape_blocked(gate, "write_file", {"path": str(wt / "ok.md")}, alloc)
    outside = wg.WorkspaceGate._escape_blocked(gate, "write_file", {"path": str(primary / "no.md")}, alloc)

    assert inside is None
    assert outside is not None
