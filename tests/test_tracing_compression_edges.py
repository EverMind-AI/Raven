"""Edge paths of the compressed trace store, exercised directly.

The happy paths live with their modules; these pin the branches a migration or
a damaged store reaches: an index write that fails, a file that cannot be
removed, a compressed log that is already compressed, and readers that must
degrade to "missing" instead of raising.
"""

from __future__ import annotations

from pathlib import Path

from raven.tracing import artifact_v2, message_pack
from raven.tracing import codec as trace_codec
from raven.tracing.store import TraceStore
from raven.trajectory import bundle, selection
from raven.trajectory import index as tidx

MAGIC = b"\x28\xb5\x2f\xfd"


def _publish(store: TraceStore, content: str = "hi") -> str:
    return store.address_items([{"role": "user", "content": content}])[0]["$msg"]


def test_path_mtime_and_a_missing_pack(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    sha1 = _publish(store)
    message_pack.pack_messages(store.artifacts_dir)
    path = artifact_v2.message_path(store.artifacts_dir, sha1)
    assert message_pack.path_mtime(path) is not None

    next(message_pack.packs_dir(store.artifacts_dir).glob("*.pack.zst")).unlink()
    assert message_pack.path_mtime(path) is None


def test_pack_without_a_message_directory(tmp_path: Path) -> None:
    result = message_pack.pack_messages(tmp_path / "logs" / "audit-artifacts")
    assert result.scanned == 0


def test_pack_skips_a_file_that_is_not_a_message(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    _publish(store)
    (store.messages_dir / "notes.txt").write_text("x", encoding="utf-8")

    result = message_pack.pack_messages(store.artifacts_dir)

    assert result.packed == 1


def test_pack_index_write_failure_keeps_the_files(tmp_path: Path, monkeypatch) -> None:
    store = TraceStore(tmp_path)
    sha1 = _publish(store)
    path = artifact_v2.message_path(store.artifacts_dir, sha1)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(message_pack, "_atomic_write_index", boom)
    result = message_pack.pack_messages(store.artifacts_dir)

    assert result.errors
    assert path.exists()


def test_pack_removes_an_orphan_pack(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    _publish(store)
    message_pack.pack_messages(store.artifacts_dir)
    orphan = message_pack.packs_dir(store.artifacts_dir) / ("0" * 32 + ".pack.zst")
    orphan.write_bytes(b"orphan")
    _publish(store, "second")

    message_pack.pack_messages(store.artifacts_dir)

    assert not orphan.exists()


def test_pack_reports_a_file_it_cannot_remove(tmp_path: Path, monkeypatch) -> None:
    store = TraceStore(tmp_path)
    sha1 = _publish(store)
    path = artifact_v2.message_path(store.artifacts_dir, sha1)
    original = Path.unlink

    def failing(self, *args, **kwargs):
        if self == path:
            raise OSError("busy")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing)
    result = message_pack.pack_messages(store.artifacts_dir)

    assert any("busy" in error for error in result.errors)


def test_blob_intact_rejects_a_corrupt_frame(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    blob = store._blob_path("a" * 40, "json")
    blob.parent.mkdir(parents=True)
    blob.write_bytes(MAGIC + b" broken")

    assert store._blob_is_intact(blob, "a" * 40) is False


def test_compress_rotated_skips_a_compressed_log(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    path = tmp_path / "rotated.log"
    path.write_bytes(trace_codec.compress_bytes(b"line\n"))
    before = path.read_bytes()

    store._compress_rotated(path)

    assert path.read_bytes() == before


def test_compress_rotated_swallows_a_missing_file(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    store._compress_rotated(tmp_path / "missing.log")


def test_stored_bytes_of_a_missing_file_is_none(tmp_path: Path) -> None:
    assert bundle._stored_bytes(tmp_path / "gone.json") is None


def test_copy_resolved_rejects_invalid_json(tmp_path: Path) -> None:
    source = tmp_path / "src.json"
    source.write_text("{not json", encoding="utf-8")

    assert bundle._copy_resolved(source, tmp_path / "out.json", tmp_path / "artifacts", []) is False


def test_plaintext_size_cache_hit_and_unreadable_file(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "audit-spans.log").write_text('{"spanId": "s"}\n', encoding="utf-8")
    # A name the chain lists but whose plaintext size cannot be read is skipped.
    (logs / "archive" / "2026-01-01" / "audit-spans-1.log").mkdir(parents=True)

    scanner = tidx.SpanLogScanner(tmp_path)
    first = scanner._catalog()
    second = scanner._catalog()

    assert first == second
    assert [path.name for path, *_ in first] == ["audit-spans.log"]


def test_a_packed_read_outside_the_root_is_refused(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "store")
    sha1 = _publish(store)
    message_pack.pack_messages(store.artifacts_dir)
    path = artifact_v2.message_path(store.artifacts_dir, sha1)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    assert selection.read_store_file(path, outside) is None


def test_turn_input_degrades_on_bad_json(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "logs").mkdir(parents=True)
    artifact = state / "logs" / "audit-artifacts" / "turn.input" / "2026-10-01" / "bad.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{not json", encoding="utf-8")
    turn = selection.TurnSummary(
        trace_id="t",
        span_id="s",
        number=1,
        start="2026-10-01T10:00:00+00:00",
        end=None,
        input_preview="",
        in_progress=False,
        input_artifact=str(artifact),
        main_line=True,
    )

    assert selection._turn_input(turn, state) is None
