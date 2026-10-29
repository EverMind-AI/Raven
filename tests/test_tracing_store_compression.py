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

import zstandard

from raven.tracing import artifact_v2
from raven.tracing.codec import plaintext_size
from raven.tracing.compact import compact
from raven.tracing.compress import compress
from raven.tracing.store import TraceStore
from raven.trajectory import details
from raven.trajectory.conversation import _read_artifact

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


def test_compact_records_a_corrupt_frame(tmp_path: Path) -> None:
    bad = tmp_path / "logs" / "audit-artifacts" / "llm.input" / "2026-01-01" / "bad.json"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(MAGIC + b" not a frame")
    os.utime(bad, (0, 0))

    result = compact(tmp_path / "logs" / "audit-artifacts")

    assert result.errors
    assert bad.exists()
