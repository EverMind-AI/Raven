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


def test_existing_in_workspace_sessions_are_migrated_once(tmp_path):
    workspace = tmp_path / "repo"
    legacy = workspace / "sessions" / "cli"
    legacy.mkdir(parents=True)
    (legacy / "old.jsonl").write_text('{"role": "user", "content": "hi"}\n', encoding="utf-8")

    mgr = SessionManager(workspace)
    assert (mgr.sessions_dir / "cli" / "old.jsonl").read_text(encoding="utf-8").startswith('{"role"')
    # The workspace copy is gone, so the repository stops showing an untracked dir.
    assert not (workspace / "sessions").exists()


def test_migration_never_overwrites_newer_state(tmp_path):
    workspace = tmp_path / "repo"
    legacy = workspace / "sessions" / "cli"
    legacy.mkdir(parents=True)
    (legacy / "s.jsonl").write_text("legacy\n", encoding="utf-8")

    mgr = SessionManager(workspace)
    target = mgr.sessions_dir / "cli" / "s.jsonl"
    target.write_text("current\n", encoding="utf-8")

    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "s.jsonl").write_text("legacy again\n", encoding="utf-8")
    SessionManager(workspace)
    assert target.read_text(encoding="utf-8") == "current\n"


def test_round_trip_still_works_from_the_new_location(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    mgr = SessionManager(workspace)
    session = mgr.get_or_create("cli:abc")
    session.messages.append({"role": "user", "content": "hello"})
    mgr.save(session)

    reloaded = SessionManager(workspace).get_or_create("cli:abc")
    assert [m["content"] for m in reloaded.messages] == ["hello"]
