"""Sessions live outside the workspace, bucketed per workspace.

For a coding agent the workspace is the user's repository: writing session
files there makes every run show up as untracked files, which the agent then
spends turns investigating. ``get_workspace_state_dir`` is the established
convention for raven's own state; sessions predate it and are migrated here.
"""

from raven.session.manager import SessionManager


def test_sessions_are_not_written_inside_the_workspace(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    mgr = SessionManager(workspace)
    assert workspace not in mgr.sessions_dir.parents
    assert not (workspace / "sessions").exists()


def test_each_workspace_gets_its_own_bucket(tmp_path):
    a = tmp_path / "repo_a"
    b = tmp_path / "repo_b"
    a.mkdir()
    b.mkdir()
    assert SessionManager(a).sessions_dir != SessionManager(b).sessions_dir


def test_in_workspace_sessions_are_left_alone(tmp_path):
    """A sessions/ directory inside the workspace belongs to the CALLER.

    Run as a sub-agent this raven inherits the host agent's working directory,
    and the host keeps its own transcripts and direct-chat records under
    exactly <workspace>/sessions. The migration that used to sweep that tree
    into our state dir stole the host's in-flight direct-chat record, so the
    host silently dropped the reply. The contract now: never move, delete or
    read it.
    """
    workspace = tmp_path / "repo"
    theirs = workspace / "sessions" / "cli"
    theirs.mkdir(parents=True)
    (theirs / "host.jsonl").write_text(chr(123) + "role: host" + chr(125) + chr(10), encoding="utf-8")

    mgr = SessionManager(workspace)
    # Untouched in place...
    assert (theirs / "host.jsonl").read_text(encoding="utf-8").startswith(chr(123))
    # ...and not copied into our own state either.
    assert not (mgr.sessions_dir / "cli" / "host.jsonl").exists()


def test_round_trip_still_works_from_the_new_location(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    mgr = SessionManager(workspace)
    session = mgr.get_or_create("cli:abc")
    session.messages.append({"role": "user", "content": "hello"})
    mgr.save(session)

    reloaded = SessionManager(workspace).get_or_create("cli:abc")
    assert [m["content"] for m in reloaded.messages] == ["hello"]
