"""Tests for the on-disk redaction history (record, resolve, remember, forget, clear)."""

from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

from raven.trajectory import redaction_history as rh
from raven.trajectory import review as treview
from raven.trajectory.redact import _mask

TOKEN_A = "qT7zK9mP4vX2sW8dQ5nRfJ3y"
TOKEN_B = "Zx4cV7bN1mQ9kL6pT2wYhG8s"
TOKEN_C = "Hj5tR8uE3iO7pA1sD4fGk9lZW6xC2v"
TOKEN_D = "Pn3bQ8rT1yU6iO9pL2kJ7hG4fD5sA"


@pytest.fixture
def path(tmp_path) -> Path:
    return tmp_path / "home" / rh.HISTORY_FILENAME


@pytest.fixture
def hist(path) -> rh.RedactionHistory:
    return rh.RedactionHistory(path)


def _item(token: str, *, linked=(), kind=treview.KIND_SUSPECTED) -> treview.ReviewItem:
    item_id = treview._item_id(token or "pattern.private-key-block")
    return treview.ReviewItem(
        id=item_id,
        kind=kind,
        category="high-entropy" if token else "private-key-block",
        token=token,
        masked_sample="sample",
        linked=list(linked),
    )


def _key_item() -> treview.ReviewItem:
    return _item("", kind=treview.KIND_CONFIRMED)


def _link(*items: treview.ReviewItem) -> None:
    for item in items:
        for other in items:
            if other is not item and other.id not in item.linked:
                item.linked.append(other.id)


def _decide(items, actions: dict[str, str]) -> list[treview.ReviewDecision]:
    return [treview.ReviewDecision(item.id, actions[item.id]) for item in items]


def _entry(**overrides) -> dict:
    entry = {
        "action": "kept",
        "category": "high-entropy",
        "masked": "abcd***wxyz",
        "decided_at": "2026-10-01T00:00:00.000000+00:00",
        "source": "cli",
        "revision": "0123456789abcdef",
        "applied": 0,
        "last_applied_at": None,
    }
    entry.update(overrides)
    return entry


def _write_doc(path: Path, entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8")


def _siblings(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if p.name != ".lock")


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{rh.HISTORY_FILENAME}.corrupt-*"))


# ── module switches ──────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", " false "])
def test_enabled_off_values(value):
    assert rh.enabled({rh.ENV_SWITCH: value}) is False


@pytest.mark.parametrize("environ", [{}, {rh.ENV_SWITCH: ""}, {rh.ENV_SWITCH: "on"}, {rh.ENV_SWITCH: "1"}])
def test_enabled_by_default(environ):
    assert rh.enabled(environ) is True


def test_default_path_follows_raven_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "custom"))
    assert rh.default_path() == tmp_path / "custom" / rh.HISTORY_FILENAME
    assert rh.RedactionHistory().path == tmp_path / "custom" / rh.HISTORY_FILENAME


# ── record / lookup / read ───────────────────────────────────────────


def test_record_round_trip_stores_hash_and_mask_only(hist, path):
    entry = hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")

    assert hist.lookup(TOKEN_A) == entry
    assert hist.read().entries == [entry]
    assert entry.id == entry.digest[:12] == treview._item_id(TOKEN_A)
    assert entry.masked == _mask(TOKEN_A)
    assert entry.applied == 0 and entry.last_applied_at is None
    text = path.read_text(encoding="utf-8")
    assert TOKEN_A not in text
    assert list(json.loads(text)["entries"]) == [entry.digest]


def test_record_replaces_the_whole_entry(hist):
    first = hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    same = hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="browser")
    changed = hist.record(TOKEN_A, treview.ACTION_REDACTED, category="jwt-like", source="web")

    assert same.revision != first.revision
    assert same.source == "browser" and same.applied == 0
    assert changed.revision not in (first.revision, same.revision)
    assert hist.lookup(TOKEN_A) == changed
    assert len(hist.read().entries) == 1


def test_read_orders_newest_first(hist):
    hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    hist.record(TOKEN_B, treview.ACTION_REDACTED, category="high-entropy", source="cli")

    assert [e.masked for e in hist.read().entries] == [_mask(TOKEN_B), _mask(TOKEN_A)]


def test_files_are_private(hist, path):
    hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    assert path.stat().st_mode & 0o777 == 0o600
    hist.record(TOKEN_B, treview.ACTION_KEPT, category="high-entropy", source="cli")
    assert path.stat().st_mode & 0o777 == 0o600


def test_concurrent_records_lose_nothing(path):
    tokens = [f"Tq{i:02d}Xv8kP3mW7zR2nL5bY9cJ4hD" for i in range(20)]

    def _work(chunk):
        history = rh.RedactionHistory(path)
        for token in chunk:
            history.record(token, treview.ACTION_KEPT, category="high-entropy", source="cli")

    threads = [threading.Thread(target=_work, args=(tokens[i::2],)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(rh.RedactionHistory(path).read().entries) == 20


@pytest.mark.parametrize(
    "token, action, category, source",
    [
        ("", "kept", "high-entropy", "cli"),
        (TOKEN_A, "acknowledged", "high-entropy", "cli"),
        (TOKEN_A, "kept", "", "cli"),
        (TOKEN_A, "kept", "high-entropy", "email"),
    ],
)
def test_record_rejects_bad_arguments(hist, path, token, action, category, source):
    with pytest.raises(ValueError):
        hist.record(token, action, category=category, source=source)
    assert not path.exists()


# ── replay against concurrent writers ────────────────────────────────


def _replay_setup(path):
    seed = rh.RedactionHistory(path)
    original = seed.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    items = [_item(TOKEN_A)]
    reader = rh.RedactionHistory(path)
    resolution = reader.resolve(items)
    assert resolution.replayed[items[0].id].revision == original.revision
    return original, items, reader, resolution


def test_replay_counts_a_reuse_when_unchanged(path):
    original, items, reader, resolution = _replay_setup(path)

    result = reader.remember(items, resolution.auto, source="cli", replayed=resolution.replayed)

    assert (result.recorded, result.replayed, result.stale) == (0, 1, 0)
    entry = reader.lookup(TOKEN_A)
    assert entry.applied == 1 and entry.last_applied_at is not None
    assert entry.revision == original.revision and entry.decided_at == original.decided_at


def test_stale_replay_never_overrides_a_newer_answer(path):
    _original, items, reader, resolution = _replay_setup(path)
    newer = rh.RedactionHistory(path).record(TOKEN_A, treview.ACTION_REDACTED, category="high-entropy", source="web")

    result = reader.remember(items, resolution.auto, source="cli", replayed=resolution.replayed)

    assert (result.recorded, result.replayed, result.stale) == (0, 0, 1)
    assert reader.lookup(TOKEN_A) == newer


@pytest.mark.parametrize("eraser", ["forget", "clear"])
def test_stale_replay_never_revives_a_removed_record(path, eraser):
    original, items, reader, resolution = _replay_setup(path)
    other = rh.RedactionHistory(path)
    if eraser == "forget":
        assert other.forget([original.id]).removed == [original.id]
    else:
        assert other.clear().removed == 1

    result = reader.remember(items, resolution.auto, source="cli", replayed=resolution.replayed)

    assert result.stale == 1 and result.replayed == 0
    assert reader.lookup(TOKEN_A) is None


# ── failures are distinguishable ─────────────────────────────────────


def _fail_replace(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "replace", _boom)


def test_save_failure_raises_and_leaves_no_trace(hist, path, monkeypatch):
    hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    before = path.read_bytes()
    _fail_replace(monkeypatch)

    with pytest.raises(rh.HistoryStorageError, match="cannot save"):
        hist.record(TOKEN_B, treview.ACTION_KEPT, category="high-entropy", source="cli")

    assert path.read_bytes() == before
    assert _siblings(path) == [rh.HISTORY_FILENAME]


@pytest.mark.parametrize("call", ["forget", "clear", "remember"])
def test_write_failures_raise_and_keep_the_records(hist, path, monkeypatch, call):
    entry = hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    _fail_replace(monkeypatch)

    with pytest.raises(rh.HistoryStorageError):
        if call == "forget":
            hist.forget([entry.id])
        elif call == "clear":
            hist.clear()
        else:
            items = [_item(TOKEN_B)]
            hist.remember(items, _decide(items, {items[0].id: "redacted"}), source="cli")

    monkeypatch.undo()
    assert hist.read().entries == [entry]
    assert _siblings(path) == [rh.HISTORY_FILENAME]


def test_clear_on_an_empty_history_succeeds_without_a_file(hist, path):
    result = hist.clear()

    assert result == rh.ClearResult(removed=0, backup=None)
    assert not path.exists()


def test_unreadable_file_raises_instead_of_reading_empty(hist, path, monkeypatch):
    hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    real_read_bytes = Path.read_bytes

    def _denied(self):
        if self == path:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _denied)

    for call in (
        hist.read,
        lambda: hist.lookup(TOKEN_A),
        lambda: hist.resolve([_item(TOKEN_A)]),
        lambda: hist.record(TOKEN_B, treview.ACTION_KEPT, category="high-entropy", source="cli"),
    ):
        with pytest.raises(rh.HistoryStorageError, match="cannot read"):
            call()


# ── corrupt files ────────────────────────────────────────────────────

CORRUPT_CONTENTS = [
    b"\xff\xfe",
    b"{not json",
    json.dumps({"schema_version": 1, "entries": {"short": {}}}).encode(),
    json.dumps({"schema_version": 1, "entries": {"a" * 64: _entry(action="ignored")}}).encode(),
    json.dumps({"schema_version": 1, "entries": []}).encode(),
    json.dumps(["not", "an", "object"]).encode(),
    json.dumps({"entries": {}}).encode(),
]


@pytest.mark.parametrize("content", CORRUPT_CONTENTS)
def test_corrupt_file_reads_as_empty_with_a_reason(hist, path, content):
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    snapshot = hist.read()
    resolution = hist.resolve([_item(TOKEN_A)])

    assert snapshot.entries == [] and snapshot.corrupt
    assert resolution.auto == [] and len(resolution.remaining) == 1
    assert resolution.corrupt == snapshot.corrupt


@pytest.mark.parametrize("content", CORRUPT_CONTENTS[:3])
def test_saving_over_a_corrupt_file_backs_it_up_first(hist, path, content):
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    entry = hist.record(TOKEN_A, treview.ACTION_REDACTED, category="high-entropy", source="cli")

    (backup,) = _backups(path)
    assert backup.read_bytes() == content
    assert backup.stat().st_mode & 0o777 == 0o600
    assert hist.read() == rh.HistorySnapshot(entries=[entry], corrupt=None)


def test_backup_never_overwrites_an_existing_backup(hist, path, monkeypatch):
    monkeypatch.setattr(rh, "_backup_stamp", lambda: "20261013T000000000000Z")
    path.parent.mkdir(parents=True)
    taken = path.parent / f"{rh.HISTORY_FILENAME}.corrupt-20261013T000000000000Z"
    taken.write_bytes(b"older backup")
    path.write_bytes(b"{not json")

    result = hist.clear()

    assert result.backup == path.parent / f"{taken.name}-1"
    assert result.backup.read_bytes() == b"{not json"
    assert taken.read_bytes() == b"older backup"


def test_backup_create_failure_leaves_the_original(hist, path, monkeypatch):
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{not json")
    real_open = os.open

    def _no_space(target, *args, **kwargs):
        if ".corrupt-" in os.fspath(target):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(os, "open", _no_space)

    with pytest.raises(rh.HistoryStorageError, match="back up"):
        hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")

    assert path.read_bytes() == b"{not json"
    assert _siblings(path) == [rh.HISTORY_FILENAME]


def test_backup_write_failure_removes_the_partial_backup(hist, path, monkeypatch):
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{not json")

    def _io_error(_fd):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "fsync", _io_error)

    with pytest.raises(rh.HistoryStorageError, match="back up"):
        hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")

    assert path.read_bytes() == b"{not json"
    assert _siblings(path) == [rh.HISTORY_FILENAME]


def test_failed_save_removes_its_own_backup(hist, path, monkeypatch):
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{not json")
    _fail_replace(monkeypatch)

    with pytest.raises(rh.HistoryStorageError, match="cannot save"):
        hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")

    assert path.read_bytes() == b"{not json"
    assert _siblings(path) == [rh.HISTORY_FILENAME]


def test_recovery_interleaved_with_another_writer(path):
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{not json")
    items = [_item(TOKEN_A)]
    first = rh.RedactionHistory(path)
    resolution = first.resolve(items)
    assert resolution.corrupt

    rh.RedactionHistory(path).record(TOKEN_B, treview.ACTION_KEPT, category="high-entropy", source="web")
    result = first.remember(items, _decide(items, {items[0].id: "redacted"}), source="cli")

    assert result.recorded == 1 and result.backup is None
    assert {e.masked for e in first.read().entries} == {_mask(TOKEN_A), _mask(TOKEN_B)}
    assert len(_backups(path)) == 1


def test_newer_schema_is_never_overwritten(hist, path):
    path.parent.mkdir(parents=True)
    content = json.dumps({"schema_version": 2, "entries": {}}).encode()
    path.write_bytes(content)

    snapshot = hist.read()
    assert snapshot.entries == [] and "newer raven" in snapshot.corrupt
    for call in (
        lambda: hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli"),
        hist.clear,
    ):
        with pytest.raises(rh.HistoryStorageError, match="refusing to overwrite"):
            call()
    assert path.read_bytes() == content
    assert _backups(path) == []


# ── resolve ──────────────────────────────────────────────────────────


def test_resolve_decides_a_recorded_item(hist):
    entry = hist.record(TOKEN_A, treview.ACTION_REDACTED, category="high-entropy", source="cli")
    known, unknown = _item(TOKEN_A), _item(TOKEN_B)

    resolution = hist.resolve([unknown, known])

    assert resolution.auto == [treview.ReviewDecision(known.id, treview.ACTION_REDACTED)]
    assert resolution.remaining == [unknown]
    assert resolution.replayed == {known.id: entry}
    assert resolution.hints == {} and resolution.corrupt is None


def test_resolve_asks_a_partly_recorded_component(hist):
    inner_a, inner_b, outer = _item(TOKEN_A), _item(TOKEN_B), _item(TOKEN_C)
    _link(inner_a, outer)
    _link(inner_b, outer)
    hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    hist.record(TOKEN_C, treview.ACTION_KEPT, category="high-entropy", source="cli")

    resolution = hist.resolve([inner_a, inner_b, outer])

    assert resolution.auto == []
    assert resolution.remaining == [inner_a, inner_b, outer]
    assert set(resolution.hints) == {inner_a.id, outer.id}


def test_resolve_asks_a_component_with_disagreeing_records(hist):
    inner, outer = _item(TOKEN_A), _item(TOKEN_C)
    _link(inner, outer)
    hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    hist.record(TOKEN_C, treview.ACTION_REDACTED, category="high-entropy", source="cli")

    resolution = hist.resolve([inner, outer])

    assert resolution.auto == [] and resolution.remaining == [inner, outer]
    assert set(resolution.hints) == {inner.id, outer.id}


def test_resolve_decides_an_agreeing_component(hist):
    inner, outer, other = _item(TOKEN_A), _item(TOKEN_C), _item(TOKEN_D)
    _link(inner, outer)
    hist.record(TOKEN_A, treview.ACTION_REDACTED, category="high-entropy", source="cli")
    hist.record(TOKEN_C, treview.ACTION_REDACTED, category="high-entropy", source="cli")

    resolution = hist.resolve([outer, other, inner])

    assert [d.item_id for d in resolution.auto] == [outer.id, inner.id]
    assert resolution.remaining == [other]


def test_resolve_never_decides_confirmed_items(hist):
    key, item = _key_item(), _item(TOKEN_A)
    hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")

    resolution = hist.resolve([key, item])

    assert resolution.remaining == [key]
    assert [d.item_id for d in resolution.auto] == [item.id]


# ── remember ─────────────────────────────────────────────────────────


def test_remember_records_only_suspected_decisions(hist):
    key, kept, replaced = _key_item(), _item(TOKEN_A), _item(TOKEN_B)
    decisions = _decide(
        [key, kept, replaced],
        {key.id: "acknowledged", kept.id: "kept", replaced.id: "redacted"},
    )

    result = hist.remember([key, kept, replaced], decisions, source="browser")

    assert (result.recorded, result.replayed, result.stale) == (2, 0, 0)
    assert hist.lookup(TOKEN_A).action == "kept"
    assert hist.lookup(TOKEN_B).action == "redacted"
    assert {e.source for e in hist.read().entries} == {"browser"}


def test_remember_without_suspected_items_touches_nothing(hist, path):
    key = _key_item()

    result = hist.remember([key], _decide([key], {key.id: "acknowledged"}), source="cli")

    assert result == rh.RememberResult(recorded=0, replayed=0, stale=0)
    assert not path.exists()


def test_remember_with_only_stale_replays_does_not_rewrite(path):
    _original, items, reader, resolution = _replay_setup(path)
    rh.RedactionHistory(path).record(TOKEN_A, treview.ACTION_REDACTED, category="high-entropy", source="cli")
    inode = path.stat().st_ino

    reader.remember(items, resolution.auto, source="cli", replayed=resolution.replayed)

    assert path.stat().st_ino == inode


def test_remember_saves_once_per_review(hist, monkeypatch):
    hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    items = [_item(TOKEN_A), _item(TOKEN_B), _item(TOKEN_C)]
    resolution = hist.resolve(items)
    actions = {d.item_id: d.action for d in resolution.auto}
    actions.update({items[1].id: "redacted", items[2].id: "kept"})
    calls = []
    real_mkstemp = tempfile.mkstemp

    def _counting(*args, **kwargs):
        calls.append(1)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", _counting)

    result = hist.remember(items, _decide(items, actions), source="cli", replayed=resolution.replayed)

    assert (result.recorded, result.replayed) == (2, 1)
    assert len(calls) == 1


# ── forget ───────────────────────────────────────────────────────────


def test_forget_accepts_short_full_and_uppercase_ids(hist):
    a = hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    b = hist.record(TOKEN_B, treview.ACTION_KEPT, category="high-entropy", source="cli")
    c = hist.record(TOKEN_C, treview.ACTION_KEPT, category="high-entropy", source="cli")

    result = hist.forget([a.id, b.digest, c.id.upper(), a.id])

    assert result.removed == [a.id, b.id, c.id]
    assert result.unknown == [] and result.ambiguous == []
    assert hist.read().entries == []


def test_forget_with_an_unknown_id_removes_nothing(hist, path):
    a = hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    before = path.read_bytes()

    result = hist.forget([a.id, "  ffffffffffff "])

    assert result.removed == [] and result.unknown == ["  ffffffffffff "]
    assert path.read_bytes() == before


def test_forget_after_a_concurrent_delete_removes_nothing(path):
    first = rh.RedactionHistory(path)
    a = first.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")
    b = first.record(TOKEN_B, treview.ACTION_KEPT, category="high-entropy", source="cli")
    assert {e.id for e in first.read().entries} == {a.id, b.id}
    rh.RedactionHistory(path).forget([b.id])

    result = first.forget([a.id, b.id])

    assert result.removed == [] and result.unknown == [b.id]
    assert [e.id for e in first.read().entries] == [a.id]


def test_forget_an_ambiguous_prefix_removes_nothing(hist, path):
    prefix = "abcdef012345"
    _write_doc(path, {prefix + "0" * 52: _entry(), prefix + "1" * 52: _entry()})

    result = hist.forget([prefix])

    assert result.removed == [] and result.ambiguous == [prefix]
    assert len(hist.read().entries) == 2


@pytest.mark.parametrize("bad", ["xyz", "abcdef01234", "g" * 12])
def test_forget_treats_malformed_ids_as_unknown(hist, bad):
    hist.record(TOKEN_A, treview.ACTION_KEPT, category="high-entropy", source="cli")

    result = hist.forget([bad])

    assert result.unknown == [bad] and result.removed == []
    assert len(hist.read().entries) == 1


def test_forget_nothing_is_a_no_op(hist, path):
    assert hist.forget([]) == rh.ForgetResult()
    assert not path.exists()
