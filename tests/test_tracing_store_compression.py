"""Compressed artifact storage: one zstd copy per payload, paths unchanged.

The store keeps every payload under the same path it always used; only the
bytes change. That is what lets an old span's recorded ``artifact_path`` keep
pointing at a readable file after a migration, and why every reader decides by
the frame, never by the file name.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import zstandard

from raven.tracing import artifact_v2
from raven.tracing import codec as trace_codec
from raven.tracing.codec import is_compressed, plaintext_size
from raven.tracing.compact import compact
from raven.tracing.compress import compress, compress_logs
from raven.tracing.store import TraceStore
from raven.trajectory import details, selection
from raven.trajectory.conversation import _read_artifact
from raven.trajectory.store import iter_spans

MAGIC = b"\x28\xb5\x2f\xfd"
PAYLOAD = {"messages": [{"role": "user", "content": "hi " * 4000}]}


def _persist(state: Path, payload: object = PAYLOAD) -> dict:
    return TraceStore(state).persist_artifact("llm.input", {"traceId": "t", "sessionKey": "s"}, payload)


def _plain(data: bytes) -> bytes:
    return zstandard.ZstdDecompressor().decompressobj().decompress(data)


def test_persist_artifact_writes_a_compressed_blob_under_the_plain_path(tmp_path: Path) -> None:
    artifact = _persist(tmp_path)
    path = Path(artifact["path"])
    assert path.suffix == ".json" and not path.name.endswith(".zst")

    data = path.read_bytes()
    assert data.startswith(MAGIC)
    plain = _plain(data)
    assert json.loads(plain) == PAYLOAD

    sha1 = hashlib.sha1(plain).hexdigest()
    assert artifact["sha1"] == sha1
    assert artifact["bytes"] == len(plain)

    blob = tmp_path / "logs" / "audit-artifacts" / "_blobs" / sha1[:2] / f"{sha1}.json"
    assert blob.read_bytes().startswith(MAGIC)
    assert blob.stat().st_ino == path.stat().st_ino
    assert plaintext_size(blob) == len(plain)


def test_identical_payloads_share_one_blob(tmp_path: Path) -> None:
    first = _persist(tmp_path)
    second = _persist(tmp_path)
    assert Path(first["path"]).stat().st_ino == Path(second["path"]).stat().st_ino


def test_the_reader_decompresses_and_still_bounds_what_it_reads(tmp_path: Path) -> None:
    artifact = _persist(tmp_path)
    path = str(artifact["path"])

    text, reason = _read_artifact(tmp_path, path)
    assert reason is None
    assert json.loads(text) == PAYLOAD

    limited, note = _read_artifact(tmp_path, path, 64)
    assert limited == text[:64]
    assert note is not None and "truncated" in note


def test_plain_artifacts_from_before_compression_still_read(tmp_path: Path) -> None:
    plain = tmp_path / "logs" / "llm.input" / "2026-01-01" / "old.json"
    plain.parent.mkdir(parents=True)
    plain.write_text('{"old": true}', encoding="utf-8")

    text, reason = _read_artifact(tmp_path, str(plain))

    assert reason is None
    assert text == '{"old": true}'


def test_messages_are_compressed_and_addressable(tmp_path: Path) -> None:
    message = {"role": "user", "content": "x" * 500}
    store = TraceStore(tmp_path)

    refs = store.address_items([message])

    sha1 = artifact_v2.message_sha1(message)
    assert refs == [{"$msg": sha1}]
    path = artifact_v2.message_path(tmp_path / "logs" / "audit-artifacts", sha1)
    assert path.read_bytes().startswith(MAGIC)
    text, reason = _read_artifact(tmp_path, str(path))
    assert reason is None and json.loads(text) == message


def test_compact_reads_compressed_artifacts(tmp_path: Path) -> None:
    artifact = _persist(tmp_path)
    artifacts = tmp_path / "logs" / "audit-artifacts"

    result = compact(artifacts)

    assert result.errors == []
    assert result.scanned == 1
    assert result.folded == 0
    assert Path(artifact["path"]).read_bytes().startswith(MAGIC)


def test_compact_folds_a_second_copy_of_the_same_payload(tmp_path: Path) -> None:
    artifact = _persist(tmp_path)
    source = Path(artifact["path"])
    copy = source.parent / "copy.json"
    copy.write_bytes(source.read_bytes())
    os.utime(copy, (0, 0))

    result = compact(tmp_path / "logs" / "audit-artifacts")

    assert result.folded == 1
    assert copy.stat().st_ino == source.stat().st_ino


def _plant_plain_store(state: Path) -> tuple[Path, Path, str]:
    """A pre-compression store: a plain blob, its artifact link, and a message."""
    text = '{"legacy": "payload"}'
    sha1 = hashlib.sha1(text.encode("utf-8")).hexdigest()
    artifacts = state / "logs" / "audit-artifacts"
    blob = artifacts / "_blobs" / sha1[:2] / f"{sha1}.json"
    blob.parent.mkdir(parents=True)
    blob.write_text(text, encoding="utf-8")
    artifact = artifacts / "llm.input" / "2026-01-01" / "old.json"
    artifact.parent.mkdir(parents=True)
    os.link(blob, artifact)
    message = {"role": "user", "content": "m" * 300}
    msha = artifact_v2.message_sha1(message)
    mpath = artifact_v2.message_path(artifacts, msha)
    mpath.parent.mkdir(parents=True)
    mpath.write_text(artifact_v2.message_text(message), encoding="utf-8")
    return artifact, mpath, sha1


def test_compress_migrates_plain_blobs_and_keeps_every_path(tmp_path: Path) -> None:
    artifact, message_path, sha1 = _plant_plain_store(tmp_path)
    artifacts = tmp_path / "logs" / "audit-artifacts"

    result = compress(artifacts)

    assert result.compressed == 1
    assert result.errors == []
    assert artifact.read_bytes().startswith(MAGIC)
    assert message_path.read_bytes().startswith(MAGIC)
    blob = artifacts / "_blobs" / sha1[:2] / f"{sha1}.json"
    assert artifact.stat().st_ino == blob.stat().st_ino
    text, reason = _read_artifact(tmp_path, str(artifact))
    assert reason is None and text == '{"legacy": "payload"}'
    text, reason = _read_artifact(tmp_path, str(message_path))
    assert reason is None and json.loads(text)["content"] == "m" * 300


def test_compress_is_idempotent_and_dry_run_writes_nothing(tmp_path: Path) -> None:
    artifact, message_path, _ = _plant_plain_store(tmp_path)
    artifacts = tmp_path / "logs" / "audit-artifacts"
    before = artifact.read_bytes()

    dry = compress(artifacts, dry_run=True)
    assert dry.compressed == 1
    assert artifact.read_bytes() == before

    once = compress(artifacts)
    assert once.compressed == 1
    again = compress(artifacts)
    assert again.compressed == 0
    assert again.already >= 1
    assert artifact.read_bytes().startswith(MAGIC)
    assert message_path.read_bytes().startswith(MAGIC)


def test_blob_prefix_reports_the_plaintext_size(tmp_path: Path) -> None:
    message = {"role": "user", "content": "x" * 1000}
    TraceStore(tmp_path).address_items([message])
    sha1 = artifact_v2.message_sha1(message)
    plain = artifact_v2.message_text(message)
    reader = details._Reader(tmp_path, max_artifacts=None, max_blobs=None, max_bytes=None)

    text, size = reader.blob_prefix(sha1, 10)

    assert text == plain[:10]
    assert size == len(plain.encode("utf-8"))


def test_a_corrupt_frame_is_reported_not_raised(tmp_path: Path) -> None:
    bad = tmp_path / "logs" / "llm.input" / "2026-01-01" / "bad.json"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(MAGIC + b" not a frame")

    text, reason = _read_artifact(tmp_path, str(bad))

    assert text is None
    assert reason is not None and "corrupt" in reason


def test_decompression_is_bounded(tmp_path: Path) -> None:
    frame = zstandard.ZstdCompressor().compress(b"x" * 4096)
    assert trace_codec.read_plaintext(frame) == b"x" * 4096
    assert trace_codec.read_plaintext(frame, 1024) is None

    path = tmp_path / "bomb"
    path.write_bytes(frame)
    assert trace_codec.read_plaintext_file(path, 1024) is None
    with pytest.raises(OSError):
        trace_codec.open_plaintext(path, 1024)


def test_compact_records_a_corrupt_frame(tmp_path: Path) -> None:
    bad = tmp_path / "logs" / "audit-artifacts" / "llm.input" / "2026-01-01" / "bad.json"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(MAGIC + b" not a frame")
    os.utime(bad, (0, 0))

    result = compact(tmp_path / "logs" / "audit-artifacts")

    assert result.errors
    assert bad.exists()


def _span_record(trace: str, span_id: str) -> dict:
    return {"traceId": trace, "spanId": span_id, "name": "tool.call", "attributes": {}}


def _plant_logs(state: Path) -> Path:
    """A store with two rotated span logs, one active log, and one frame journal.

    Returns the older rotated log: the newest is left for the next rotation.
    """
    logs = state / "logs"
    archive = logs / "archive" / "2026-07-01" / "audit-spans-2026-07-01-1.log"
    archive.parent.mkdir(parents=True)
    archive.write_text(json.dumps(_span_record("t1", "s1")) + "\n", encoding="utf-8")
    os.utime(archive, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    newest = archive.with_name("audit-spans-2026-07-01-2.log")
    newest.write_text(json.dumps(_span_record("t1", "s1b")) + "\n", encoding="utf-8")
    os.utime(newest, ns=(1_000_000_100_000_000_000, 1_000_000_100_000_000_000))
    (logs / "audit-spans.log").write_text(json.dumps(_span_record("t2", "s2")) + "\n", encoding="utf-8")
    frames = logs / "acp-frames" / "2026-07-01" / "a.jsonl"
    frames.parent.mkdir(parents=True)
    frames.write_text('{"method": "x"}\n', encoding="utf-8")
    return archive


def _rotated(state: Path, kind: str, name: str, mtime_ns: int) -> Path:
    path = state / "logs" / "archive" / "2026-07-01" / f"audit-{kind}-2026-07-01-{name}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_span_record("t", f"{kind}-{name}")) + "\n", encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def test_compress_logs_leaves_the_newest_log_of_each_kind_and_keeps_mtimes(tmp_path: Path) -> None:
    """A writer that opened the active log just before it rotated may still
    append to the newest archive, so that one waits for the next rotation; the
    viewer orders logs by mtime, so a compressed one keeps its own."""
    old_spans = _rotated(tmp_path, "spans", "1", 1_000_000_000_000_000_000)
    new_spans = _rotated(tmp_path, "spans", "2", 1_000_000_100_000_000_000)
    old_events = _rotated(tmp_path, "events", "1", 1_000_000_000_000_000_000)
    new_events = _rotated(tmp_path, "events", "2", 1_000_000_100_000_000_000)

    result = compress_logs(tmp_path / "logs")

    for newest in (new_spans, new_events):
        assert not is_compressed(newest.read_bytes())
    for older in (old_spans, old_events):
        assert is_compressed(older.read_bytes())
        assert older.stat().st_mtime_ns == 1_000_000_000_000_000_000
    assert (result.files, result.compressed, result.newest) == (4, 2, 2)


def test_compress_logs_rewrites_only_rotated_files(tmp_path: Path) -> None:
    archive = _plant_logs(tmp_path)
    active = tmp_path / "logs" / "audit-spans.log"
    frames = tmp_path / "logs" / "acp-frames" / "2026-07-01" / "a.jsonl"
    active_before = active.read_bytes()
    frames_before = frames.read_bytes()

    result = compress_logs(tmp_path / "logs")

    assert (result.files, result.compressed, result.already, result.newest) == (2, 1, 0, 1)
    assert result.errors == []
    assert is_compressed(archive.read_bytes())
    # A span records a plaintext byte range into a frame journal, so that file
    # must keep its bytes; the active log is still being appended to.
    assert active.read_bytes() == active_before
    assert frames.read_bytes() == frames_before


def test_iter_spans_reads_a_compressed_archive(tmp_path: Path) -> None:
    archive = _plant_logs(tmp_path)
    compress_logs(tmp_path / "logs")
    assert is_compressed(archive.read_bytes())

    ids = [span["spanId"] for span in iter_spans(tmp_path)]

    assert ids == ["s1", "s1b", "s2"]


def test_selection_reads_a_compressed_archive(tmp_path: Path) -> None:
    archive = _plant_logs(tmp_path)
    compress_logs(tmp_path / "logs")
    assert is_compressed(archive.read_bytes())

    records = list(selection._records(tmp_path, None))

    assert [record["spanId"] for record in records] == ["s1", "s1b", "s2"]


def test_compress_logs_is_idempotent_and_dry_run_writes_nothing(tmp_path: Path) -> None:
    archive = _plant_logs(tmp_path)
    before = archive.read_bytes()

    dry = compress_logs(tmp_path / "logs", dry_run=True)
    assert dry.compressed == 1
    assert archive.read_bytes() == before

    once = compress_logs(tmp_path / "logs")
    assert once.compressed == 1
    again = compress_logs(tmp_path / "logs")
    assert again.compressed == 0
    assert (again.already, again.newest) == (1, 1)


def test_rotation_compresses_the_previous_archive_and_keeps_its_mtime(tmp_path: Path) -> None:
    """The viewer orders logs by mtime, so a re-encoded archive keeps the one
    it had; the log just rotated stays plain until the next rotation."""
    store = TraceStore(tmp_path, max_bytes=1)
    store.append_span(_span_record("t1", "s1"))
    store.append_span(_span_record("t2", "s2"))
    archive = tmp_path / "logs" / "archive"
    first = next(archive.rglob("audit-spans-*.log"))
    assert not is_compressed(first.read_bytes())
    mtime = first.stat().st_mtime_ns

    store.append_span(_span_record("t3", "s3"))

    second = next(path for path in archive.rglob("audit-spans-*.log") if path != first)
    assert is_compressed(first.read_bytes())
    assert first.stat().st_mtime_ns == mtime
    assert not is_compressed(second.read_bytes())
    assert [span["spanId"] for span in iter_spans(tmp_path)] == ["s1", "s2", "s3"]


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses to rename a log another handle holds open")
def test_a_writer_holding_the_active_log_across_rotation_keeps_its_span(tmp_path: Path) -> None:
    """Another writer can open the active log just before a rotation renames
    it and write a moment later; that line lands in the rotated inode."""
    store = TraceStore(tmp_path, max_bytes=1)
    store.append_span(_span_record("t1", "s1"))
    held = (tmp_path / "logs" / "audit-spans.log").open("a", encoding="utf-8")

    store.append_span(_span_record("t2", "s2"))
    held.write(json.dumps(_span_record("t3", "late")) + "\n")
    held.close()

    assert {span["spanId"] for span in iter_spans(tmp_path)} == {"s1", "s2", "late"}
