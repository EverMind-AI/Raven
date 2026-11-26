"""Tests for SessionManager."""

import gc
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import raven.session.manager as manager_mod
from raven.session.manager import (
    Session,
    SessionManager,
    SessionWriterConflictError,
    new_chat_id,
    read_session_file,
)
from raven.session.title import TITLE_STORAGE_MAX


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

    A housekeeping pass can patch the record while a terminal holds the copy
    open. The saved record used to be this copy's metadata whole, so a flag the
    patch wrote after this copy was loaded -- archiving, most visibly -- was
    gone the next time anything here saved, and the conversation came back.
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


def test_a_patch_waits_out_a_record_that_lands_while_it_reads(tmp_path: Path):
    """A patch merges into the record the file holds when it writes, not when it started.

    The scan that finds the merge basis and the append that lands the merged
    record must sit in the same write transaction the saves hold. Without it,
    a record appended in between -- another process's ordinary turn, carrying
    its log_generation and last_consolidated -- was followed by this patch's
    merged copy of the older record, and the last record on disk lost the
    newer writer's fields.
    """
    from raven.utils.atomic_io import locked_append, write_transaction

    key = "tui:20260610_100000_landed"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(key)
    session.add_message("user", "hello")
    mgr.save(session)
    path = mgr.session_path(key)

    newer = {
        "_type": "metadata",
        "key": key,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "metadata": {"title": "second writer"},
        "last_consolidated": 7,
        "log_id": 2,
        "log_generation": 2,
        "log_started_at": None,
        "pending_clarification": None,
    }
    payload = json.dumps(newer, ensure_ascii=False)
    held = threading.Event()

    def other_writer() -> None:
        with write_transaction(path):
            held.set()
            time.sleep(0.2)
            locked_append(path, [payload])

    lander = threading.Thread(target=other_writer)
    lander.start()
    assert held.wait(5.0)
    assert mgr.append_metadata_patch(key, {"pinned": True}) is True
    lander.join()

    last_row: dict = {}
    for text in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(text)
        if row.get("_type") == "metadata":
            last_row = row
    assert last_row["log_generation"] == 2
    assert last_row["metadata"]["title"] == "second writer"
    assert last_row["metadata"]["pinned"] is True


def test_a_save_does_not_resurrect_a_key_this_copy_cleared(tmp_path: Path):
    """The metadata fold keeps what it did not write, which makes clearing explicit.

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

    # A metadata patch touches the record, so the next save has to fold it in.
    SessionManager(tmp_path).append_metadata_patch(key, {"archived": True})

    session.metadata["pinned"] = False
    session.add_message("user", "unpinned now")
    holder.save(session)

    stored = _last_metadata(holder.session_path(key))
    assert stored.get("pinned") is False
    assert stored.get("archived") is True


def test_a_save_over_a_transcript_with_no_record_writes_its_own(tmp_path: Path):
    """Nothing to fold under is not an error; this copy's metadata stands.

    A transcript can lose its metadata record -- a crashed writer, a truncated
    file -- and there is nothing on disk to read. Refusing to save then would
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
    """A metadata patch costs a scan, and an undisturbed conversation does not pay it.

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


def test_a_transcript_saved_with_a_byte_order_mark_is_still_listed(tmp_path: Path):
    """A Windows editor that saves UTF-8 with a byte order mark puts it in front of the metadata line.
    Read as plain UTF-8, that line did not parse and the whole session left the list."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:bom01")
    session.add_message("user", "hello")
    mgr.save(session)
    path = tmp_path / "sessions" / "tui" / "bom01.jsonl"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    rows = [info for info in SessionManager(tmp_path).list_sessions() if info["key"] == "tui:bom01"]

    assert len(rows) == 1
    assert rows[0]["first_user_message"] == "hello"


def test_a_transcript_saved_with_a_byte_order_mark_opens_with_what_the_list_shows(tmp_path: Path):
    """Opening reads the metadata line the list reads. Read as plain UTF-8 it did not parse: the session
    opened without its title and archive flag, the next save wrote over both, and the file claimed no key."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:bom02")
    session.add_message("user", "hello")
    session.metadata.update(title="My Title", archived=True)
    mgr.save(session)
    path = tmp_path / "sessions" / "tui" / "bom02.jsonl"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    assert manager_mod._stored_key(path) == "tui:bom02"
    fresh = SessionManager(tmp_path)
    opened = fresh.get_or_create("tui:bom02")
    assert opened.metadata.get("title") == "My Title"
    assert opened.metadata.get("archived") is True
    opened.add_message("user", "again")
    fresh.save(opened)

    rows = [info for info in SessionManager(tmp_path).list_sessions() if info["key"] == "tui:bom02"]
    assert rows[0]["metadata"].get("title") == "My Title"
    assert rows[0]["metadata"].get("archived") is True


def _in_a_legacy_code_page(path: Path) -> None:
    path.write_bytes(path.read_text(encoding="utf-8").replace("\\u4f60\\u597d", "\u4f60\u597d").encode("cp936"))


def test_a_transcript_in_a_legacy_code_page_is_left_out_without_breaking_the_list(tmp_path: Path):
    """A transcript saved in a Windows ANSI code page is not UTF-8. It is left out as an unreadable file
    is, rather than its decoding error escaping the whole list."""
    mgr = SessionManager(tmp_path)
    for key in ("tui:good01", "tui:cp01"):
        session = mgr.get_or_create(key)
        session.add_message("user", "\u4f60\u597d")
        mgr.save(session)
    _in_a_legacy_code_page(tmp_path / "sessions" / "tui" / "cp01.jsonl")

    keys = [info["key"] for info in SessionManager(tmp_path).list_sessions()]

    assert keys == ["tui:good01"]
    assert SessionManager(tmp_path).find_most_recent_chat_id("tui") == "good01"


def test_a_turn_on_a_transcript_in_a_legacy_code_page_fails_without_writing(tmp_path: Path):
    """Leaving such a transcript out of the list must not reach a write: UTF-8 lines appended to a file
    no reader can open lose the turn without a word, where failing the save reports it."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:cp02")
    session.add_message("user", "\u4f60\u597d")
    mgr.save(session)
    path = tmp_path / "sessions" / "tui" / "cp02.jsonl"
    _in_a_legacy_code_page(path)
    before = path.read_bytes()

    fresh = SessionManager(tmp_path)
    opened = fresh.get_or_create("tui:cp02")
    opened.add_message("user", "again")
    with pytest.raises(UnicodeDecodeError):
        fresh.save(opened)
    with pytest.raises(UnicodeDecodeError):
        fresh.append_metadata_patch("tui:cp02", {"archived": True})

    assert path.read_bytes() == before


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


def test_a_clear_replaces_the_file_whole(tmp_path: Path):
    """A clear mints a new log: the old file is replaced, not appended to."""
    key = "tui:20260610_100000_clearwhole"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(key)
    session.add_message("user", "old")
    mgr.save(session)

    session.clear()
    mgr.save(session)

    loaded = read_session_file(mgr.session_path(key), key)
    assert loaded is not None
    assert loaded.messages == []


def test_a_second_writer_is_refused_not_merged(tmp_path: Path):
    """One transcript, one writer: a copy whose file moved on must fail loudly."""
    key = "tui:20260610_100000_secondwriter"
    first = SessionManager(tmp_path)
    session = first.get_or_create(key)
    session.add_message("user", "from the first terminal")
    first.save(session)

    second = SessionManager(tmp_path)
    other = second.get_or_create(key)
    other.add_message("user", "from the second terminal")
    second.save(other)

    session.add_message("assistant", "the first terminal's answer")
    with pytest.raises(SessionWriterConflictError, match="another process is writing this session"):
        first.save(session)

    reloaded = SessionManager(tmp_path).get_or_create(key)
    assert [m["content"] for m in reloaded.messages] == ["from the first terminal", "from the second terminal"]


def test_a_refused_save_hands_the_next_load_the_file_not_the_stale_copy(tmp_path: Path):
    """The conflict says the copy is stale, so something has to make that true.

    The manager keeps the copy in memory, and every later save of it is refused
    for as long as it is there. Dropping it on the raise is what makes the next
    ``get_or_create`` read the transcript the other writer left -- and lets this
    process use the session again instead of until a restart.
    """
    key = "tui:20260610_100000_stalecopy"
    first = SessionManager(tmp_path)
    session = first.get_or_create(key)
    session.add_message("user", "from the first terminal")
    first.save(session)

    second = SessionManager(tmp_path)
    other = second.get_or_create(key)
    other.add_message("user", "from the second terminal")
    second.save(other)

    session.add_message("assistant", "the first terminal's answer")
    with pytest.raises(SessionWriterConflictError, match="another process is writing this session"):
        first.save(session)

    reloaded = first.get_or_create(key)
    assert [m["content"] for m in reloaded.messages] == ["from the first terminal", "from the second terminal"], (
        "the next load served the copy the conflict just refused"
    )
    reloaded.add_message("assistant", "the first terminal's answer")
    first.save(reloaded)

    on_disk = read_session_file(first.session_path(key), key)
    assert [m["content"] for m in on_disk.messages] == [
        "from the first terminal",
        "from the second terminal",
        "the first terminal's answer",
    ], "the reloaded copy could not save the session the conflict released"


def _a_same_length_rewrite(tmp_path: Path, key: str) -> tuple[SessionManager, Session, Path, tuple]:
    """Let a second writer replace one turn with another of exactly the same length.

    The file keeps its size and its message count; the revision moves, because the
    second copy undid its turn before appending the replacement.
    """
    first = SessionManager(tmp_path)
    session = first.get_or_create(key)
    session.add_message("user", "q1")
    session.add_message("assistant", "a1")
    first.save(session)
    path = first.session_path(key)
    before = session._file_stamp
    assert before is not None

    second = SessionManager(tmp_path)
    other = second.get_or_create(key)
    assert other.undo_last_turn() == 2
    other.add_message("user", "q2")
    other.add_message("assistant", "a2")
    second.save(other)

    assert path.stat().st_size == before[0], "the fixture no longer rewrites the same number of bytes"
    assert other.log_generation == session.log_generation + 1, "the fixture no longer moves the revision"
    return first, session, path, before


def test_a_same_length_rewrite_in_the_same_clock_tick_is_refused(tmp_path: Path):
    """Size and clock cannot tell two rewrites of one length apart.

    ``st_mtime_ns`` counts in the filesystem's ticks (about a millisecond here),
    so a rewrite of the same length lands on the reading the first copy already
    holds. Left at that, the copy appends over the other writer; the tail digest
    in the stamp is what still sees the file move.
    """
    first, session, path, before = _a_same_length_rewrite(tmp_path, "tui:20260610_100000_samesize")
    os.utime(path, ns=(before[1], before[1]))

    session.add_message("assistant", "the first terminal's answer")
    with pytest.raises(SessionWriterConflictError, match="another process is writing this session"):
        first.save(session)


def test_a_rewrite_that_keeps_the_size_and_the_count_is_refused_when_the_log_moved(tmp_path: Path):
    """The other half of the same judgement: the rows are not the ones this copy wrote.

    A same-length replacement keeps the message count, so counting the rows is not
    enough to call the file unchanged -- the revision it records is.
    """
    first, session, path, before = _a_same_length_rewrite(tmp_path, "tui:20260610_100000_logmoved")
    os.utime(path, ns=(before[1] + 1_000_000_000, before[1] + 1_000_000_000))

    session.add_message("assistant", "the first terminal's answer")
    with pytest.raises(SessionWriterConflictError, match="another process is writing this session"):
        first.save(session)


def _a_saved_turn(tmp_path: Path, key: str) -> tuple[SessionManager, Session, Path]:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create(key)
    session.add_message("user", "q")
    session.add_message("assistant", "a")
    manager.save(session)
    return manager, session, manager.session_path(key)


def test_a_turn_meta_leaves_another_clients_metadata_record_for_the_next_save(tmp_path: Path):
    """A late stamp filed after another client's metadata patch must not hide that patch.

    The patch is the one record a second process may add without being a second
    writer, and the next save folds it in because the file no longer matches this
    copy's stamp. A ``turn_meta`` appended in between used to move the stamp past
    the patch, so the closing save took the file for unchanged and wrote this
    copy's metadata over the archive flag.
    """
    from raven.session.manager import append_turn_meta

    key = "tui:20260610_100000_lateflag"
    manager, session, path = _a_saved_turn(tmp_path, key)

    assert SessionManager(tmp_path).append_metadata_patch(key, {"archived": True})
    append_turn_meta(path, [{"row_id": "r1", "keys": {"duration_ms": 3}}], session=session)
    session.add_message("user", "next")
    manager.save(session)

    assert read_session_file(path, key).metadata.get("archived") is True, (
        "the save after the late stamp wrote over another client's archive flag"
    )


def test_a_metadata_patch_leaves_the_stamp_behind_another_clients_record(tmp_path: Path):
    """A patch made here does not settle the cached copy's stamp over the file.

    The record it appends merges the last one on disk, which can be another
    client's that this copy has not folded yet. Moved past both, the stamp would
    let the next save write this copy's metadata over that client's keys.
    """
    key = "tui:20260610_100000_twopatches"
    manager, session, path = _a_saved_turn(tmp_path, key)

    assert SessionManager(tmp_path).append_metadata_patch(key, {"pinned": True})
    assert manager.append_metadata_patch(key, {"archived": True})
    session.add_message("user", "next")
    manager.save(session)

    metadata = read_session_file(path, key).metadata
    assert (metadata.get("pinned"), metadata.get("archived")) == (True, True), (
        "the save after a local patch wrote over the pin another client set"
    )


def test_a_turn_meta_is_not_filed_over_rows_another_writer_added(tmp_path: Path):
    """A late stamp is not filed into a transcript a second writer has moved.

    Filing it would also be the first write this copy makes past the other
    writer's rows. Refused here, the file is untouched and the stamp stays where
    it was, so the save that follows still sees the conflict and raises.
    """
    from raven.session.manager import append_turn_meta

    key = "tui:20260610_100000_lateover"
    manager, session, path = _a_saved_turn(tmp_path, key)
    other = SessionManager(tmp_path)
    theirs = other.get_or_create(key)
    theirs.add_message("user", "from the second terminal")
    other.save(theirs)
    before, stamp = path.read_bytes(), session._file_stamp

    append_turn_meta(path, [{"row_id": "r1", "keys": {"duration_ms": 3}}], session=session)

    assert path.read_bytes() == before, "a late stamp was filed over another writer's rows"
    assert session._file_stamp == stamp, "the stamp moved past rows this copy never read"
    session.add_message("assistant", "the first terminal's answer")
    with pytest.raises(SessionWriterConflictError):
        manager.save(session)


def test_a_cut_that_could_not_be_written_leaves_the_copy_as_it_was(tmp_path: Path, monkeypatch):
    """A failed cut changes nothing: the caller is told False and keeps the copy it had.

    The rerun reads False as "do not run again", so the in-memory list must still
    hold the rows the file holds. A copy left cut would save a rewrite that drops
    them on its next save, which is the cut the caller just declined to make.
    """
    key = "tui:20260610_100000_cutfails"
    manager = SessionManager(tmp_path)
    session = manager.get_or_create(key)
    for row_id, role, content in [("r1", "user", "q"), ("r2", "assistant", "attempt"), ("r3", "tool", "result")]:
        session.record({"role": role, "content": content, "_row_id": row_id})
    manager.save(session)
    rows, truncations, persisted = session.messages, session.truncations, session._persisted_count

    def _refuse(_session):
        raise OSError("disk full")

    monkeypatch.setattr(manager, "save", _refuse)
    assert manager.drop_filed_rows(session, {"r2", "r3"}) is False

    assert session.messages is rows, "the failed cut left the copy cut"
    assert [m["content"] for m in session.messages] == ["q", "attempt", "result"]
    assert (session.truncations, session._persisted_count) == (truncations, persisted)


def test_a_cut_takes_the_row_it_names_from_the_middle_of_the_transcript(tmp_path: Path):
    """The cut is by identifier: the named row goes and its neighbours stay.

    Every other cut test names rows at the tail, where taking the last rows and
    taking the named ones agree. A row between two kept ones is where they part.
    """
    key = "tui:20260610_100000_cutmiddle"
    manager = SessionManager(tmp_path)
    session = manager.get_or_create(key)
    for row_id, role, content in [("r1", "user", "q"), ("r2", "assistant", "dropped"), ("r3", "assistant", "kept")]:
        session.record({"role": role, "content": content, "_row_id": row_id})
    manager.save(session)

    assert manager.drop_filed_rows(session, {"r2"}) is True

    assert [m["content"] for m in session.messages] == ["q", "kept"]
    assert [m["content"] for m in read_session_file(manager.session_path(key), key).messages] == ["q", "kept"]


def test_a_new_record_writer_cannot_hide_another_clients_metadata_patch(tmp_path: Path):
    """A writer of a record kind that does not exist yet gets the stamp rule for free.

    It writes inside ``_writing_as`` and states nothing about what the file held:
    the comparison with the copy's own stamp decides. After another client's
    patch the stamp stays behind it, and the next save folds the patch in.
    """
    from raven.session.manager import _writing_as
    from raven.utils.atomic_io import locked_append

    key = "tui:20260610_100000_newrecord"
    manager, session, path = _a_saved_turn(tmp_path, key)
    stamp = session._file_stamp
    assert SessionManager(tmp_path).append_metadata_patch(key, {"archived": True})

    with _writing_as(session, path):
        locked_append(path, [json.dumps({"_type": "future_record", "note": "x"})])

    assert session._file_stamp == stamp, "the stamp moved past another client's record"
    session.add_message("user", "next")
    manager.save(session)
    assert read_session_file(path, key).metadata.get("archived") is True


def test_a_stamp_cannot_be_taken_past_a_file_the_copy_did_not_read(tmp_path: Path):
    """Saying the copy took the file in is checked, not trusted.

    ``_took_in`` is the one statement a writer makes about what the file held. Made
    without the read behind it, or past rows another writer added, it raises and
    the stamp stays where it was.
    """
    from raven.session.manager import SessionManager as Manager
    from raven.session.manager import _took_in, _writing_as

    key = "tui:20260610_100000_tookin"
    _manager, session, path = _a_saved_turn(tmp_path, key)
    other = SessionManager(tmp_path)
    theirs = other.get_or_create(key)
    theirs.add_message("user", "from the second terminal")
    other.save(theirs)
    stamp = session._file_stamp

    with pytest.raises(RuntimeError, match="has not read and taken in"):
        with _writing_as(session, path) as before:
            _took_in(session, before, None)
    last, count, *_rest = Manager._scan_file(path)
    with pytest.raises(RuntimeError, match="has not read and taken in"):
        with _writing_as(session, path) as before:
            _took_in(session, before, (last, count))

    assert session._file_stamp == stamp


def test_a_save_that_folded_another_clients_record_does_not_read_the_file_again(tmp_path: Path, monkeypatch):
    """Folding a metadata record is taking the file in, so the stamp moves past it.

    ``save`` says so through ``_took_in``. Without it the stamp stays behind that
    record for good, and every later save scans the whole transcript again.
    """
    key = "tui:20260610_100000_foldonce"
    manager, session, _path = _a_saved_turn(tmp_path, key)
    assert SessionManager(tmp_path).append_metadata_patch(key, {"archived": True})
    session.add_message("user", "one")
    manager.save(session)

    scans: list[Path] = []
    scan = SessionManager._scan_file
    monkeypatch.setattr(SessionManager, "_scan_file", staticmethod(lambda path: scans.append(path) or scan(path)))
    session.add_message("user", "two")
    manager.save(session)

    assert scans == [], "a save re-read a file this copy had already taken in"


def test_only_the_writing_helpers_move_a_file_stamp():
    """Every assignment to ``_file_stamp`` in the package sits in the three places that own it.

    The stamp is what a save trusts to skip the re-read, and a write path that set
    it from its own reasoning hid another client's metadata patch once. Loading
    sets the first stamp; ``_writing_as`` moves it on evidence; ``_took_in`` takes
    in a file the caller checked. A fourth place is a new owner and has to be
    argued for here.
    """
    import ast

    owners = set()
    for path in sorted(Path(manager_mod.__file__).parents[1].rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "_file_stamp" not in source:
            continue
        tree = ast.parse(source)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
                if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    continue
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "_file_stamp":
                        owners.add(f"{path.name}:{func.name}")

    assert owners == {"manager.py:read_session_file", "manager.py:_writing_as", "manager.py:_took_in"}, owners


def test_a_refused_copy_saving_again_leaves_the_reloaded_copy_cached(tmp_path: Path):
    """Dropping the refused copy must not drop the copy that replaced it.

    A turn whose cut was refused keeps its stale copy and reaches its closing
    save, which is refused again. By then something else may have loaded the
    transcript afresh; dropping the cache entry by key evicted that copy, the
    next load made a second one beside it, and the two refused each other.
    """
    key = "tui:20260610_100000_twocopies"
    manager, stale, _path = _a_saved_turn(tmp_path, key)
    other = SessionManager(tmp_path)
    theirs = other.get_or_create(key)
    theirs.add_message("user", "from the second terminal")
    other.save(theirs)

    stale.add_message("assistant", "stale")
    with pytest.raises(SessionWriterConflictError):
        manager.save(stale)
    fresh = manager.get_or_create(key)
    with pytest.raises(SessionWriterConflictError):
        manager.save(stale)

    assert manager.get_or_create(key) is fresh, "the refused copy's second save evicted the copy that replaced it"
    fresh.add_message("assistant", "answer")
    manager.save(fresh)


def test_flush_reports_a_transcript_another_process_wrote(tmp_path: Path):
    """Like every other save, a flush that loses the transcript says so."""
    key = "tui:20260610_100000_flushlost"
    manager, session, _path = _a_saved_turn(tmp_path, key)
    other = SessionManager(tmp_path)
    theirs = other.get_or_create(key)
    theirs.add_message("user", "from the second terminal")
    other.save(theirs)

    session.add_message("assistant", "unsaved")
    with pytest.raises(SessionWriterConflictError):
        manager.flush(key)


def test_an_undo_moves_the_append_boundary_and_keeps_the_next_turn(tmp_path: Path):
    """Undo cuts the list; the append boundary has to follow it.

    Left where it was, messages recorded after the undo would land behind a
    boundary that still calls them persisted and an append would skip them.
    """
    key = "tui:20260610_100000_undoboundary"
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create(key)
    for role, content in (("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")):
        session.add_message(role, content)
    mgr.save(session)
    assert session._persisted_count == 4

    assert session.undo_last_turn() == 2
    assert session._persisted_count == 2, "the append boundary stayed past rows the undo removed"

    session.add_message("user", "q3")
    session.add_message("assistant", "a3")
    assert len(session.messages) - session._persisted_count == 2, "the new turn looks persisted already"
    mgr.save(session)

    reloaded = SessionManager(tmp_path).get_or_create(key)
    assert [m["content"] for m in reloaded.messages] == ["q1", "a1", "q3", "a3"]


def _seed_with_ids(mgr: SessionManager, key: str, *rows: tuple[str, str, str]) -> Session:
    session = mgr.get_or_create(key)
    for role, content, row_id in rows:
        session.record({"role": role, "content": content, "_row_id": row_id})
    mgr.save(session)
    return session


def test_drop_filed_rows_takes_rows_that_never_reached_the_file(tmp_path: Path):
    """Rows the attempt filed only in memory are still the attempt's to drop."""
    key = "cli:20260610_100000_dropstale"
    mgr = SessionManager(tmp_path)
    session = _seed_with_ids(mgr, key, ("user", "q", "r1"), ("assistant", "a", "r2"))
    session.record({"role": "user", "content": "only in memory", "_row_id": "gone"})

    assert mgr.drop_filed_rows(session, {"gone"}) is True

    assert [m["content"] for m in session.messages] == ["q", "a"]
    loaded = read_session_file(mgr.session_path(key), key)
    assert [m["content"] for m in loaded.messages] == ["q", "a"]


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


def test_an_observer_that_asks_where_the_session_lived_hears_it_from_before_the_unlink(tmp_path: Path):
    """Once the transcript is gone the store cannot find a session another process filed
    under its project's slug, so the directory is resolved before the unlink and handed to
    an observer that declares ``session_dir``; one that does not is called as before."""
    project = SessionManager(tmp_path, project_slug="proj")
    _saved(project, "tui:obs10")
    lived = project.session_dir("tui:obs10")
    gateway = SessionManager(tmp_path)
    heard: list[tuple[str, bool, Path]] = []

    class _Asks:
        def on_session_deleted(self, session_key: str, removed: bool, *, session_dir: Path) -> None:
            heard.append((session_key, removed, session_dir))

    old_style = _DeleteProbe()
    gateway.set_delete_observers((_Asks(), old_style))

    assert gateway.delete("tui:obs10") is True

    assert heard == [("tui:obs10", True, lived)]
    assert gateway.session_dir("tui:obs10") != lived, "asked now, the store has lost it"
    assert old_style.log == [("probe", "tui:obs10", True)]


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
    with pytest.raises(ValueError, match="imported session"):
        mgr.save(session)
    with pytest.raises(ValueError, match="imported session"):
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


def test_list_sessions_message_count_excludes_turn_meta_lines(tmp_path: Path):
    """A turn_meta record is a stamp carrier, not a message the reader saw."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:mc03")
    for role, text in (("user", "one"), ("assistant", "two"), ("user", "three")):
        session.add_message(role, text)
    mgr.save(session)
    with mgr.session_path("tui:mc03").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps({"_type": "turn_meta", "patches": [{"row_id": "x", "keys": {"replied_via_tool": True}}]}) + "\n"
        )

    entries = mgr.list_sessions()
    assert entries[0]["message_count"] == 3


def test_a_turn_meta_does_not_make_the_next_save_re_read_the_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The count a turn_meta used to inflate sent every later save into a re-read."""
    from raven.session.manager import append_turn_meta

    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("tui:tmcount")
    session.add_message("user", "hi")
    session.add_message("assistant", "hello")
    mgr.save(session)
    append_turn_meta(mgr.session_path("tui:tmcount"), [{"row_id": "nope", "keys": {"replied_via_tool": True}}])

    reads: list[Path] = []
    real = manager_mod.read_session_file
    monkeypatch.setattr(manager_mod, "read_session_file", lambda *a, **k: reads.append(a[0]) or real(*a, **k))

    session.add_message("assistant", "again")
    mgr.save(session)

    assert reads == [], "a turn_meta record sent the save into a re-read"


def test_a_silent_runtime_turn_is_taken_back_after_a_turn_the_message_tool_answered(tmp_path: Path):
    """A turn the message tool answered does not make the next turn's silence count.

    The scanner remembers that a turn delivered its reply, so that turn stays the
    activity the session is ordered by. A later turn the runtime opened and the model
    closed with silence delivered nothing, so it is taken back whole: the delivered
    reply remains the answer, and the silent turn after it does not take its place.
    """
    mgr = SessionManager(tmp_path)
    calls = [{"id": "m1", "type": "function", "function": {"name": "message", "arguments": "{}"}}]
    session = mgr.get_or_create("cron:job01")
    session.add_message("user", "anything new?", timestamp="2026-06-10T10:00:00")
    session.add_message("assistant", "still compiling", timestamp="2026-06-10T10:00:05")
    session.add_message("user", "[Scheduled Task] check the build", timestamp="2026-06-10T11:00:00", origin="cron")
    session.add_message("assistant", "", timestamp="2026-06-10T11:00:02", tool_calls=calls)
    session.add_message("tool", "Message sent", timestamp="2026-06-10T11:00:03", tool_call_id="m1", name="message")
    session.add_message("assistant", "[SILENT]", timestamp="2026-06-10T11:00:09", silent=True, replied_via_tool=True)
    session.add_message("user", "[Scheduled Task] check the build", timestamp="2026-06-10T12:00:00", origin="cron")
    session.add_message("assistant", "[SILENT]", timestamp="2026-06-10T12:00:05", silent=True)
    mgr.save(session)

    [row] = mgr.list_sessions()
    assert row["last_message_at"] == "2026-06-10T11:00:02"


def test_a_silent_runtime_turn_whose_message_call_answered_nobody_is_taken_back(tmp_path: Path):
    """A message call is not an answer by being made. The live turn counted it only when the send went
    out and filed that verdict on the closing row; without it, a call that failed leaves a Silent Turn,
    taken back whole."""
    mgr = SessionManager(tmp_path)
    calls = [{"id": "m1", "type": "function", "function": {"name": "message", "arguments": '{"content": "x"}'}}]
    session = mgr.get_or_create("cron:job02")
    session.add_message("user", "anything new?", timestamp="2026-06-10T10:00:00")
    session.add_message("assistant", "still compiling", timestamp="2026-06-10T10:00:05")
    session.add_message("user", "[Scheduled Task] check the build", timestamp="2026-06-10T11:00:00", origin="cron")
    session.add_message("assistant", "", timestamp="2026-06-10T11:00:02", tool_calls=calls)
    session.add_message(
        "tool",
        "Error sending message: the channel is down",
        timestamp="2026-06-10T11:00:03",
        tool_call_id="m1",
        name="message",
    )
    session.add_message("assistant", "[SILENT]", timestamp="2026-06-10T11:00:09", silent=True)
    mgr.save(session)

    [row] = mgr.list_sessions()
    assert (row["last_message_at"], row["last_message_preview"]) == ("2026-06-10T10:00:05", "still compiling")


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


def _seed_dangling_call(mgr: SessionManager, key: str) -> None:
    """A transcript whose last point is an assistant call with no result."""
    session = mgr.get_or_create(key)
    session.record({"role": "user", "content": "go"})
    session.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
        }
    )
    mgr.save(session)


def test_the_readers_repair_is_not_counted_as_a_file_row(tmp_path: Path):
    """A metadata patch is not a second writer because the repair lengthened memory.

    The reader inserts one synthetic result, so its message list is longer than
    the file. What a save compares against the file is the number of rows the
    file actually holds; counting the repair there would make every metadata
    patch look like a message append and refuse the save.
    """
    _seed_dangling_call(SessionManager(tmp_path), "cli:repaircount")
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("cli:repaircount")
    assert session._persisted_count == 3
    assert session._persisted_file_count == 2

    SessionManager(tmp_path).append_metadata_patch(session.key, {"archived": True})
    session.add_message("user", "next")
    mgr.save(session)

    reloaded = SessionManager(tmp_path).get_or_create(session.key)
    assert reloaded._persisted_file_count == 3
    assert [m["content"] for m in reloaded.messages if m.get("role") == "user"] == ["go", "next"]


def test_a_save_does_not_materialize_the_readers_interrupted_repair(tmp_path: Path):
    """The repair is derived on read, so a rewrite (an undo) must not file it.

    Filing it would put a tool result in the transcript that no tool produced,
    indistinguishable from one that really was interrupted.
    """
    _seed_dangling_call(SessionManager(tmp_path), "cli:repair")
    reader = SessionManager(tmp_path)
    session = reader.get_or_create("cli:repair")
    assert any(m.get("content") == "[interrupted] this call never returned" for m in session.messages)
    # The dangling call belongs to an earlier turn; cut a later one so the
    # rewrite keeps the call while dropping the turn that followed it.
    session.record({"role": "user", "content": "next"})
    session.record({"role": "assistant", "content": "later"})
    reader.save(session)
    reader = SessionManager(tmp_path)
    session = reader.get_or_create("cli:repair")
    assert session.undo_last_turn() == 2

    reader.save(session)

    path = reader.session_path("cli:repair")
    text = path.read_text(encoding="utf-8")
    assert "[interrupted]" not in text, "the reader's repair was written into the file"
    reloaded = SessionManager(tmp_path).get_or_create("cli:repair")
    assert any(m.get("content") == "[interrupted] this call never returned" for m in reloaded.messages), (
        "the repair is still derived on read"
    )


def test_a_fork_repairs_the_child_it_caches_without_writing_the_repair(tmp_path: Path):
    """The child served from this manager is provider-valid; its file says what happened.

    A fork filters the synthetic results a reader derives out of its inherited
    history, so a parent that ends mid-call leaves the child ending on an
    assistant ``tool_calls`` row with no result. The child is cached and served
    from that cache for its first turn, where a strict provider rejects it. The
    repair is made in memory; the save leaves it out of the file, which derives
    it again on read.
    """
    mgr = SessionManager(tmp_path)
    _seed_dangling_call(mgr, "cli:forkrepair")

    child = mgr.fork("cli:forkrepair")

    assert child is not None
    assert mgr.get_or_create(child.key) is child, "the fork is served from this manager's cache"
    repairs = [m for m in child.messages if m.get("role") == "tool"]
    assert len(repairs) == 1, f"the cached child is not provider-valid: {child.messages}"
    assert repairs[0].get(manager_mod.SYNTHETIC_INTERRUPTED_KEY) is True
    path = mgr.session_path(child.key)
    assert "[interrupted]" not in path.read_text(encoding="utf-8"), "the reader's repair was filed"


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


def test_fork_inherits_the_reasoning_effort_the_parent_picked(tmp_path: Path):
    """The pick rides the record beside the model, and a fork continues the
    conversation it was picked for -- so it continues at it, restart or not."""
    from raven.agent import session_effort

    mgr = SessionManager(tmp_path)
    src = _seed(mgr, "cli:src10", ("user", "a"))
    src.metadata["model"] = "anthropic/claude-opus-4-8"
    src.metadata[session_effort.METADATA_KEY] = "high"
    mgr.save(src)

    child = mgr.fork("cli:src10")

    reloaded = SessionManager(tmp_path).get_or_create(child.key)
    assert session_effort.of(reloaded.metadata) == "high"
    assert reloaded.metadata["model"] == "anthropic/claude-opus-4-8"


def test_fork_of_a_parent_following_the_default_carries_no_effort(tmp_path: Path):
    """A cleared pick is null on the record, not absent -- a metadata patch sets
    keys and never removes one -- and the fork starts without the key either way."""
    from raven.agent import session_effort

    mgr = SessionManager(tmp_path)
    src = _seed(mgr, "cli:src11", ("user", "a"))
    src.metadata[session_effort.METADATA_KEY] = None
    mgr.save(src)

    child = mgr.fork("cli:src11")

    assert session_effort.METADATA_KEY not in child.metadata


def test_the_fork_carries_the_key_the_effort_pick_is_kept_under():
    """The session layer spells the key rather than importing the agent's;
    this is what keeps the two spellings one."""
    from raven.agent import session_effort

    assert session_effort.METADATA_KEY in manager_mod.FORK_CARRIES


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


def test_list_sessions_scans_an_unchanged_transcript_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A poll that finds the same transcript does not read it again.

    The picker asks for the same files on every request; re-reading all of them
    each time is the cost this cache exists to remove.
    """
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:one")
    session.add_message("user", "hello")
    manager.save(session)

    scans: list[Path] = []
    real = SessionManager._scan_file  # a staticmethod: it takes the path only
    monkeypatch.setattr(manager, "_scan_file", lambda path: scans.append(path) or real(path))

    first = manager.list_sessions()
    second = manager.list_sessions()

    assert first == second
    assert len(scans) == 1


def test_list_sessions_rescans_a_transcript_that_grew(tmp_path: Path):
    """A transcript that changed is read again, so the listing is not stale."""
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:one")
    session.add_message("user", "first")
    manager.save(session)
    assert manager.list_sessions()[0]["message_count"] == 1

    grown = manager.get_or_create("cli:one")
    grown.add_message("assistant", "second")
    manager.save(grown)

    assert manager.list_sessions()[0]["message_count"] == 2


def test_list_sessions_skips_a_transcript_it_cannot_stat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A path that vanished between glob and stat is left out, not raised."""
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:one")
    session.add_message("user", "hello")
    manager.save(session)
    [path] = list((tmp_path / "sessions").rglob("*.jsonl"))

    real_stat = Path.stat

    def failing_stat(self: Path, *args, **kwargs):
        if self == path:
            raise OSError("gone between glob and stat")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_stat)

    assert manager.list_sessions() == []


def test_the_scan_cache_evicts_the_least_recently_used_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The cache is bounded; past the limit the entry scanned longest ago goes.

    The limit is lowered so the test walks the same eviction branch without
    writing a couple thousand transcripts.
    """
    monkeypatch.setattr(manager_mod, "_SCAN_CACHE_LIMIT", 2)
    manager = SessionManager(tmp_path)
    paths = []
    for name in ("one", "two", "three"):
        session = manager.get_or_create(f"cli:{name}")
        session.add_message("user", name)
        manager.save(session)
        paths.append(manager.session_path(f"cli:{name}"))

    for path in paths:
        assert manager._scan_listed(path)[0] is not None

    assert list(manager._scan_cache) == paths[1:]
    assert {entry["key"] for entry in manager.list_sessions()} == {"cli:one", "cli:two", "cli:three"}


def test_list_sessions_rescans_an_equal_size_rewrite_with_a_newer_mtime(tmp_path: Path):
    """The stamp carries mtime as well as size, so a same-size rewrite is seen."""
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:one")
    session.add_message("user", "hello")
    manager.save(session)
    [path] = list((tmp_path / "sessions").rglob("*.jsonl"))
    assert manager.list_sessions()[0]["last_message_preview"] == "hello"

    raw = path.read_bytes()
    rewritten = raw.replace(b"hello", b"world")
    assert len(rewritten) == len(raw), "the rewrite must not change the byte count"
    path.write_bytes(rewritten)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

    listed = manager.list_sessions()
    assert listed[0]["last_message_preview"] == "world"
    assert listed[0]["message_count"] == 1


def test_list_sessions_rescans_after_an_undecodable_transcript_becomes_readable(tmp_path: Path):
    """A cached decode failure is retried when the file changes, not kept forever."""
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:bad")
    session.add_message("user", "hello")
    manager.save(session)
    [path] = list((tmp_path / "sessions").rglob("*.jsonl"))
    readable = path.read_bytes()

    path.write_bytes(b"\xff\xfe\x00\x01")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    assert manager.list_sessions() == []

    path.write_bytes(readable)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

    assert [entry["key"] for entry in manager.list_sessions()] == ["cli:bad"]


def test_save_invalidates_the_scan_cache_when_the_stamp_would_not_change(tmp_path: Path):
    """A save that rewrites a transcript is listed fresh, not from the cache.

    The cache is keyed on ``(st_mtime_ns, st_size)``, and a same-size rewrite
    can land in the same mtime tick. The rewrite is made through ``save`` here:
    the turn is cut and retold with a message of the same length, and the mtime
    is put back on the cached value, so only the explicit drop can tell the
    listing that the file changed.
    """
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:one")
    session.add_message("user", "hello")
    manager.save(session)
    # A rewrite save first, so the file the cache records and the rewrite the
    # test makes below share a line-ending convention on every platform.
    session.undo_last_turn()
    session.add_message("user", "world")
    manager.save(session)
    path = manager.session_path("cli:one")
    before = path.stat()
    assert manager.list_sessions()[0]["last_message_preview"] == "world"
    assert path in manager._scan_cache

    session.undo_last_turn()
    session.add_message("user", "again")
    manager.save(session)
    after = path.stat()
    assert after.st_size == before.st_size, "the regression needs the stamp to stay put"
    os.utime(path, ns=(after.st_atime_ns, before.st_mtime_ns))

    listed = manager.list_sessions()
    assert listed[0]["last_message_preview"] == "again"
    assert listed[0]["message_count"] == 1


def test_append_metadata_patch_invalidates_the_scan_cache(tmp_path: Path):
    """A patch is visible to the very next listing, even on a colliding stamp.

    An append always changes the size, so the stamp alone would usually notice
    it; the explicit drop covers a clock that hands the same mtime back. The
    cache entry is seeded with the stamp the append will leave, which is the
    case the drop exists for.
    """
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:one")
    session.add_message("user", "hello")
    manager.save(session)
    path = manager.session_path("cli:one")
    manager.list_sessions()
    cached = manager._scan_cache[path]

    patch = {"archived": True}
    last = manager._scan_file(path)[0]
    merged = {**(last.get("metadata") or {}), **patch}
    line = json.dumps({**last, "metadata": merged}, ensure_ascii=False)
    appended_size = path.stat().st_size + len((line + "\n").encode("utf-8"))
    appended_mtime = cached[0] + 1_000_000_000
    manager._scan_cache[path] = (appended_mtime, appended_size, cached[2])

    assert manager.append_metadata_patch("cli:one", patch) is True
    stat = path.stat()
    assert stat.st_size == appended_size
    os.utime(path, ns=(stat.st_atime_ns, appended_mtime))

    [listed] = manager.list_sessions()
    assert listed["metadata"].get("archived") is True


def test_a_session_recreated_after_delete_is_scanned_fresh(tmp_path: Path):
    """A new transcript at a deleted session's path never reads the old entry.

    ``delete`` drops the cached scan itself because the stamp alone can match:
    a same-key session rewritten to the same size can share the removed file's
    mtime. The replacement is written by another manager, as a second client
    would, and its stamp is put back on the old one.
    """
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:one")
    session.add_message("user", "old!")
    manager.save(session)
    path = manager.session_path("cli:one")
    before = path.stat()
    assert manager.list_sessions()[0]["last_message_preview"] == "old!"

    assert manager.delete("cli:one") is True

    other = SessionManager(tmp_path)
    recreated = other.get_or_create("cli:one")
    recreated.add_message("user", "new!")
    other.save(recreated)
    after = path.stat()
    assert after.st_size == before.st_size, "the regression needs the stamp to stay put"
    os.utime(path, ns=(after.st_atime_ns, before.st_mtime_ns))

    assert manager.list_sessions()[0]["last_message_preview"] == "new!"


def test_list_sessions_metadata_is_a_top_level_copy(tmp_path: Path):
    """A caller editing the returned record must not edit the cached parse.

    Only the top level is copied: a nested dict or list is still shared with
    the cache, which is the accepted trade-off for not deep-copying every
    listing.
    """
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:one")
    session.add_message("user", "hello")
    session.metadata["note"] = "kept"
    manager.save(session)

    first = manager.list_sessions()[0]
    assert first["metadata"]["note"] == "kept"
    first["metadata"]["note"] = "changed"

    assert manager.list_sessions()[0]["metadata"]["note"] == "kept"


def test_list_sessions_reports_empty_metadata_for_a_legacy_record(tmp_path: Path):
    """A metadata record from before the field existed lists as ``{}``, not None."""
    path = tmp_path / "sessions" / "tui" / "legacy01.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"_type": "metadata", "key": "tui:legacy01", "created_at": "2026-01-01T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    assert SessionManager(tmp_path).list_sessions()[0]["metadata"] == {}
