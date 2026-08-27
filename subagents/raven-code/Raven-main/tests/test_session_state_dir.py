"""Sessions live outside the workspace, bucketed per workspace.

For a coding agent the workspace is the user's repository: writing session
files there makes every run show up as untracked files, which the agent then
spends turns investigating. ``get_workspace_state_dir`` is the established
convention for raven's own state, and this bucket is where sessions go.

What this build must not do is reach into ``<workspace>/sessions`` and clear it
out: a raven that shares the workspace keeps its live transcripts exactly
there. See ``../local-patches.diff``.
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


def test_an_in_workspace_sessions_tree_is_left_untouched(tmp_path):
    """Another raven's live store shares this directory, and outranks us.

    An earlier version read ``<workspace>/sessions`` as this build's own legacy
    layout and moved it into the state directory, removing the source. Pointed
    at a workspace a current raven also uses, that ate the host's live session
    store rather than a legacy one.
    """
    workspace = tmp_path / "repo"
    host = workspace / "sessions" / "cli"
    host.mkdir(parents=True)
    (host / "live.jsonl").write_text('{"role": "user", "content": "hi"}\n', encoding="utf-8")

    mgr = SessionManager(workspace)

    assert (host / "live.jsonl").read_text(encoding="utf-8").startswith('{"role"')
    assert not (mgr.sessions_dir / "cli" / "live.jsonl").exists()


def test_round_trip_still_works_from_the_new_location(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    mgr = SessionManager(workspace)
    session = mgr.get_or_create("cli:abc")
    session.messages.append({"role": "user", "content": "hello"})
    mgr.save(session)

    reloaded = SessionManager(workspace).get_or_create("cli:abc")
    assert [m["content"] for m in reloaded.messages] == ["hello"]
