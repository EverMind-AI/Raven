"""Compress a trace store's payloads in place, under the paths they already have.

A span records its artifact's path when it is written, and those records are
the audit trail: a migration cannot rename them. So this keeps every path and
changes only the bytes, then re-points each artifact hard link at the one
compressed copy of its payload. Readers pick the encoding from the zstd frame,
never from the name, which is also what lets a half-migrated store read.

Idempotent: a blob or message already compressed is counted and left alone, so
a second pass writes nothing. A dry run compresses in memory to report the real
saving and writes no file at all.

The rotated span/event logs are the same story one level up: closed files under
``archive/``, re-encoded in place. The active log and the ACP frame journals
are not touched -- the first is still being appended to, and a span records a
plaintext byte range into the second.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import artifact_v2
from .codec import compress_bytes, is_compressed
from .message_pack import PACKS_DIR_NAME
from .store import BLOBS_DIR_NAME


@dataclass
class CompressResult:
    """What one pass saw, did, and saved."""

    blobs: int = 0
    compressed: int = 0
    already: int = 0
    relinked: int = 0
    messages: int = 0
    messages_compressed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return max(self.bytes_before - self.bytes_after, 0)


def _files(root: Path) -> Iterator[Path]:
    for directory, _, names in os.walk(root):
        for name in names:
            if name.startswith("."):
                continue
            path = Path(directory) / name
            if path.is_file():
                yield path


def _replace(path: Path, payload: bytes) -> None:
    """Swap ``path``'s bytes without ever leaving the name pointing at nothing."""
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.compress.tmp"
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _relink(blob: Path, artifact: Path, old_inode: int) -> bool:
    """Point ``artifact`` at ``blob``, leaving it readable across the swap.

    A hard link is staged beside the artifact and renamed over it, so the path
    never passes through a state where it does not exist. The inode check
    keeps a path that something else has already replaced from being clobbered.
    """
    try:
        if artifact.stat().st_ino != old_inode:
            return False
        staged = artifact.parent / f".{artifact.name}.{os.getpid()}.{uuid.uuid4().hex}.relink"
        try:
            os.link(blob, staged)
            os.replace(staged, artifact)
        finally:
            staged.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def compress(artifacts_dir: Path, *, dry_run: bool = False) -> CompressResult:
    """Compress every plain payload under ``artifacts_dir``; see the module doc."""
    result = CompressResult()
    artifacts_dir = Path(artifacts_dir)
    if not artifacts_dir.is_dir():
        return result
    blobs_dir = artifacts_dir / BLOBS_DIR_NAME
    messages_dir = artifact_v2.messages_dir(artifacts_dir)
    packs_dir = artifacts_dir / PACKS_DIR_NAME

    # One walk to learn which artifact paths share each blob inode; the links
    # must be moved after the blob's own name is swapped.
    links: dict[int, list[Path]] = {}
    for path in _files(artifacts_dir):
        if path.is_relative_to(blobs_dir) or path.is_relative_to(messages_dir) or path.is_relative_to(packs_dir):
            continue
        try:
            inode = path.stat().st_ino
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")
            continue
        links.setdefault(inode, []).append(path)

    for blob in _files(blobs_dir):
        result.blobs += 1
        try:
            data = blob.read_bytes()
            inode = blob.stat().st_ino
        except OSError as exc:
            result.errors.append(f"{blob}: {exc}")
            continue
        if is_compressed(data):
            result.already += 1
            continue
        packed = compress_bytes(data)
        result.bytes_before += len(data)
        result.bytes_after += len(packed)
        result.compressed += 1
        if dry_run:
            continue
        # Stage beside the blob first, move the artifact links onto the staged
        # inode, and only then give the blob name that inode: a crash in the
        # middle leaves every path readable, one encoding or the other.
        staged = blob.parent / f".{blob.name}.{os.getpid()}.{uuid.uuid4().hex}.compress.tmp"
        try:
            staged.write_bytes(packed)
            for artifact in links.get(inode, []):
                if _relink(staged, artifact, inode):
                    result.relinked += 1
            os.replace(staged, blob)
        except OSError as exc:
            result.errors.append(f"{blob}: {exc}")
        finally:
            staged.unlink(missing_ok=True)

    for path in _files(messages_dir):
        result.messages += 1
        try:
            data = path.read_bytes()
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")
            continue
        if is_compressed(data):
            continue
        packed = compress_bytes(data)
        result.bytes_before += len(data)
        result.bytes_after += len(packed)
        result.messages_compressed += 1
        if dry_run:
            continue
        try:
            _replace(path, packed)
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")
    return result


def _is_rotated_log(name: str) -> bool:
    return (name.startswith("audit-spans-") or name.startswith("audit-events-")) and name.endswith(".log")


@dataclass
class LogCompressResult:
    """What one pass over the rotated logs saw, did, and saved."""

    files: int = 0
    compressed: int = 0
    already: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return max(self.bytes_before - self.bytes_after, 0)


def compress_logs(logs_dir: Path, *, dry_run: bool = False) -> LogCompressResult:
    """Compress rotated span and event logs in place.

    Only ``archive/`` is touched: the active log is still being appended to.
    ACP frame journals are left alone -- a span records a plaintext byte range
    into one, so re-encoding it would invalidate that audit record.

    A rotated log is closed and never appended to again, which is why it can be
    rewritten here and why a running writer needs no coordination. Idempotent:
    a compressed file is counted and left alone; a dry run writes nothing.
    """
    result = LogCompressResult()
    archive = Path(logs_dir) / "archive"
    if not archive.is_dir():
        return result
    for path in sorted(p for p in _files(archive) if _is_rotated_log(p.name)):
        result.files += 1
        try:
            data = path.read_bytes()
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")
            continue
        if is_compressed(data):
            result.already += 1
            continue
        packed = compress_bytes(data)
        result.bytes_before += len(data)
        result.bytes_after += len(packed)
        result.compressed += 1
        if dry_run:
            continue
        try:
            _replace(path, packed)
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")
    return result


__all__ = ["CompressResult", "LogCompressResult", "compress", "compress_logs"]
