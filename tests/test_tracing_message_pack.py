"""Packed message blobs: a few large frames plus an address index.

A packed store keeps the artifact contract: an address resolves whether its
bytes still live in a file or in a pack slice, every reader that resolved a
file before resolves a packed address, and a migration never deletes a file
before the index naming its slice is durable.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.tracing import artifact_v2, message_pack
from raven.tracing.store import TraceStore
from raven.trajectory import conversation, details, selection

MESSAGES = [
    {"role": "user", "content": "first " * 100},
    {"role": "assistant", "content": "second " * 100},
]


def _publish(state: Path) -> list[dict]:
    return TraceStore(state).address_items(MESSAGES)


def test_the_pack_module_names_the_artifact_store_directory() -> None:
    """The literal exists to keep artifact_v2 out of this module's imports."""
    assert message_pack.MESSAGES_DIR_NAME == artifact_v2.MESSAGES_DIR_NAME


def test_pack_moves_files_into_a_pack_and_every_reader_resolves(tmp_path: Path) -> None:
    refs = _publish(tmp_path)
    sha1s = [ref["$msg"] for ref in refs]
    artifacts = tmp_path / "logs" / "audit-artifacts"
    texts = {sha1: artifact_v2.message_text(message).encode() for sha1, message in zip(sha1s, MESSAGES)}
    assert all(artifact_v2.message_path(artifacts, sha1).is_file() for sha1 in sha1s)

    result = message_pack.pack_messages(artifacts)

    assert (result.packed, result.packs, result.removed) == (2, 1, 2)
    assert result.errors == []
    assert not any(artifact_v2.message_path(artifacts, sha1).exists() for sha1 in sha1s)
    for sha1 in sha1s:
        assert message_pack.read_message(artifacts, sha1) == texts[sha1]

    shell = {"artifactFormat": artifact_v2.ARTIFACT_FORMAT, "messages": refs, "prompt": refs[0]}
    resolved = artifact_v2.resolve_payload(shell, artifacts)
    assert resolved["messages"] == MESSAGES
    assert resolved["prompt"] == MESSAGES[0]["content"]

    path = str(artifact_v2.message_path(artifacts, sha1s[0]))
    text, reason = conversation._read_artifact(tmp_path, path)
    assert reason is None
    assert json.loads(text) == MESSAGES[0]
    limited, note = conversation._read_artifact(tmp_path, path, 10)
    assert limited == texts[sha1s[0]][:10].decode()
    assert note is not None and "truncated" in note

    reader = details._Reader(tmp_path, max_artifacts=None, max_blobs=None, max_bytes=None)
    prefix, size = reader.blob_prefix(sha1s[0], 12)
    assert prefix == texts[sha1s[0]][:12].decode()
    assert size == len(texts[sha1s[0]])

    found = selection.read_store_file(artifact_v2.message_path(artifacts, sha1s[0]), artifacts.resolve())
    assert found is not None
    assert found[0] == texts[sha1s[0]]


def test_a_shell_stops_resolving_after_its_total_budget(tmp_path: Path) -> None:
    refs = _publish(tmp_path)
    artifacts = tmp_path / "logs" / "audit-artifacts"
    first = len(artifact_v2.message_text(MESSAGES[0]).encode())
    shell = {"artifactFormat": artifact_v2.ARTIFACT_FORMAT, "messages": refs}

    resolved = artifact_v2.resolve_payload(shell, artifacts, max_total_bytes=first + 1)

    assert resolved["messages"][0] == MESSAGES[0]
    assert "missing" in resolved["messages"][1]["content"]


def test_a_small_target_splits_messages_across_packs(tmp_path: Path) -> None:
    refs = _publish(tmp_path)
    artifacts = tmp_path / "logs" / "audit-artifacts"

    result = message_pack.pack_messages(artifacts, target_bytes=1)

    assert result.packs == 2
    for ref, message in zip(refs, MESSAGES):
        assert json.loads(message_pack.read_message(artifacts, ref["$msg"])) == message


def test_pack_is_idempotent_and_dry_run_writes_nothing(tmp_path: Path) -> None:
    refs = _publish(tmp_path)
    artifacts = tmp_path / "logs" / "audit-artifacts"
    files = [artifact_v2.message_path(artifacts, ref["$msg"]) for ref in refs]

    dry = message_pack.pack_messages(artifacts, dry_run=True)
    assert (dry.packed, dry.packs) == (2, 1)
    assert all(path.is_file() for path in files)
    assert not message_pack.index_path(artifacts).exists()

    once = message_pack.pack_messages(artifacts)
    assert once.packed == 2
    again = message_pack.pack_messages(artifacts)
    assert again.scanned == 0
    assert again.packed == 0


def test_the_writer_does_not_republish_a_packed_message(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    refs = store.address_items(MESSAGES)
    message_pack.pack_messages(store.artifacts_dir)

    assert store.address_items(MESSAGES) == refs
    assert not any(artifact_v2.message_path(store.artifacts_dir, ref["$msg"]).exists() for ref in refs)


def test_a_corrupt_message_file_is_reported_and_not_removed(tmp_path: Path) -> None:
    refs = _publish(tmp_path)
    artifacts = tmp_path / "logs" / "audit-artifacts"
    corrupt = artifact_v2.message_path(artifacts, refs[1]["$msg"])
    corrupt.write_bytes(b"\x28\xb5\x2f\xfd not a frame")

    result = message_pack.pack_messages(artifacts)

    assert result.packed == 1
    assert result.errors
    assert corrupt.exists()
    assert not artifact_v2.message_path(artifacts, refs[0]["$msg"]).exists()
    assert message_pack.read_message(artifacts, refs[0]["$msg"]) is not None
