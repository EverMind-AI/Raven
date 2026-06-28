"""Tests for SessionManager."""

import json
import multiprocessing
import re
from datetime import datetime
from pathlib import Path

from raven.session.manager import Session, SessionManager, new_chat_id


def _turn_worker(workspace_str: str, key: str, writer_id: int) -> None:
    mgr = SessionManager(Path(workspace_str))
    session = mgr.get_or_create(key)
    session.add_message("user", f"q-{writer_id}")
    session.add_message("assistant", f"a-{writer_id}")
    mgr.save(session)


def test_new_chat_id_shape():
    """A minted chat_id matches the opaque sortable form YYYYMMDD_HHMMSS_xxxxxx."""
    cid = new_chat_id()
    assert re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{6}", cid), cid


def test_new_chat_id_sortable_by_time():
    """Lexicographic order of chat_ids matches chronological mint order."""
    early = new_chat_id(now=datetime(2026, 6, 10, 14, 30, 52))
    late = new_chat_id(now=datetime(2026, 6, 10, 14, 30, 53))
    assert early < late


def test_new_chat_id_unique_same_second():
    """Two chat_ids minted in the same second still differ (uuid suffix)."""
    now = datetime(2026, 6, 10, 14, 30, 52)
    assert new_chat_id(now=now) != new_chat_id(now=now)


def test_save_writes_nested_channel_path(tmp_path: Path):
    """A saved session lands at sessions/{channel}/{chat_id}.jsonl."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:20260610_143052_a1b2c3")
    session.add_message("user", "hi")
    mgr.save(session)
    assert (tmp_path / "sessions" / "tui" / "20260610_143052_a1b2c3.jsonl").exists()


def test_key_from_path_reverses_nested_encoding(tmp_path: Path):
    """key_from_path maps sessions/{channel}/{chat_id}.jsonl back to
    channel:chat_id; a chat_id containing an underscore is preserved verbatim."""
    path = tmp_path / "sessions" / "telegram" / "user_42.jsonl"
    assert SessionManager.key_from_path(path) == "telegram:user_42"


def test_deterministic_chat_id_maps_uniformly(tmp_path: Path):
    """Deterministic chat_ids (cron:x) use the same nested rule as minted ones."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("cron:morning_brief")
    session.add_message("user", "ping")
    mgr.save(session)
    assert (tmp_path / "sessions" / "cron" / "morning_brief.jsonl").exists()


def test_roundtrip_load_from_nested_path(tmp_path: Path):
    """A fresh manager loads a saved session back from the nested path."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:abc123")
    session.add_message("user", "hello")
    session.add_message("assistant", "world")
    mgr.save(session)

    loaded = SessionManager(tmp_path).get_or_create("tui:abc123")
    assert [m["content"] for m in loaded.messages] == ["hello", "world"]


def test_record_stamps_timestamp_only(tmp_path: Path):
    """record() stamps a per-message timestamp and carries neither the
    dropped per-message received_at nor turn_id."""
    session = Session(key="tui:t1")
    session.add_message("user", "q1")
    session.add_message("assistant", "a1")
    session.add_message("tool", "r1")

    for m in session.messages:
        assert m["timestamp"]
        assert "received_at" not in m
        assert "turn_id" not in m


def test_save_reserves_metadata_keys(tmp_path: Path):
    """Metadata reserves source/channel/chat_id/title/parent_session_id."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:meta01")
    session.add_message("user", "x")
    mgr.save(session)

    first_line = (tmp_path / "sessions" / "tui" / "meta01.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()[0]
    meta = json.loads(first_line)["metadata"]
    assert meta["channel"] == "tui"
    assert meta["chat_id"] == "meta01"
    assert meta["parent_session_id"] is None
    assert "source" in meta
    assert "title" in meta


def test_load_preserves_on_disk_message_order(tmp_path: Path):
    """Messages keep file order on load even when received_at is out of order."""
    session_dir = tmp_path / "sessions" / "tui"
    session_dir.mkdir(parents=True)
    lines = [
        {"_type": "metadata", "key": "tui:order01", "metadata": {}},
        {"role": "user", "content": "late", "received_at": "2026-06-10T10:00:05"},
        {"role": "user", "content": "early", "received_at": "2026-06-10T10:00:01"},
    ]
    (session_dir / "order01.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )

    loaded = SessionManager(tmp_path).get_or_create("tui:order01")
    assert [m["content"] for m in loaded.messages] == ["late", "early"]


def test_created_session_is_lazy_until_first_save(tmp_path: Path):
    """get_or_create materializes no file; the session is absent from list."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:lazy01")
    assert not (tmp_path / "sessions" / "tui" / "lazy01.jsonl").exists()
    assert mgr.list_sessions() == []

    session.add_message("user", "first")
    mgr.save(session)
    assert (tmp_path / "sessions" / "tui" / "lazy01.jsonl").exists()
    assert [info["key"] for info in mgr.list_sessions()] == ["tui:lazy01"]


def test_list_sessions_sees_nested_layout(tmp_path: Path):
    """list_sessions enumerates nested per-channel files."""
    mgr = SessionManager(tmp_path)
    for key in ("tui:s1", "cli:s2"):
        session = mgr.get_or_create(key)
        session.add_message("user", "x")
        mgr.save(session)

    keys = {info["key"] for info in mgr.list_sessions()}
    assert keys == {"tui:s1", "cli:s2"}


def _seed_nested(tmp_path: Path, channel: str, chat_id: str, updated_at: str) -> Path:
    channel_dir = tmp_path / "sessions" / channel
    channel_dir.mkdir(parents=True, exist_ok=True)
    path = channel_dir / f"{chat_id}.jsonl"
    meta = {
        "_type": "metadata",
        "key": f"{channel}:{chat_id}",
        "updated_at": updated_at,
        "metadata": {},
    }
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return path


def test_find_most_recent_chat_id_nested_by_updated_at(tmp_path: Path):
    """Returns the chat_id with the newest updated_at on the channel."""
    _seed_nested(tmp_path, "tui", "older", "2026-06-10T10:00:00")
    _seed_nested(tmp_path, "tui", "newer", "2026-06-10T11:00:00")
    _seed_nested(tmp_path, "cli", "distractor", "2026-06-10T12:00:00")

    mgr = SessionManager(tmp_path)
    assert mgr.find_most_recent_chat_id("tui") == "newer"
    assert mgr.find_most_recent_chat_id("cli") == "distractor"
    assert mgr.find_most_recent_chat_id("feishu") is None


def test_find_most_recent_ignores_old_flat_files(tmp_path: Path):
    """Pre-refactor flat files are ignored for lookup but never deleted."""
    _seed_nested(tmp_path, "tui", "nested01", "2026-06-10T10:00:00")
    flat = tmp_path / "sessions" / "tui_flat01.jsonl"
    flat.write_text(
        json.dumps({
            "_type": "metadata",
            "key": "tui:flat01",
            "updated_at": "2026-06-10T23:59:59",
        }) + "\n",
        encoding="utf-8",
    )

    mgr = SessionManager(tmp_path)
    assert mgr.find_most_recent_chat_id("tui") == "nested01"
    assert flat.exists()
    assert "tui:flat01" not in {info["key"] for info in mgr.list_sessions()}


def test_save_appends_instead_of_rewriting(tmp_path: Path):
    """A later save appends the new turn; earlier bytes stay untouched."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:app01")
    session.add_message("user", "q1")
    mgr.save(session)
    path = tmp_path / "sessions" / "tui" / "app01.jsonl"
    first_save = path.read_text(encoding="utf-8")

    session.add_message("assistant", "a1")
    mgr.save(session)
    assert path.read_text(encoding="utf-8").startswith(first_save)

    loaded = SessionManager(tmp_path).get_or_create("tui:app01")
    assert [m["content"] for m in loaded.messages] == ["q1", "a1"]


def test_no_lock_sidecar_beside_session_jsonl(tmp_path: Path):
    """After a save, the channel dir holds the transcript only; the flock
    sidecar lives in a hidden .lock/ subdir and never clutters the listing."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:lock01")
    session.add_message("user", "x")
    mgr.save(session)

    channel_dir = tmp_path / "sessions" / "tui"
    beside = [p.name for p in channel_dir.iterdir() if p.is_file() and p.name.endswith(".lock")]
    assert beside == []
    assert (channel_dir / ".lock" / "lock01.jsonl.lock").exists()
    assert [info["key"] for info in mgr.list_sessions()] == ["tui:lock01"]


def test_clear_rewrites_file(tmp_path: Path):
    """clear() + save truncates the transcript on disk (atomic replace)."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:clr01")
    session.add_message("user", "q1")
    mgr.save(session)

    session.clear()
    mgr.save(session)
    loaded = SessionManager(tmp_path).get_or_create("tui:clr01")
    assert loaded.messages == []

    session.add_message("user", "q2")
    mgr.save(session)
    loaded = SessionManager(tmp_path).get_or_create("tui:clr01")
    assert [m["content"] for m in loaded.messages] == ["q2"]


def test_concurrent_writers_lose_no_turns(tmp_path: Path):
    """Two processes saving the same session: both turn blocks land,
    each block's messages contiguous (tool_call/result adjacency)."""
    key = "tui:race01"
    procs = [
        multiprocessing.Process(target=_turn_worker, args=(str(tmp_path), key, w))
        for w in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    loaded = SessionManager(tmp_path).get_or_create(key)
    contents = [m["content"] for m in loaded.messages]
    assert sorted(contents) == ["a-0", "a-1", "q-0", "q-1"]
    for writer_id in (0, 1):
        q_idx = contents.index(f"q-{writer_id}")
        assert contents[q_idx + 1] == f"a-{writer_id}"


def test_find_most_recent_reflects_latest_append(tmp_path: Path):
    """Recency follows the LAST metadata record, not the first line."""
    mgr = SessionManager(tmp_path)
    first = mgr.get_or_create("tui:first")
    first.add_message("user", "x")
    mgr.save(first)
    second = mgr.get_or_create("tui:second")
    second.add_message("user", "y")
    mgr.save(second)

    first.add_message("user", "z")
    mgr.save(first)
    assert mgr.find_most_recent_chat_id("tui") == "first"


def test_loader_skips_partial_trailing_line(tmp_path: Path):
    """A crash mid-append leaves a partial trailing line; loader skips it."""
    session_dir = tmp_path / "sessions" / "tui"
    session_dir.mkdir(parents=True)
    full = json.dumps({"role": "user", "content": "full"})
    (session_dir / "crash01.jsonl").write_text(
        json.dumps({"_type": "metadata", "key": "tui:crash01", "metadata": {}})
        + "\n" + full + "\n" + '{"role": "assistant", "content": "tru',
        encoding="utf-8",
    )

    loaded = SessionManager(tmp_path).get_or_create("tui:crash01")
    assert [m["content"] for m in loaded.messages] == ["full"]


def test_legacy_global_sessions_shim_removed(tmp_path: Path, monkeypatch):
    """~/.raven/sessions files are no longer migrated nor consulted."""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    legacy_file = legacy / "tui_x.jsonl"
    legacy_file.write_text(
        json.dumps({"_type": "metadata", "key": "tui:x"}) + "\n"
        + json.dumps({"role": "user", "content": "old"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "raven.session.manager.get_legacy_sessions_dir",
        lambda: legacy,
        raising=False,
    )

    session = SessionManager(tmp_path / "ws").get_or_create("tui:x")
    assert session.messages == []
    assert legacy_file.exists()


# ---------------------------------------------------------------------------
# New public API: delete / peek / flush
# ---------------------------------------------------------------------------


def test_delete_removes_file_and_returns_true(tmp_path: Path):
    """delete() removes the JSONL file and returns True."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:del01")
    session.add_message("user", "hi")
    mgr.save(session)
    path = tmp_path / "sessions" / "tui" / "del01.jsonl"
    assert path.exists()

    result = mgr.delete("tui:del01")
    assert result is True
    assert not path.exists()


def test_delete_invalidates_cache(tmp_path: Path):
    """delete() removes the key from the in-memory cache."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:del02")
    session.add_message("user", "x")
    mgr.save(session)
    assert "tui:del02" in mgr._cache

    mgr.delete("tui:del02")
    assert "tui:del02" not in mgr._cache


def test_delete_unknown_key_returns_false(tmp_path: Path):
    """delete() on a key with no file returns False without error."""
    mgr = SessionManager(tmp_path)
    result = mgr.delete("tui:nonexistent_del")
    assert result is False


def test_delete_returns_false_when_unlink_fails(tmp_path: Path, monkeypatch):
    """delete() returns False when removal raises — True only if a file was removed."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:del04")
    session.add_message("user", "x")
    mgr.save(session)

    def _boom_unlink(self, missing_ok=False):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", _boom_unlink)
    assert mgr.delete("tui:del04") is False


def test_delete_does_not_touch_other_sessions(tmp_path: Path):
    """delete() only removes the targeted session file."""
    mgr = SessionManager(tmp_path)
    for key in ("tui:keep01", "tui:del03"):
        s = mgr.get_or_create(key)
        s.add_message("user", "y")
        mgr.save(s)

    mgr.delete("tui:del03")
    assert (tmp_path / "sessions" / "tui" / "keep01.jsonl").exists()
    assert not (tmp_path / "sessions" / "tui" / "del03.jsonl").exists()


def test_peek_returns_cached_session_without_extra_load(tmp_path: Path):
    """peek() returns the cached Session when already in memory."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:peek01")
    session.add_message("user", "peek test")
    mgr.save(session)

    peeked = mgr.peek("tui:peek01")
    assert peeked is session


def test_peek_loads_from_disk_without_caching(tmp_path: Path):
    """peek() loads from disk for unknown keys but does not add to cache."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:peek02")
    session.add_message("user", "disk message")
    mgr.save(session)

    fresh_mgr = SessionManager(tmp_path)
    peeked = fresh_mgr.peek("tui:peek02")
    assert peeked is not None
    assert peeked.messages[0]["content"] == "disk message"
    assert "tui:peek02" not in fresh_mgr._cache


def test_peek_returns_none_for_unknown_key(tmp_path: Path):
    """peek() returns None for a key that has no file and is not cached."""
    mgr = SessionManager(tmp_path)
    assert mgr.peek("tui:ghost") is None


def test_flush_saves_dirty_session(tmp_path: Path):
    """flush() persists a session with unpersisted messages and returns True."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:flush01")
    session.add_message("user", "first")
    mgr.save(session)
    session.add_message("assistant", "second")
    assert session._persisted_count == 1

    assert mgr.flush("tui:flush01") is True

    path = tmp_path / "sessions" / "tui" / "flush01.jsonl"
    lines = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    msg_lines = [ln for ln in lines if ln.get("_type") != "metadata"]
    assert len(msg_lines) == 2


def test_flush_skips_clean_session(tmp_path: Path):
    """flush() does not rewrite a clean session and returns True."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:flush02")
    session.add_message("user", "saved")
    mgr.save(session)
    path = tmp_path / "sessions" / "tui" / "flush02.jsonl"
    before = path.read_text()

    assert mgr.flush("tui:flush02") is True
    assert path.read_text() == before


def test_flush_does_nothing_for_uncached_key(tmp_path: Path):
    """flush() is a no-op for an uncached key and returns True."""
    mgr = SessionManager(tmp_path)
    assert mgr.flush("tui:not_in_cache") is True


def test_flush_returns_false_when_save_fails(tmp_path: Path, monkeypatch):
    """flush() swallows a save failure and returns False."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:flush03")
    session.add_message("user", "dirty")

    def _boom_save(s) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mgr, "save", _boom_save)
    assert mgr.flush("tui:flush03") is False


def test_exists_true_for_saved_session(tmp_path: Path):
    """exists() is True once the session file is on disk."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:ex01")
    session.add_message("user", "x")
    mgr.save(session)
    assert mgr.exists("tui:ex01") is True


def test_exists_false_for_lazy_or_unknown_session(tmp_path: Path):
    """exists() is False for a lazy (never-saved) or unknown key."""
    mgr = SessionManager(tmp_path)
    mgr.get_or_create("tui:ex02")
    assert mgr.exists("tui:ex02") is False
    assert mgr.exists("tui:ghost") is False


# ---------------------------------------------------------------------------
# Extended list_sessions: channel filter + message_count
# ---------------------------------------------------------------------------


def test_list_sessions_channel_filter(tmp_path: Path):
    """list_sessions(channel='tui') returns only tui sessions."""
    mgr = SessionManager(tmp_path)
    for key in ("tui:ch01", "cli:ch02", "tui:ch03"):
        s = mgr.get_or_create(key)
        s.add_message("user", "x")
        mgr.save(s)

    tui_sessions = mgr.list_sessions(channel="tui")
    keys = {info["key"] for info in tui_sessions}
    assert keys == {"tui:ch01", "tui:ch03"}


def test_list_sessions_no_channel_returns_all(tmp_path: Path):
    """list_sessions() with no filter returns all channels (backward compat)."""
    mgr = SessionManager(tmp_path)
    for key in ("tui:all01", "cli:all02"):
        s = mgr.get_or_create(key)
        s.add_message("user", "x")
        mgr.save(s)

    assert len(mgr.list_sessions()) == 2


def test_list_sessions_includes_message_count(tmp_path: Path):
    """list_sessions entries include message_count matching the stored messages."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:mc01")
    for i in range(3):
        session.add_message("user", f"msg{i}")
    mgr.save(session)

    entries = mgr.list_sessions()
    assert len(entries) == 1
    assert entries[0]["message_count"] == 3


def test_list_sessions_message_count_excludes_metadata_lines(tmp_path: Path):
    """message_count counts only message lines, not metadata records."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:mc02")
    session.add_message("user", "one")
    mgr.save(session)
    session.add_message("assistant", "two")
    mgr.save(session)

    entries = mgr.list_sessions()
    assert entries[0]["message_count"] == 2


def _msg(role, content):
    return {"role": role, "content": content}


def test_undo_last_turn_drops_last_user_block():
    s = Session(key="tui:t1")
    s.messages = [
        _msg("user", "q1"), _msg("assistant", "a1"),
        _msg("user", "q2"), _msg("assistant", "a2"), _msg("tool", "t2"),
    ]
    removed = s.undo_last_turn()
    assert removed == 3
    assert [m["content"] for m in s.messages] == ["q1", "a1"]


def test_undo_last_turn_no_user_returns_zero():
    s = Session(key="tui:t1")
    s.messages = [_msg("assistant", "a1"), _msg("tool", "t1")]
    assert s.undo_last_turn() == 0
    assert len(s.messages) == 2


def test_undo_last_turn_empty_session_returns_zero():
    s = Session(key="tui:t1")
    assert s.undo_last_turn() == 0


def test_undo_last_turn_never_crosses_last_consolidated():
    s = Session(key="tui:t1")
    s.messages = [
        _msg("user", "q1"), _msg("assistant", "a1"),
        _msg("user", "q2"), _msg("assistant", "a2"),
    ]
    s.last_consolidated = 2
    removed = s.undo_last_turn()
    assert removed == 2
    assert [m["content"] for m in s.messages] == ["q1", "a1"]
    assert s.undo_last_turn() == 0
    assert len(s.messages) == 2


def test_undo_last_turn_n_clamps_to_tail_first_user():
    s = Session(key="tui:t1")
    s.messages = [
        _msg("user", "q1"), _msg("assistant", "a1"),
        _msg("user", "q2"), _msg("assistant", "a2"),
    ]
    removed = s.undo_last_turn(n=5)
    assert removed == 4
    assert s.messages == []


def test_clear_then_save_truncates_file_on_disk(tmp_path):
    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("tui:keepme")
    s.record({"role": "user", "content": "q1"})
    s.record({"role": "assistant", "content": "a1"})
    mgr.save(s)
    assert mgr.exists("tui:keepme")

    s.clear()
    mgr.save(s)

    fresh = SessionManager(tmp_path)
    reloaded = fresh.get_or_create("tui:keepme")
    assert reloaded.messages == []
    assert reloaded.key == "tui:keepme"


def test_undo_then_save_truncates_file_on_disk(tmp_path):
    from raven.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("tui:undome")
    for role, content in [("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")]:
        s.record({"role": role, "content": content})
    mgr.save(s)

    removed = s.undo_last_turn()
    assert removed == 2
    mgr.save(s)

    fresh = SessionManager(tmp_path)
    reloaded = fresh.get_or_create("tui:undome")
    assert [m["content"] for m in reloaded.messages] == ["q1", "a1"]
    assert reloaded.key == "tui:undome"

# ── fork (session fork/branch) ──────────────────────────────────────────────


def _seed(mgr: SessionManager, key: str, *turns: tuple[str, str]) -> Session:
    session = mgr.get_or_create(key)
    for role, content in turns:
        session.add_message(role, content)
    mgr.save(session)
    return session


def test_fork_copies_history_to_new_same_channel_session(tmp_path: Path):
    """fork mints a fresh same-channel chat_id holding a verbatim message copy."""
    mgr = SessionManager(tmp_path)
    _seed(mgr, "cli:src01", ("user", "q1"), ("assistant", "a1"))

    child = mgr.fork("cli:src01")

    assert child is not None
    assert child.key.startswith("cli:")
    assert child.key != "cli:src01"
    assert [m["content"] for m in child.messages] == ["q1", "a1"]


def test_fork_sets_parent_session_id_to_full_source_key(tmp_path: Path):
    """The child's parent_session_id is the source's full session key (composite)."""
    mgr = SessionManager(tmp_path)
    _seed(mgr, "cli:src02", ("user", "x"))

    child = mgr.fork("cli:src02")

    assert child.metadata["parent_session_id"] == "cli:src02"


def test_fork_leaves_source_unchanged(tmp_path: Path):
    """Forking does not mutate the source session on disk."""
    mgr = SessionManager(tmp_path)
    _seed(mgr, "cli:src03", ("user", "x"))

    mgr.fork("cli:src03")

    reloaded = SessionManager(tmp_path).get_or_create("cli:src03")
    assert reloaded.metadata.get("parent_session_id") is None
    assert [m["content"] for m in reloaded.messages] == ["x"]


def test_fork_child_is_persisted_immediately(tmp_path: Path):
    """fork is never lazy — the child file exists right after fork."""
    mgr = SessionManager(tmp_path)
    _seed(mgr, "cli:src04", ("user", "x"))

    child = mgr.fork("cli:src04")

    assert mgr.exists(child.key)
    loaded = SessionManager(tmp_path).get_or_create(child.key)
    assert [m["content"] for m in loaded.messages] == ["x"]


def test_fork_child_independent_after_parent_delete(tmp_path: Path):
    """Deleting the parent leaves the child's copied history intact."""
    mgr = SessionManager(tmp_path)
    _seed(mgr, "cli:src05", ("user", "q1"), ("assistant", "a1"))
    child = mgr.fork("cli:src05")

    mgr.delete("cli:src05")

    loaded = SessionManager(tmp_path).get_or_create(child.key)
    assert [m["content"] for m in loaded.messages] == ["q1", "a1"]


def test_fork_inherits_last_consolidated(tmp_path: Path):
    """The child inherits the source's last_consolidated boundary."""
    mgr = SessionManager(tmp_path)
    src = _seed(mgr, "cli:src06", ("user", "a"), ("assistant", "b"))
    src.last_consolidated = 1
    mgr.save(src)

    child = mgr.fork("cli:src06")

    assert child.last_consolidated == 1


def test_fork_resets_pending_clarification(tmp_path: Path):
    """The child does not carry the source's clarification wait-state."""
    mgr = SessionManager(tmp_path)
    src = _seed(mgr, "cli:src07", ("user", "a"))
    src.pending_clarification = {"original_message": "a", "question": "?", "domain": "d"}
    mgr.save(src)

    child = mgr.fork("cli:src07")

    assert child.pending_clarification is None


def test_fork_refuses_missing_source(tmp_path: Path):
    """Forking a source that does not exist returns None and creates nothing."""
    mgr = SessionManager(tmp_path)
    assert mgr.fork("cli:nope") is None


def test_fork_refuses_empty_source(tmp_path: Path):
    """Forking a zero-message source (e.g. titled-only) is refused."""
    mgr = SessionManager(tmp_path)
    titled = mgr.get_or_create("cli:src08")
    titled.metadata["title"] = "empty"
    mgr.save(titled)

    assert mgr.fork("cli:src08") is None


def test_fork_deepcopies_messages(tmp_path: Path):
    """Child messages are a deepcopy — mutating the source's nested content
    block after fork does not leak into the child."""
    mgr = SessionManager(tmp_path)
    src = mgr.get_or_create("cli:src09")
    src.record({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    mgr.save(src)

    child = mgr.fork("cli:src09")
    src.messages[0]["content"].append({"type": "text", "text": "MUTATED"})

    assert child.messages[0]["content"] == [{"type": "text", "text": "hi"}]


def test_fork_default_title_appends_fork_suffix(tmp_path: Path):
    """Without an explicit title, a titled parent yields '<title> (fork)'."""
    mgr = SessionManager(tmp_path)
    src = _seed(mgr, "cli:src10", ("user", "x"))
    src.metadata["title"] = "My chat"
    mgr.save(src)

    child = mgr.fork("cli:src10")

    assert child.metadata["title"] == "My chat (fork)"


def test_fork_untitled_parent_yields_no_title(tmp_path: Path):
    """An untitled parent yields a child with no title (no bare '(fork)')."""
    mgr = SessionManager(tmp_path)
    _seed(mgr, "cli:src11", ("user", "x"))

    child = mgr.fork("cli:src11")

    assert child.metadata.get("title") is None


def test_fork_explicit_title_overrides(tmp_path: Path):
    """An explicit title is used verbatim."""
    mgr = SessionManager(tmp_path)
    _seed(mgr, "cli:src12", ("user", "x"))

    child = mgr.fork("cli:src12", title="Custom")

    assert child.metadata["title"] == "Custom"


# ── resolve_key (shared cross-channel resolution core) ─────────────────


def test_resolve_key_full_key_passthrough(tmp_path: Path):
    """A value carrying ':' is treated as a full key, no lookup."""
    mgr = SessionManager(tmp_path)
    res = mgr.resolve_key("feishu:abc123")
    assert res.status == "resolved"
    assert res.key == "feishu:abc123"


def test_resolve_key_bare_exact_cross_channel(tmp_path: Path):
    """A bare chat_id resolves to its full key on whatever channel holds it."""
    mgr = SessionManager(tmp_path)
    cid = "20990101_000000_aaaaaa"
    _seed(mgr, f"tui:{cid}", ("user", "hi"))
    res = mgr.resolve_key(cid)
    assert res.status == "resolved"
    assert res.key == f"tui:{cid}"


def test_resolve_key_bare_prefix_unique(tmp_path: Path):
    """A unique prefix resolves to the single matching key."""
    mgr = SessionManager(tmp_path)
    cid = "20990101_000000_cccccc"
    _seed(mgr, f"cli:{cid}", ("user", "hi"))
    res = mgr.resolve_key(cid[:20])
    assert res.status == "resolved"
    assert res.key == f"cli:{cid}"


def test_resolve_key_ambiguous_returns_candidates(tmp_path: Path):
    """The same bare id on two channels is ambiguous; both keys surface."""
    mgr = SessionManager(tmp_path)
    cid = "20990101_000000_dddddd"
    _seed(mgr, f"cli:{cid}", ("user", "hi"))
    _seed(mgr, f"tui:{cid}", ("user", "hi"))
    res = mgr.resolve_key(cid)
    assert res.status == "ambiguous"
    assert set(res.candidates) == {f"cli:{cid}", f"tui:{cid}"}


def test_resolve_key_not_found(tmp_path: Path):
    """No match anywhere yields not_found (no minting, no fallback)."""
    mgr = SessionManager(tmp_path)
    res = mgr.resolve_key("nope000")
    assert res.status == "not_found"
    assert res.key is None
