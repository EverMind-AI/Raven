"""Tests for SessionManager."""

import gc
import json
import multiprocessing
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import raven.session.manager as manager_mod
from raven.session.manager import Session, SessionManager, new_chat_id
from raven.session.title import TITLE_STORAGE_MAX


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


def _last_metadata(path: Path) -> dict:
    last: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("_type") == "metadata":
            last = row.get("metadata") or {}
    return last


def test_a_save_keeps_a_key_another_writer_added(tmp_path: Path):
    """A save speaks for the keys its own copy carries and for no others.

    A page and a terminal over one home are two managers over one file. The
    saved record used to be this copy's metadata whole, so a flag the other
    one wrote after this copy was loaded -- archiving, most visibly -- was gone
    the next time anything here saved, and the conversation came back.
    """
    key = "tui:20260610_100000_merge"
    holder = SessionManager(tmp_path)
    session = holder.get_or_create(key)
    session.add_message("user", "hello")
    holder.save(session)

    SessionManager(tmp_path).append_metadata_patch(key, {"archived": True})
    assert session.metadata.get("archived") is None, "this copy never saw it"

    session.add_message("user", "still talking here")
    holder.save(session)

    path = holder.session_path(key)
    assert _last_metadata(path).get("archived") is True
    assert SessionManager(tmp_path).peek(key).metadata.get("archived") is True


def test_a_save_does_not_write_back_a_value_it_only_read(tmp_path: Path):
    """A copy speaks for what it changed, not for every key it happens to hold.

    ``metadata`` is the whole snapshot this copy loaded, so overlaying it whole
    keeps a key it never touched at the value it read: restore persists
    archived False, a second client loads that, the first archives again, and
    the second's next ordinary turn writes its stale False back over the newer
    True -- the conversation comes back after having been restored once, which
    is the shape a reader meets it in.
    """
    key = "tui:20260610_100000_stale"
    first = SessionManager(tmp_path)
    session = first.get_or_create(key)
    session.add_message("user", "hello")
    session.metadata["archived"] = False
    first.save(session)

    # A second client loads the conversation, archived False and all.
    second = SessionManager(tmp_path)
    held = second.get_or_create(key)
    assert held.metadata["archived"] is False

    first.append_metadata_patch(key, {"archived": True})

    held.add_message("user", "an ordinary turn over here")
    second.save(held)

    assert _last_metadata(second.session_path(key)).get("archived") is True


def test_a_patch_stops_being_a_local_change_once_it_is_on_disk(tmp_path: Path):
    """A key this copy wrote through is written, not still pending.

    ``append_metadata_patch`` puts the key on the file and on the cached
    session, so it is no longer that copy's unsaved opinion. Left out of the
    baseline it reads as a local change for the rest of the process's life and
    is asserted again on every later save -- so a client that archived once
    would undo somebody else's restore with its next ordinary turn.
    """
    key = "tui:20260610_100000_patched"
    first = SessionManager(tmp_path)
    session = first.get_or_create(key)
    session.add_message("user", "hello")
    session.metadata["archived"] = False
    first.save(session)

    first.append_metadata_patch(key, {"archived": True})
    assert session.metadata["archived"] is True

    SessionManager(tmp_path).append_metadata_patch(key, {"archived": False})

    session.add_message("user", "an ordinary turn, long after")
    first.save(session)

    assert _last_metadata(first.session_path(key)).get("archived") is False


def test_a_save_does_not_resurrect_a_key_this_copy_cleared(tmp_path: Path):
    """The merge keeps what it did not write, which makes clearing explicit.

    Nothing may clear a key by leaving it out any more -- the record on disk
    would hand it back. Every remover states a false value instead, and this
    pins that the stated value wins over the one on disk.
    """
    key = "tui:20260610_100000_cleared"
    holder = SessionManager(tmp_path)
    session = holder.get_or_create(key)
    session.add_message("user", "hello")
    session.metadata["pinned"] = True
    holder.save(session)

    # Another writer touches the file, so the next save has to merge.
    SessionManager(tmp_path).append_metadata_patch(key, {"archived": True})

    session.metadata["pinned"] = False
    session.add_message("user", "unpinned now")
    holder.save(session)

    stored = _last_metadata(holder.session_path(key))
    assert stored.get("pinned") is False
    assert stored.get("archived") is True


def test_a_save_over_a_transcript_with_no_record_writes_its_own(tmp_path: Path):
    """Nothing to merge under is not an error; this copy's metadata stands.

    A transcript can lose its metadata record -- a crashed writer, a truncated
    file -- and the merge has nothing to read. Refusing to save then would
    lose the conversation over a record that is already gone.
    """
    key = "tui:20260610_100000_norecord"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(key)
    session.add_message("user", "hello")
    session.metadata["archived"] = True
    mgr.save(session)

    path = mgr.session_path(key)
    kept = [line for line in path.read_text(encoding="utf-8").splitlines() if '"_type": "metadata"' not in line]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    session.add_message("user", "again")
    mgr.save(session)

    assert _last_metadata(path).get("archived") is True


def test_a_save_re_reads_only_when_the_file_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The merge costs a scan, and an undisturbed conversation does not pay it.

    Every turn saves, so re-reading the transcript on each one would put the
    file's whole length on the hot path. The copy knows what it last wrote, so
    it only looks when the bytes moved.
    """
    key = "tui:20260610_100000_stamp"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(key)
    session.add_message("user", "hello")
    mgr.save(session)

    scans: list[Path] = []
    real_scan = mgr._scan_file
    monkeypatch.setattr(mgr, "_scan_file", lambda path: (scans.append(path), real_scan(path))[1])

    session.add_message("user", "nobody else wrote")
    mgr.save(session)
    assert scans == []

    SessionManager(tmp_path).append_metadata_patch(key, {"archived": True})
    session.add_message("user", "somebody did")
    mgr.save(session)
    assert scans == [mgr.session_path(key)]


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

    first_line = (tmp_path / "sessions" / "tui" / "meta01.jsonl").read_text(encoding="utf-8").splitlines()[0]
    meta = json.loads(first_line)["metadata"]
    assert meta["channel"] == "tui"
    assert meta["chat_id"] == "meta01"
    assert meta["parent_session_id"] is None
    assert "source" in meta
    assert "title" in meta


def test_metadata_model_survives_a_save_load_round_trip(tmp_path: Path):
    """metadata["model"] is the per-session model's home; the append-only file's
    last metadata record must win on reload."""
    mgr = SessionManager(tmp_path)
    s = mgr.get_or_create("web:abc")
    s.metadata["model"] = "deepseek/deepseek-v3"
    mgr.save(s)

    s.metadata["model"] = "anthropic/claude-opus-4-5"
    mgr.save(s)

    mgr.invalidate("web:abc")
    assert mgr.get_or_create("web:abc").metadata["model"] == "anthropic/claude-opus-4-5"


def test_load_preserves_on_disk_message_order(tmp_path: Path):
    """Messages keep file order on load even when received_at is out of order."""
    session_dir = tmp_path / "sessions" / "tui"
    session_dir.mkdir(parents=True)
    lines = [
        {"_type": "metadata", "key": "tui:order01", "metadata": {}},
        {"role": "user", "content": "late", "received_at": "2026-06-10T10:00:05"},
        {"role": "user", "content": "early", "received_at": "2026-06-10T10:00:01"},
    ]
    (session_dir / "order01.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

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


def test_a_session_holding_only_its_open_question_is_a_complete_row(tmp_path: Path):
    """A turn now files its question before it starts answering, so a session
    whose first turn is still running is on disk with exactly one message. The
    picker has to be able to draw that row -- a name, a count and a stamp --
    rather than skip it as half-written."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:opening")
    session.add_message("user", "read the repo and summarise it")
    mgr.save(session)

    rows = [info for info in mgr.list_sessions() if info["key"] == "tui:opening"]
    assert len(rows) == 1
    row = rows[0]
    assert row["message_count"] == 1
    assert row["first_user_message"] == "read the repo and summarise it"
    assert row["metadata"]["title"] == "read the repo and summarise it"
    assert row["metadata"]["title_auto"] is True
    assert row["last_message_at"], "the row has no stamp to sort the picker by"


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


def test_find_most_recent_can_exclude_archived_sessions(tmp_path: Path):
    """Resume skips archived sessions while delivery keeps its existing default."""
    mgr = SessionManager(tmp_path)
    active = mgr.get_or_create("tui:active")
    active.add_message("user", "keep visible")
    active.updated_at = datetime(2026, 6, 10, 10, 0, 0)
    mgr.save(active)

    archived = mgr.get_or_create("tui:archived")
    archived.add_message("user", "hide me")
    archived.metadata["archived"] = True
    archived.updated_at = datetime(2026, 6, 10, 11, 0, 0)
    mgr.save(archived)

    assert mgr.find_most_recent_chat_id("tui") == "archived"
    assert mgr.find_most_recent_chat_id("tui", include_archived=False) == "active"


def test_find_most_recent_ignores_old_flat_files(tmp_path: Path):
    """Pre-refactor flat files are ignored for lookup but never deleted."""
    _seed_nested(tmp_path, "tui", "nested01", "2026-06-10T10:00:00")
    flat = tmp_path / "sessions" / "tui_flat01.jsonl"
    flat.write_text(
        json.dumps(
            {
                "_type": "metadata",
                "key": "tui:flat01",
                "updated_at": "2026-06-10T23:59:59",
            }
        )
        + "\n",
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
    # spawn, not the Linux default fork: pytest leaves the parent multi-threaded,
    # and forking from there segfaults the interpreter at exit.
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_turn_worker, args=(str(tmp_path), key, w)) for w in range(2)]
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


def test_find_most_recent_prefers_sessions_with_messages(tmp_path: Path):
    """A freshly minted zero-message session (bare ``sessions create``) must
    not hijack recency: the newest session WITH messages wins even when an
    empty one carries a later updated_at."""
    mgr = SessionManager(tmp_path)
    real = mgr.get_or_create("cli:real01")
    real.add_message("user", "hello")
    real.updated_at = datetime(2026, 6, 10, 10, 0, 0)
    mgr.save(real)

    empty = mgr.get_or_create("cli:empty01")
    empty.updated_at = datetime(2026, 6, 10, 11, 0, 0)
    mgr.save(empty)

    assert mgr.find_most_recent_chat_id("cli") == "real01"


def test_find_most_recent_falls_back_to_empty_when_all_empty(tmp_path: Path):
    """When every session on the channel has zero messages, the newest empty
    one is still returned rather than None."""
    _seed_nested(tmp_path, "cli", "older", "2026-06-10T10:00:00")
    _seed_nested(tmp_path, "cli", "newer", "2026-06-10T11:00:00")

    mgr = SessionManager(tmp_path)
    assert mgr.find_most_recent_chat_id("cli") == "newer"


def test_loader_skips_partial_trailing_line(tmp_path: Path):
    """A crash mid-append leaves a partial trailing line; loader skips it."""
    session_dir = tmp_path / "sessions" / "tui"
    session_dir.mkdir(parents=True)
    full = json.dumps({"role": "user", "content": "full"})
    (session_dir / "crash01.jsonl").write_text(
        json.dumps({"_type": "metadata", "key": "tui:crash01", "metadata": {}})
        + "\n"
        + full
        + "\n"
        + '{"role": "assistant", "content": "tru',
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
        json.dumps({"_type": "metadata", "key": "tui:x"})
        + "\n"
        + json.dumps({"role": "user", "content": "old"})
        + "\n",
        encoding="utf-8",
    )

    session = SessionManager(tmp_path / "chanwork").get_or_create("tui:x")
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


class _DeleteProbe:
    """A paper-shaped observer (contracts/session_events.py) recording calls."""

    def __init__(self, tag: str = "probe", log: list | None = None) -> None:
        self.tag = tag
        self.log = log if log is not None else []

    def on_session_deleted(self, session_key: str, removed: bool) -> None:
        self.log.append((self.tag, session_key, removed))


def _saved(mgr: SessionManager, key: str) -> Path:
    session = mgr.get_or_create(key)
    session.add_message("user", "x")
    mgr.save(session)
    return mgr.session_path(key)


def test_a_fresh_manager_notifies_nobody(tmp_path: Path):
    """The empty roster is the default: a manager nobody attached to (the CLI
    face builds exactly this shape) deletes as it always did. Cross-process
    deletion staying out of sight is the paper's third discipline."""
    mgr = SessionManager(tmp_path)
    assert mgr._delete_observers == ()
    _saved(mgr, "tui:obs00")
    assert mgr.delete("tui:obs00") is True


def test_delete_notifies_observers_with_removed_true(tmp_path: Path):
    """A delete that removed a file reports removed=True, after the store acted."""
    mgr = SessionManager(tmp_path)
    probe = _DeleteProbe()
    mgr.set_delete_observers((probe,))
    path = _saved(mgr, "tui:obs01")

    assert mgr.delete("tui:obs01") is True
    assert not path.exists()
    assert probe.log == [("probe", "tui:obs01", True)]


def test_delete_notifies_observers_with_removed_false_when_no_file(tmp_path: Path):
    """The observer fires for every delete request the store handles -- a
    cache-only session (minted, never saved) still notifies, with removed=False."""
    mgr = SessionManager(tmp_path)
    probe = _DeleteProbe()
    mgr.set_delete_observers((probe,))
    mgr.get_or_create("tui:obs02")

    assert mgr.delete("tui:obs02") is False
    assert probe.log == [("probe", "tui:obs02", False)]


def test_delete_notifies_observers_with_removed_false_when_unlink_fails(tmp_path: Path, monkeypatch):
    mgr = SessionManager(tmp_path)
    probe = _DeleteProbe()
    mgr.set_delete_observers((probe,))
    _saved(mgr, "tui:obs03")

    def _boom_unlink(self, missing_ok=False):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", _boom_unlink)
    assert mgr.delete("tui:obs03") is False
    assert probe.log == [("probe", "tui:obs03", False)]


def test_delete_observers_hear_in_registration_order_each_once(tmp_path: Path):
    mgr = SessionManager(tmp_path)
    log: list = []
    mgr.set_delete_observers((_DeleteProbe("first", log), _DeleteProbe("second", log)))
    _saved(mgr, "tui:obs04")

    mgr.delete("tui:obs04")
    assert log == [("first", "tui:obs04", True), ("second", "tui:obs04", True)]


def test_a_raising_observer_is_skipped_and_the_rest_still_hear(tmp_path: Path):
    """An observer that raises is logged and skipped: the deletion's outcome is
    already decided, the return value stands, and later observers still hear."""

    class _Broken:
        def on_session_deleted(self, session_key: str, removed: bool) -> None:
            raise RuntimeError("ledger unreachable")

    mgr = SessionManager(tmp_path)
    probe = _DeleteProbe()
    mgr.set_delete_observers((_Broken(), probe))
    path = _saved(mgr, "tui:obs05")

    assert mgr.delete("tui:obs05") is True
    assert not path.exists()
    assert probe.log == [("probe", "tui:obs05", True)]


def test_set_delete_observers_replaces_the_whole_tuple(tmp_path: Path):
    """The setter is whole-tuple replacement, idempotent: re-attaching the same
    tuple never doubles a notification, and attaching () detaches."""
    mgr = SessionManager(tmp_path)
    log: list = []
    first, second = _DeleteProbe("first", log), _DeleteProbe("second", log)

    mgr.set_delete_observers((first,))
    mgr.set_delete_observers((first,))
    _saved(mgr, "tui:obs06")
    mgr.delete("tui:obs06")
    assert log == [("first", "tui:obs06", True)]

    mgr.set_delete_observers((second,))
    _saved(mgr, "tui:obs07")
    mgr.delete("tui:obs07")
    assert log == [("first", "tui:obs06", True), ("second", "tui:obs07", True)]

    mgr.set_delete_observers(())
    _saved(mgr, "tui:obs08")
    mgr.delete("tui:obs08")
    assert len(log) == 2


def test_read_session_file_reads_a_transcript_outside_the_store(tmp_path: Path):
    path = tmp_path / "elsewhere" / "session.jsonl"
    assert manager_mod.read_session_file(path, "imported:x") is None
    path.parent.mkdir()
    rows = [
        {"_type": "metadata", "metadata": {"title": "first"}},
        {"role": "user", "content": "hi"},
        {"_type": "metadata", "metadata": {"title": "second"}, "last_consolidated": 1},
        {"role": "assistant", "content": "hello"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + '{"role": "user", "cont', encoding="utf-8")
    session = manager_mod.read_session_file(path, "imported:x")
    assert session is not None
    assert session.key == "imported:x"
    assert session.metadata == {"title": "second"}
    assert session.last_consolidated == 1
    assert [m["content"] for m in session.messages] == ["hi", "hello"]
    path.write_text('{"_type": "metadata", "created_at": "not a date"}\n', encoding="utf-8")
    assert manager_mod.read_session_file(path, "imported:x") is None


def test_the_store_refuses_to_keep_an_imported_conversation(tmp_path: Path):
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("imported:" + "a" * 32)
    session.add_message("user", "hi")
    with pytest.raises(ValueError, match="imported conversation"):
        mgr.save(session)
    with pytest.raises(ValueError, match="imported conversation"):
        mgr.fork("imported:" + "a" * 32)
    assert not mgr.sessions_dir.exists() or not any(mgr.sessions_dir.rglob("*.jsonl"))
    other = mgr.get_or_create("tui:imported")
    other.add_message("user", "hi")
    mgr.save(other)
    assert mgr.session_path("tui:imported").is_file()


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


def test_flush_writes_a_cut_whose_own_save_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = SessionManager(tmp_path)
    session = _seed(mgr, "tui:flush-cut", ("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2"))
    session.undo_last_turn()
    _fail_to_rewrite(monkeypatch, mgr, session)

    assert mgr.flush("tui:flush-cut") is True
    reloaded = SessionManager(tmp_path).get_or_create("tui:flush-cut")
    assert [m["content"] for m in reloaded.messages] == ["q1", "a1"]


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


def test_list_sessions_multi_channel_filter(tmp_path: Path):
    mgr = SessionManager(tmp_path)
    for key in ("tui:a", "cron:b", "cli:c"):
        session = mgr.get_or_create(key)
        session.add_message("user", key)
        mgr.save(session)

    assert {info["key"] for info in mgr.list_sessions(channels={"tui", "cron"})} == {"tui:a", "cron:b"}


def test_list_sessions_rejects_two_filter_modes(tmp_path: Path):
    mgr = SessionManager(tmp_path)

    with pytest.raises(ValueError, match="mutually exclusive"):
        mgr.list_sessions(channel="tui", channels={"tui"})


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
        _msg("user", "q1"),
        _msg("assistant", "a1"),
        _msg("user", "q2"),
        _msg("assistant", "a2"),
        _msg("tool", "t2"),
    ]
    removed = s.undo_last_turn()
    assert removed == 3
    assert [m["content"] for m in s.messages] == ["q1", "a1"]


def test_undo_last_turn_pulls_the_episodic_cursor_back_to_the_cut():
    s = Session(key="cli:u")
    for i in range(4):
        s.messages.extend([{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}])
    s.metadata["annotated_through"] = 8

    assert s.undo_last_turn(2) == 4
    assert s.metadata["annotated_through"] == 4

    s.metadata["annotated_through"] = 2
    assert s.undo_last_turn(1) == 2
    assert s.metadata["annotated_through"] == 2


def test_a_fork_starts_annotated_through_its_inherited_history(tmp_path: Path):
    """A fork's past is its parent's, and a past is never handed to one call.

    The after-turn step seeds a session it has not seen before, so a fork is
    safe on that path. ``/new`` is the one that is not: it reads the cursor
    directly, and on an unseeded fork it reads 0 and hands every inherited
    message to the annotator -- episodes the parent already wrote to the same
    workspace log.
    """
    sm = SessionManager(tmp_path)
    parent = sm.get_or_create("cli:p")
    for i in range(3):
        parent.messages.extend([{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}])
    parent.metadata["annotated_through"] = 6
    sm.save(parent)

    child = sm.fork("cli:p")

    assert child is not None
    assert child.metadata["annotated_through"] == len(child.messages) == 6

    sm._cache.pop(child.key, None)
    assert sm.get_or_create(child.key).metadata.get("annotated_through") == 6


def test_clear_resets_the_episodic_cursor_through_a_save(tmp_path: Path):
    """Asserted after a save and a reload, because that is where it was lost.

    An in-memory check passes while the cursor is merely dropped, and a
    dropped key comes back on the next save (the removal rule in
    ``_metadata_to_write``). A cleared session carrying its old cursor
    swallows every turn written after the clear until the count climbs past
    it.
    """
    sm = SessionManager(tmp_path)
    s = sm.get_or_create("cli:u")
    for i in range(5):
        s.messages.extend([{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}])
    sm.save(s)
    sm.append_metadata_patch("cli:u", {"annotated_through": 10})
    s.metadata["annotated_through"] = 10

    s.clear()
    sm.save(s)
    sm._cache.pop("cli:u", None)

    assert sm.get_or_create("cli:u").metadata.get("annotated_through") == 0


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
        _msg("user", "q1"),
        _msg("assistant", "a1"),
        _msg("user", "q2"),
        _msg("assistant", "a2"),
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
        _msg("user", "q1"),
        _msg("assistant", "a1"),
        _msg("user", "q2"),
        _msg("assistant", "a2"),
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


def _fail_to_rewrite(monkeypatch: pytest.MonkeyPatch, mgr: SessionManager, session: Session) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patched:
        patched.setattr(manager_mod, "atomic_replace", _boom)
        with pytest.raises(OSError):
            mgr.save(session)


@pytest.mark.parametrize("turns_after", [1, 2], ids=["back-to-the-saved-count", "past-the-saved-count"])
def test_turns_after_an_undo_whose_save_failed_replace_the_undone_one_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, turns_after: int
) -> None:
    mgr = SessionManager(tmp_path)
    session = _seed(mgr, "tui:undo-unsaved", ("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2"))
    assert session.undo_last_turn() == 2
    _fail_to_rewrite(monkeypatch, mgr, session)
    for n in range(turns_after):
        session.add_message("user", f"again {n}")
        session.add_message("assistant", f"answer {n}")
    mgr.save(session)

    reloaded = SessionManager(tmp_path).get_or_create("tui:undo-unsaved")
    assert [m["content"] for m in reloaded.messages] == [m["content"] for m in session.messages]


def test_a_turn_after_a_clear_whose_save_failed_replaces_the_cleared_one_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = SessionManager(tmp_path)
    session = _seed(mgr, "tui:clear-unsaved", ("user", "old question"), ("assistant", "old answer"))
    session.clear()
    _fail_to_rewrite(monkeypatch, mgr, session)
    session.add_message("user", "new question")
    session.add_message("assistant", "new answer")
    mgr.save(session)

    reloaded = SessionManager(tmp_path).get_or_create("tui:clear-unsaved")
    assert [m["content"] for m in reloaded.messages] == ["new question", "new answer"]


def test_once_a_cut_is_on_disk_a_save_keeps_what_another_writer_appended(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    session = _seed(mgr, "tui:cut-saved", ("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2"))
    session.undo_last_turn()
    mgr.save(session)
    elsewhere_mgr = SessionManager(tmp_path)
    elsewhere = elsewhere_mgr.get_or_create("tui:cut-saved")
    elsewhere.add_message("user", "from elsewhere")
    elsewhere_mgr.save(elsewhere)
    session.add_message("user", "from here")
    mgr.save(session)

    reloaded = SessionManager(tmp_path).get_or_create("tui:cut-saved")
    assert [m["content"] for m in reloaded.messages] == ["q1", "a1", "from elsewhere", "from here"]


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


def test_fork_inherits_the_model_the_parent_chose(tmp_path: Path):
    """A fork continues its parent's conversation, so it continues on its model.

    The branch handler re-points the live binding, but that is in memory only.
    Without the record the fork reads the default the first time it is resumed
    in a new process, which is a silent downgrade rather than a visible one.
    """
    mgr = SessionManager(tmp_path)
    src = _seed(mgr, "cli:src08", ("user", "a"))
    src.metadata["model"] = "anthropic/claude-opus-4-8"
    src.metadata["provider"] = "anthropic"
    mgr.save(src)

    child = mgr.fork("cli:src08")

    reloaded = SessionManager(tmp_path).get_or_create(child.key)
    assert reloaded.metadata["model"] == "anthropic/claude-opus-4-8"
    assert reloaded.metadata["provider"] == "anthropic"


def test_fork_of_an_unswitched_parent_carries_no_model(tmp_path: Path):
    """The parent never chose one, so the fork must start on the default too --
    not on a model copied out of nowhere.
    """
    mgr = SessionManager(tmp_path)
    _seed(mgr, "cli:src09", ("user", "a"))

    child = mgr.fork("cli:src09")

    assert "model" not in child.metadata
    assert "provider" not in child.metadata


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
    """Without an explicit title, a titled parent yields '<title> (fork)'.

    The parent was auto-named on its first save; the human rename via
    set_title clears the marker, so the new title is inherited."""
    mgr = SessionManager(tmp_path)
    src = _seed(mgr, "cli:src10", ("user", "x"))
    src.set_title("My chat")
    mgr.save(src)

    child = mgr.fork("cli:src10")

    assert child.metadata["title"] == "My chat (fork)"


def test_fork_untitled_parent_yields_no_title(tmp_path: Path):
    """A parent with no title at all yields a child with none: no bare '(fork)'.

    A conversation that opened with something other than a user message has
    nothing for ``save`` to auto-name it from, which is the one way a saved
    session still holds no title.
    """
    mgr = SessionManager(tmp_path)
    _seed(mgr, "cli:src11", ("assistant", "x"))
    assert mgr.peek("cli:src11").metadata.get("title") is None

    child = mgr.fork("cli:src11")

    assert child.metadata.get("title") is None


def test_fork_of_a_parent_at_the_storage_ceiling_keeps_a_storable_name(tmp_path: Path):
    """The derived name obeys the storage ceiling instead of overshooting it.

    A fork name is not typed by anyone, so the parent's tail gives way to the
    suffix rather than the fork losing its name to a refusal.
    """
    mgr = SessionManager(tmp_path)
    src = mgr.get_or_create("cli:src15")
    src.set_title("p" * TITLE_STORAGE_MAX)
    src.add_message("user", "x")
    mgr.save(src)

    child = mgr.fork("cli:src15")

    assert len(child.metadata["title"]) == TITLE_STORAGE_MAX
    assert child.metadata["title"].endswith(" (fork)")


def test_fork_explicit_title_overrides(tmp_path: Path):
    """An explicit title is used verbatim."""
    mgr = SessionManager(tmp_path)
    _seed(mgr, "cli:src12", ("user", "x"))

    child = mgr.fork("cli:src12", title="Custom")

    assert child.metadata["title"] == "Custom"


def test_fork_inherits_human_title_equal_to_auto_derivation(tmp_path: Path):
    """A human title that happens to equal the auto-derived text of the first
    user message is still human: the child inherits '<title> (fork)'."""
    mgr = SessionManager(tmp_path)
    src = mgr.get_or_create("cli:src13")
    src.set_title("Plan the trip")
    src.add_message("user", "Plan the trip")
    mgr.save(src)

    child = mgr.fork("cli:src13")

    assert child.metadata["title"] == "Plan the trip (fork)"


def test_fork_inherits_an_auto_named_title_too(tmp_path: Path):
    """An auto-named source is still a named source, and its fork carries that
    name, even after a disk round-trip.

    A fork is never auto-named -- its first user message names the fork point's
    ancestor -- so when inheritance skipped auto-named titles as well, the fork
    of an auto-named conversation ended up with no title at all and every one of
    them read alike under the front end's placeholder.
    """
    mgr = SessionManager(tmp_path)
    _seed(mgr, "cli:src14", ("user", "Plan the trip"))

    reloaded_mgr = SessionManager(tmp_path)
    source = reloaded_mgr.peek("cli:src14")
    assert source.metadata.get("title") == "Plan the trip"
    assert source.metadata.get("title_auto") is True

    child = reloaded_mgr.fork("cli:src14")

    assert child.metadata["title"] == "Plan the trip (fork)"
    # The fork's own name from here: nothing regenerates it behind the reader.
    assert child.metadata.get("title_auto") is None


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


def test_set_title_collapses_a_multi_line_name(tmp_path: Path):
    """A pasted name arrives as one line.

    A metadata record is one JSON line and every surface draws a title on one
    row, so an embedded newline has nowhere to render.
    """
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("cli:title1")

    session.set_title("  Ship\nthe   fix  ")

    assert session.metadata["title"] == "Ship the fix"


def test_set_title_refuses_a_name_past_the_storage_ceiling(tmp_path: Path):
    """Refused, not truncated: a person typed this.

    Quietly storing the first 200 characters hands back a fragment they never
    wrote, with nothing saying why -- so the write fails and the caller reports.
    """
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("cli:title2")

    with pytest.raises(ValueError, match="201 characters"):
        session.set_title("x" * 201)

    assert session.metadata.get("title") is None


def test_generated_title_replaces_an_auto_named_one(tmp_path: Path):
    mgr = SessionManager(tmp_path)
    session = _seed(mgr, "cli:title3", ("user", "please cut a release for the desktop build"))
    assert session.metadata.get("title_auto") is True

    assert session.set_generated_title("Cut a desktop release") is True
    assert session.metadata["title"] == "Cut a desktop release"


def test_generated_title_is_declined_once_a_person_named_the_session(tmp_path: Path):
    """The naming call ran alongside the turn, so a rename typed while it was in
    flight is the newer intent and wins."""
    mgr = SessionManager(tmp_path)
    session = _seed(mgr, "cli:title4", ("user", "please cut a release"))
    session.set_title("Release checklist")

    assert session.set_generated_title("Cut a release") is False
    assert session.metadata["title"] == "Release checklist"


def test_generated_title_is_carried_by_a_fork_and_the_marker_is_not(tmp_path: Path):
    """A generated title names the conversation, so the fork carries it.

    The marker still says the source's title is not human -- that is what keeps
    a later *generation* able to replace it, where a rename overwrites either
    kind without consulting it -- but the marker does not travel: the child's
    name is the fork's own from the moment it is minted.
    """
    mgr = SessionManager(tmp_path)
    source = _seed(mgr, "cli:title5", ("user", "plan the trip in detail"))
    source.set_generated_title("Plan the trip")
    mgr.save(source)

    child = mgr.fork("cli:title5")

    assert source.metadata.get("title_auto") is True
    assert child.metadata["title"] == "Plan the trip (fork)"
    assert child.metadata.get("title_auto") is None


def test_generated_title_is_declined_when_it_is_past_the_storage_ceiling(tmp_path: Path):
    mgr = SessionManager(tmp_path)
    session = _seed(mgr, "cli:title6", ("user", "some opening message"))

    assert session.set_generated_title("y" * 201) is False
    assert session.metadata["title"] == "some opening message"


def _saved_session(mgr: SessionManager, key: str, text: str = "hi") -> Session:
    session = mgr.get_or_create(key)
    session.add_message("user", text)
    mgr.save(session)
    return session


def _crowd_out(mgr: SessionManager) -> None:
    gc.collect()
    _saved_session(mgr, "tui:other")
    _saved_session(mgr, "tui:third")


def test_clean_sessions_beyond_the_limit_leave_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 2)
    mgr = SessionManager(tmp_path)
    for n in range(4):
        _saved_session(mgr, f"tui:c{n}")
    assert list(mgr._cache) == ["tui:c2", "tui:c3"]


def test_a_session_with_unsaved_messages_is_never_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    mgr.get_or_create("tui:pending").add_message("user", "not saved yet")
    _crowd_out(mgr)
    assert [m["content"] for m in mgr.get_or_create("tui:pending").messages] == ["not saved yet"]


def test_a_session_cut_since_its_last_save_is_never_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    session = _seed(mgr, "tui:cut-unsaved", ("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2"))
    session.undo_last_turn()
    _fail_to_rewrite(monkeypatch, mgr, session)
    session.add_message("user", "q2")
    session.add_message("assistant", "a2, retried")
    del session
    _crowd_out(mgr)
    assert [m["content"] for m in mgr.get_or_create("tui:cut-unsaved").messages] == ["q1", "a1", "q2", "a2, retried"]


def test_metadata_changed_in_memory_keeps_its_session_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    _saved_session(mgr, "tui:meta")
    mgr.get_or_create("tui:meta").metadata["workdir"] = "/somewhere"
    _crowd_out(mgr)
    assert mgr.get_or_create("tui:meta").metadata["workdir"] == "/somewhere"


def test_a_nested_metadata_change_counts_as_unsaved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:nested")
    session.metadata["harness"] = {"name": "a"}
    session.add_message("user", "x")
    mgr.save(session)
    session.metadata["harness"]["name"] = "b"
    del session
    _crowd_out(mgr)
    assert mgr.get_or_create("tui:nested").metadata["harness"]["name"] == "b"


def test_a_pending_clarification_set_in_memory_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    _saved_session(mgr, "tui:ask")
    mgr.get_or_create("tui:ask").pending_clarification = {"question": "which one?"}
    _crowd_out(mgr)
    assert mgr.get_or_create("tui:ask").pending_clarification == {"question": "which one?"}


def test_a_held_session_comes_back_as_the_same_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    held = _saved_session(mgr, "tui:held")
    _crowd_out(mgr)
    assert "tui:held" not in mgr._cache
    assert mgr.peek("tui:held") is held
    assert mgr.get_or_create("tui:held") is held


def test_a_released_session_reloads_from_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    _saved_session(mgr, "tui:gone", "kept on disk")
    _crowd_out(mgr)
    gc.collect()
    assert "tui:gone" not in mgr._held
    assert [m["content"] for m in mgr.get_or_create("tui:gone").messages] == ["kept on disk"]


def test_invalidate_forgets_a_held_session_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    held = _saved_session(mgr, "tui:inv")
    _crowd_out(mgr)
    mgr.invalidate("tui:inv")
    assert mgr.get_or_create("tui:inv") is not held


def test_a_metadata_patch_does_not_clear_unsaved_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    _saved_session(mgr, "tui:patched")
    mgr.get_or_create("tui:patched").metadata["workdir"] = "/unsaved"
    assert mgr.append_metadata_patch("tui:patched", {"pinned": True})
    _crowd_out(mgr)
    kept = mgr.get_or_create("tui:patched")
    assert kept.metadata["workdir"] == "/unsaved"
    assert kept.metadata["pinned"] is True


def test_a_patched_clean_session_can_still_leave(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    _saved_session(mgr, "tui:clean")
    assert mgr.append_metadata_patch("tui:clean", {"pinned": True})
    _crowd_out(mgr)
    assert "tui:clean" not in mgr._cache
    assert mgr.get_or_create("tui:clean").metadata["pinned"] is True


def test_a_patch_counts_its_keys_as_saved_when_the_caller_set_them_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    session = _saved_session(mgr, "tui:set-first")
    session.metadata["model"] = "m1"
    mgr.save(session)
    # The order most writers here use: their own copy first, then the record.
    session.metadata["model"] = "m2"
    del session
    assert mgr.append_metadata_patch("tui:set-first", {"model": "m2"})
    _crowd_out(mgr)
    assert "tui:set-first" not in mgr._cache
    assert mgr.get_or_create("tui:set-first").metadata["model"] == "m2"


def test_a_patch_after_its_keys_were_set_keeps_other_unsaved_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    session = _saved_session(mgr, "tui:set-both")
    session.metadata["model"] = "m2"
    session.metadata["workdir"] = "/unsaved"
    del session
    assert mgr.append_metadata_patch("tui:set-both", {"model": "m2"})
    _crowd_out(mgr)
    assert mgr.get_or_create("tui:set-both").metadata["workdir"] == "/unsaved"


@pytest.mark.parametrize(
    "field,value",
    [("last_consolidated", 1), ("pending_clarification", {"question": "which one?"})],
    ids=["consolidation-mark", "pending-clarification"],
)
def test_a_patch_after_its_keys_were_set_keeps_unsaved_state_outside_the_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    session = _saved_session(mgr, "tui:set-rest")
    setattr(session, field, value)
    session.metadata["model"] = "m2"
    del session
    assert mgr.append_metadata_patch("tui:set-rest", {"model": "m2"})
    _crowd_out(mgr)
    assert getattr(mgr.get_or_create("tui:set-rest"), field) == value


def test_an_unknown_key_read_through_get_or_create_can_leave(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path, project_dir=tmp_path)
    assert mgr.get_or_create("tui:read-only").metadata.get("workdir") is None
    _crowd_out(mgr)
    assert "tui:read-only" not in mgr._cache


def test_a_session_used_again_outlives_one_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 2)
    mgr = SessionManager(tmp_path)
    _saved_session(mgr, "tui:a")
    _saved_session(mgr, "tui:b")
    mgr.get_or_create("tui:a")
    _saved_session(mgr, "tui:c")
    assert list(mgr._cache) == ["tui:a", "tui:c"]


def test_flush_saves_a_held_session_that_left_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    held = _saved_session(mgr, "tui:flushed")
    _crowd_out(mgr)
    assert "tui:flushed" not in mgr._cache
    held.add_message("assistant", "answered after it left")
    assert mgr.flush("tui:flushed") is True
    on_disk = SessionManager(tmp_path).peek("tui:flushed")
    assert [m["content"] for m in on_disk.messages] == ["hi", "answered after it left"]


def test_a_metadata_patch_reaches_a_held_session_that_left_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    held = _saved_session(mgr, "tui:heldpatch")
    _crowd_out(mgr)
    assert mgr.append_metadata_patch("tui:heldpatch", {"pinned": True})
    assert held.metadata["pinned"] is True


def test_a_session_whose_state_cannot_be_fingerprinted_is_still_saved_and_never_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    odd = mgr.get_or_create("tui:odd")
    # Keys of two types: a save writes this, but sort_keys cannot order it.
    odd.metadata["k"] = {1: "a", "b": 2}
    odd.add_message("user", "kept")
    mgr.save(odd)
    assert mgr.append_metadata_patch("tui:odd", {"pinned": True})
    _crowd_out(mgr)
    assert "tui:odd" in mgr._cache


def test_a_value_no_save_could_write_does_not_stall_the_rest_of_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    looped = _saved_session(mgr, "tui:looped")
    # A value that refers to itself: json.dumps refuses it with ValueError, not TypeError.
    looped.metadata["self"] = looped.metadata
    _crowd_out(mgr)
    assert "tui:looped" in mgr._cache


def test_the_session_just_handed_out_is_never_the_one_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    mgr.get_or_create("tui:pending").add_message("user", "not saved yet")
    # Written through a reference nobody keeps, as most metadata writers here do.
    mgr.get_or_create("tui:fresh").metadata["workdir"] = "/unsaved"
    gc.collect()
    assert mgr.get_or_create("tui:fresh").metadata["workdir"] == "/unsaved"


def test_a_fork_child_comes_back_as_the_same_object(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    _saved_session(mgr, "tui:src")
    child = mgr.fork("tui:src")
    assert child is not None
    assert mgr.get_or_create(child.key) is child


def test_a_second_get_does_not_bless_unsaved_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    _saved_session(mgr, "tui:again")
    mgr.get_or_create("tui:again").metadata["workdir"] = "/unsaved"
    mgr.get_or_create("tui:again")
    _crowd_out(mgr)
    assert mgr.get_or_create("tui:again").metadata["workdir"] == "/unsaved"


def test_a_consolidation_mark_set_in_memory_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:consolidated")
    session.add_message("user", "a")
    session.add_message("assistant", "b")
    mgr.save(session)
    session.last_consolidated = 1
    del session
    _crowd_out(mgr)
    assert mgr.get_or_create("tui:consolidated").last_consolidated == 1


def _rewrite_saved_time(path: Path, value: object) -> None:
    """Put ``value`` where the file's metadata record keeps when the session was last updated."""
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["updated_at"] = value
    lines[0] = json.dumps(record, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a_session_reloaded_after_eviction_keeps_when_it_was_last_updated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_mod, "_CACHE_LIMIT", 1)
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:stamped")
    session.add_message("user", "hi")
    # After the message, which stamps the session with the time it was added.
    session.updated_at = datetime(2026, 1, 2, 3, 4, 5)
    mgr.save(session)
    del session
    _crowd_out(mgr)
    gc.collect()
    assert "tui:stamped" not in mgr._cache
    assert "tui:stamped" not in mgr._held
    assert mgr.get_or_create("tui:stamped").updated_at == datetime(2026, 1, 2, 3, 4, 5)


@pytest.mark.parametrize(
    "saved",
    ["not a time", None, 20260102, "9999-12-31T23:59:59-05:00", "0001-01-01T00:00:00+05:00"],
    ids=["text", "null", "number", "past-the-last-day", "before-the-first-day"],
)
def test_a_saved_time_that_does_not_parse_leaves_the_session_loadable(tmp_path: Path, saved: object) -> None:
    mgr = SessionManager(tmp_path)
    _saved_session(mgr, "tui:garbled", "kept")
    _rewrite_saved_time(mgr.session_path("tui:garbled"), saved)
    loaded = SessionManager(tmp_path).peek("tui:garbled")
    assert loaded is not None
    assert [m["content"] for m in loaded.messages] == ["kept"]
    assert abs(datetime.now() - loaded.updated_at) < timedelta(minutes=1)


def test_a_saved_time_windows_cannot_move_to_local_time_leaves_the_session_loadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class WindowsDatetime(datetime):
        # Windows refuses local time before 1970 with EINVAL where Linux converts it.
        def astimezone(self, tz=None):
            raise OSError(22, "Invalid argument")

    mgr = SessionManager(tmp_path)
    _saved_session(mgr, "tui:early", "kept")
    _rewrite_saved_time(mgr.session_path("tui:early"), "1600-01-01T00:00:00+00:00")
    monkeypatch.setattr(manager_mod, "datetime", WindowsDatetime)
    loaded = SessionManager(tmp_path).peek("tui:early")
    assert loaded is not None
    assert [m["content"] for m in loaded.messages] == ["kept"]


@pytest.mark.parametrize(
    "offset",
    [timezone.utc, timezone(timedelta(hours=5, minutes=45))],
    ids=["utc", "five-forty-five-east"],
)
def test_a_saved_time_with_an_offset_is_read_as_local_time(tmp_path: Path, offset: timezone) -> None:
    saved = datetime(2026, 1, 2, 3, 4, 5, tzinfo=offset)
    mgr = SessionManager(tmp_path)
    _saved_session(mgr, "tui:offset")
    _rewrite_saved_time(mgr.session_path("tui:offset"), saved.isoformat())
    loaded = SessionManager(tmp_path).peek("tui:offset")
    assert loaded is not None
    assert loaded.updated_at.tzinfo is None
    assert loaded.updated_at == saved.astimezone().replace(tzinfo=None)


# ---- turn markers that index into the messages ------------------------------

_TURN_MARKERS = ("output_limit_turn_at", "budget_stop_turn_at")


def _session_with_two_turns_the_last_one_cut():
    from raven.session.manager import Session

    session = Session(key="tui:markers")
    for role, text in (("user", "a"), ("assistant", "b"), ("user", "c"), ("assistant", "partial")):
        session.add_message(role, text)
    for key in _TURN_MARKERS:
        session.metadata[key] = 2
    return session


def test_clearing_a_session_forgets_the_turn_markers():
    """A marker says "the turn starting at index k was cut". After ``/new`` the
    index starts again at 0, and a command that writes no marker of its own
    (``/help``) begins at 0 <= k -- so ACP reported it cut."""
    session = _session_with_two_turns_the_last_one_cut()

    session.clear()

    assert [session.metadata[key] for key in _TURN_MARKERS] == [None, None]


def test_undoing_the_cut_turn_forgets_its_markers_and_keeps_an_earlier_ones():
    session = _session_with_two_turns_the_last_one_cut()

    session.undo_last_turn()

    assert [session.metadata[key] for key in _TURN_MARKERS] == [None, None]

    earlier = _session_with_two_turns_the_last_one_cut()
    for key in _TURN_MARKERS:
        earlier.metadata[key] = 0
    earlier.undo_last_turn()
    assert [earlier.metadata[key] for key in _TURN_MARKERS] == [0, 0], "the turn it marks is still there"
