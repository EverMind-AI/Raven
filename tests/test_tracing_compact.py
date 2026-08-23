"""Unit tests for ``raven.tracing.compact``.

Covers the two halves of a compaction run: folding already-written artifacts
onto one blob per distinct payload, and removing blobs nothing references any
more. Both must leave every artifact path in place and byte-identical.
"""

from __future__ import annotations

import errno
import hashlib
import os
import time
from pathlib import Path

from raven.tracing.compact import FRESH_SECONDS, CompactResult, compact


def _write(artifacts: Path, kind: str, day: str, name: str, text: str, *, age: int = 3600) -> Path:
    path = artifacts / kind / day / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def _plant_blob(artifacts: Path, text: str, *, age: int = 3600) -> Path:
    sha1 = hashlib.sha1(text.encode("utf-8")).hexdigest()
    blob = artifacts / "_blobs" / sha1[:2] / f"{sha1}.json"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_text(text, encoding="utf-8")
    stamp = time.time() - age
    os.utime(blob, (stamp, stamp))
    return blob


def test_duplicate_files_are_folded_onto_one_inode(tmp_path):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    b = _write(tmp_path, "llm.input", "2026-08-02", "b.json", '{"m":"same"}')

    result = compact(tmp_path)

    assert result.scanned == 2
    assert result.folded == 1
    assert result.errors == []
    assert a.stat().st_ino == b.stat().st_ino
    assert a.read_text(encoding="utf-8") == '{"m":"same"}'
    assert b.read_text(encoding="utf-8") == '{"m":"same"}'


def test_distinct_files_are_left_alone(tmp_path):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"one"}')
    b = _write(tmp_path, "llm.input", "2026-08-01", "b.json", '{"m":"two"}')

    result = compact(tmp_path)

    assert result.folded == 0
    assert a.stat().st_ino != b.stat().st_ino


def test_compaction_is_idempotent(tmp_path):
    _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    _write(tmp_path, "llm.input", "2026-08-01", "b.json", '{"m":"same"}')

    compact(tmp_path)
    second = compact(tmp_path)

    assert second.folded == 0
    assert second.bytes_reclaimed == 0


def test_fresh_files_are_skipped(tmp_path):
    _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    b = _write(tmp_path, "llm.input", "2026-08-01", "b.json", '{"m":"same"}', age=FRESH_SECONDS // 2)
    before = b.stat().st_ino

    result = compact(tmp_path)

    assert result.skipped_fresh == 1
    assert b.stat().st_ino == before


def test_dry_run_writes_nothing(tmp_path):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    b = _write(tmp_path, "llm.input", "2026-08-01", "b.json", '{"m":"same"}')

    result = compact(tmp_path, dry_run=True)

    assert result.folded == 1
    assert a.stat().st_ino != b.stat().st_ino
    assert not (tmp_path / "_blobs").exists()


def test_dry_run_agrees_with_real_run_on_pre_existing_orphan_blob(tmp_path):
    dry_root = tmp_path / "dry"
    real_root = tmp_path / "real"
    payload = '{"m":"racing"}'
    for root in (dry_root, real_root):
        _write(root, "llm.input", "2026-08-01", "a.json", payload)
        _plant_blob(root, payload)

    dry_result = compact(dry_root, dry_run=True)
    real_result = compact(real_root)

    assert dry_result.folded == real_result.folded == 1
    assert dry_result.blobs_removed == real_result.blobs_removed == 0
    assert dry_result.bytes_reclaimed == real_result.bytes_reclaimed


def test_dry_run_agrees_with_real_run_for_two_extensions_no_blobs_yet(tmp_path):
    dry_root = tmp_path / "dry"
    real_root = tmp_path / "real"
    payload = '{"m":"same"}'
    for root in (dry_root, real_root):
        _write(root, "llm.input", "2026-08-01", "a.json", payload)
        _write(root, "llm.input", "2026-08-01", "b.txt", payload)

    dry_result = compact(dry_root, dry_run=True)
    real_result = compact(real_root)

    assert dry_result.folded == real_result.folded == 0


def test_corrupt_blob_is_refused_not_propagated(tmp_path):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"good"}')
    _plant_blob(tmp_path, '{"m":"good"}')
    sha1 = hashlib.sha1(b'{"m":"good"}').hexdigest()
    blob = tmp_path / "_blobs" / sha1[:2] / f"{sha1}.json"
    blob.write_text('{"m":"TRUNCA', encoding="utf-8")

    result = compact(tmp_path)

    assert a.read_text(encoding="utf-8") == '{"m":"good"}'
    assert result.folded == 0
    assert result.errors != []


def test_two_extensions_of_the_same_payload_verify_independently(tmp_path):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", "")
    b = _write(tmp_path, "llm.input", "2026-08-01", "b.txt", "")
    sha1 = hashlib.sha1(b"").hexdigest()
    torn = tmp_path / "_blobs" / sha1[:2] / f"{sha1}.txt"
    torn.parent.mkdir(parents=True, exist_ok=True)
    torn.write_text("CORRUPT-NOT-EMPTY", encoding="utf-8")
    stamp = time.time() - 3600
    os.utime(torn, (stamp, stamp))

    result = compact(tmp_path)

    assert a.read_text(encoding="utf-8") == ""
    assert b.read_text(encoding="utf-8") == ""
    assert result.errors != []


def test_stale_compact_tmp_is_not_adopted_and_is_swept(tmp_path, monkeypatch):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    compact(tmp_path)
    blob = sorted((tmp_path / "_blobs").rglob("*.json"))[0]
    tmp = a.parent / ".a.json.compact.tmp"
    os.link(blob, tmp)
    # A tmp is always an os.link to an existing blob, so its mtime is
    # whatever the blob's content mtime is, not when the link was made;
    # only ctime reflects the link, and ctime cannot be backdated. Advance
    # the clock compact() reads instead of the file's timestamps.
    future = time.time() + FRESH_SECONDS + 60
    monkeypatch.setattr(time, "time", lambda: future)

    result = compact(tmp_path)

    assert result.scanned == 1
    assert result.errors == []
    assert not tmp.exists()
    assert blob.exists()
    assert len(list((tmp_path / "_blobs").rglob("*.json"))) == 1
    assert a.read_text(encoding="utf-8") == '{"m":"same"}'


def test_stale_compact_tmp_lets_its_orphaned_blob_be_reclaimed(tmp_path, monkeypatch):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"gone"}')
    compact(tmp_path)
    blob = sorted((tmp_path / "_blobs").rglob("*.json"))[0]
    tmp = a.parent / ".a.json.compact.tmp"
    os.link(blob, tmp)
    a.unlink()
    stamp = time.time() - 3600
    os.utime(blob, (stamp, stamp))
    future = time.time() + FRESH_SECONDS + 60
    monkeypatch.setattr(time, "time", lambda: future)

    result = compact(tmp_path)

    assert not tmp.exists()
    assert result.blobs_removed == 1
    assert result.errors == []
    assert not blob.exists()


def test_fresh_compact_tmp_survives_a_concurrent_run(tmp_path):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    compact(tmp_path)
    blob = sorted((tmp_path / "_blobs").rglob("*.json"))[0]
    tmp = a.parent / ".a.json.compact.tmp"
    os.link(blob, tmp)

    compact(tmp_path)

    assert tmp.exists()


def test_unreferenced_blobs_are_removed(tmp_path):
    path = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"gone"}')
    compact(tmp_path)
    blobs = sorted((tmp_path / "_blobs").rglob("*.json"))
    assert len(blobs) == 1
    stamp = time.time() - 3600
    path.unlink()
    os.utime(blobs[0], (stamp, stamp))

    result = compact(tmp_path)

    assert result.blobs_removed == 1
    assert result.errors == []
    assert not blobs[0].exists()


def test_referenced_blobs_survive(tmp_path):
    _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"kept"}')
    compact(tmp_path)
    blob = sorted((tmp_path / "_blobs").rglob("*.json"))[0]
    stamp = time.time() - 3600
    os.utime(blob, (stamp, stamp))

    result = compact(tmp_path)

    assert result.blobs_removed == 0
    assert blob.exists()


def test_fresh_orphan_blobs_survive(tmp_path):
    path = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"racing"}')
    compact(tmp_path)
    blob = sorted((tmp_path / "_blobs").rglob("*.json"))[0]
    path.unlink()
    os.utime(blob, None)

    result = compact(tmp_path)

    assert result.blobs_removed == 0
    assert blob.exists()


def test_orphan_blob_of_different_extension_is_reclaimed(tmp_path):
    payload = '{"m":"shared"}'
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", payload)
    compact(tmp_path)
    sha1 = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    json_blob = tmp_path / "_blobs" / sha1[:2] / f"{sha1}.json"
    assert json_blob.exists()
    txt_blob = tmp_path / "_blobs" / sha1[:2] / f"{sha1}.txt"
    txt_blob.write_text(payload, encoding="utf-8")
    stamp = time.time() - 3600
    os.utime(txt_blob, (stamp, stamp))

    result = compact(tmp_path)

    assert result.blobs_removed == 1
    assert not txt_blob.exists()
    assert json_blob.exists()
    assert a.read_text(encoding="utf-8") == payload


def test_blobs_directory_is_not_scanned_as_a_kind(tmp_path):
    _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    _write(tmp_path, "llm.input", "2026-08-01", "b.json", '{"m":"same"}')

    compact(tmp_path)
    second = compact(tmp_path)

    assert second.scanned == 2


def test_missing_artifacts_dir_is_not_an_error(tmp_path):
    result = compact(tmp_path / "nope")

    assert result == CompactResult()


def test_unreadable_day_dir_is_reported_and_the_rest_is_still_processed(tmp_path, monkeypatch):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"locked"}')
    b = _write(tmp_path, "llm.input", "2026-08-02", "b.json", '{"m":"same"}')
    c = _write(tmp_path, "llm.input", "2026-08-02", "c.json", '{"m":"same"}')
    locked_dir = a.parent
    real_iterdir = Path.iterdir

    # This box runs the test suite as uid 0, where chmod 000 does not deny a
    # root process access to iterdir(); inject the failure instead of relying
    # on real filesystem permissions.
    def fake_iterdir(self):
        if self == locked_dir:
            raise PermissionError(13, "Permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)

    result = compact(tmp_path)

    assert result.errors
    assert any(str(locked_dir) in message for message in result.errors)
    assert result.scanned == 2
    assert result.folded == 1
    assert b.stat().st_ino == c.stat().st_ino
    assert a.read_text(encoding="utf-8") == '{"m":"locked"}'


def test_emlink_on_fold_is_skipped_not_errored(tmp_path, monkeypatch):
    """A blob already at the filesystem's hard-link ceiling answers a further
    link attempt with EMLINK. That artifact must keep its own copy instead of
    being reported as an error -- an error would just repeat itself on every
    later run -- and every other file in the same run must still converge.
    """
    capped_payload = '{"m":"capped"}'
    capped_blob = _plant_blob(tmp_path, capped_payload)
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", capped_payload)
    b = _write(tmp_path, "llm.input", "2026-08-01", "b.json", capped_payload)
    c = _write(tmp_path, "llm.input", "2026-08-01", "c.json", '{"m":"normal"}')
    d = _write(tmp_path, "llm.input", "2026-08-01", "d.json", '{"m":"normal"}')
    real_link = os.link

    def flaky_link(src, dst, *args, **kwargs):
        if Path(src) == capped_blob:
            raise OSError(errno.EMLINK, "Too many links")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", flaky_link)

    result = compact(tmp_path)

    assert result.scanned == 4
    assert result.errors == []
    assert a.read_text(encoding="utf-8") == capped_payload
    assert b.read_text(encoding="utf-8") == capped_payload
    assert a.stat().st_ino != b.stat().st_ino
    assert c.stat().st_ino == d.stat().st_ino
    assert result.folded == 1


def test_errors_are_capped_and_the_overflow_is_counted(tmp_path, monkeypatch):
    """A systemic failure (here: every hash read failing) must not grow
    ``result.errors`` without bound. Past the cap, ``errors_dropped`` still
    tells the caller how many more were suppressed.
    """
    monkeypatch.setattr("raven.tracing.compact._MAX_ERRORS", 3)

    def always_fails(path):
        raise OSError("boom")

    monkeypatch.setattr("raven.tracing.compact._sha1_of", always_fails)
    for i in range(5):
        _write(tmp_path, "llm.input", "2026-08-01", f"a{i}.json", '{"m":"x"}')

    result = compact(tmp_path)

    assert result.scanned == 5
    assert len(result.errors) == 3
    assert result.errors_dropped == 2
