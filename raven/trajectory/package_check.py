"""Bug Report Package inspection: structural validation without extraction.

The receiving side of a bug report cannot trust the archive it was handed —
the format is public, so a package may be crafted. This module answers "is
this a well-formed Bug Report Package whose content matches its own
manifest" by streaming through the archive once: nothing is extracted to
disk, member bytes are hashed as they pass, and the decompressed volume is
bounded *before* it is produced.

The bounded decompression layer exists because the standard library offers
no safe path here: ``tarfile.open("r:gz")`` and ``"r|gz"`` both hand a
PAX/GNU extended header's declared size straight to one ``read()`` call, so
a header claiming gigabytes is decompressed whole into memory before the
first member is ever returned. :class:`_BoundedGunzip` sits between ``zlib``
and ``tarfile`` instead, capping each single read (which is what an extended
header is) and the total output, and refusing to go past either limit.

Checks run in archive order — size, gzip/tar framing, each member's type and
name, the metadata file, the manifest against the hashed members, the
redaction invariant — and the first failure is raised as
:class:`PackageError` with a stable ``code``.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from raven.trajectory.bugreport import (
    CLASSIFICATION_CLEAN,
    CLASSIFICATION_NEEDS_REVIEW,
    PACKAGE_METADATA_FILE,
    PACKAGE_SCHEMA,
    SCHEMA_VERSION,
    is_report_id,
)
from raven.trajectory.sanitize import find_absolute_paths

CODE_TOO_LARGE = "too_large"
CODE_NOT_GZIP_TAR = "not_gzip_tar"
CODE_HEADER_TOO_LARGE = "header_too_large"
CODE_UNPACKED_TOO_LARGE = "unpacked_too_large"
CODE_TOO_MANY_MEMBERS = "too_many_members"
CODE_BAD_MEMBER_TYPE = "bad_member_type"
CODE_BAD_MEMBER_NAME = "bad_member_name"
CODE_DUPLICATE_MEMBER = "duplicate_member"
CODE_METADATA_MISSING = "metadata_missing"
CODE_METADATA_TOO_LARGE = "metadata_too_large"
CODE_METADATA_INVALID = "metadata_invalid"
CODE_SCHEMA_MISMATCH = "schema_mismatch"
CODE_REPORT_ID_MISMATCH = "report_id_mismatch"
CODE_CONTENTS_INVALID = "contents_invalid"
CODE_CONTENTS_MISMATCH = "contents_mismatch"
CODE_RISK_NOT_ACCEPTED = "risk_not_accepted"

_CHUNK = 64 * 1024
_END_BLOCK = b"\0" * tarfile.BLOCKSIZE
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_SEGMENTS = ("", ".", "..")


@dataclass(frozen=True)
class PackageLimits:
    max_package_bytes: int = 64 * 2**20
    max_unpacked_bytes: int = 256 * 2**20
    max_header_bytes: int = 64 * 1024
    max_members: int = 32
    max_metadata_bytes: int = 4 * 2**20


@dataclass(frozen=True)
class PackageSummary:
    report_id: str
    metadata: dict[str, Any]
    members: tuple[str, ...]
    notices: tuple[str, ...]


class PackageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _BoundedGunzip(io.RawIOBase):
    """A forward-only view of a gzip stream's output with hard volume limits.

    ``max_read`` bounds one call: tarfile reads headers in 512-byte blocks and
    :func:`inspect_package` reads member data in pieces no larger than it, so
    the only bigger request a well-formed archive can make is a PAX/GNU
    extended header — the per-call cap *is* the extended-header cap, and it
    is checked before any buffer proportional to the request exists.
    ``max_output`` bounds everything ever produced — headers, data, padding
    and skipped bytes alike — and is checked before decompressing.
    """

    def __init__(self, raw: BinaryIO, *, max_output: int, max_read: int) -> None:
        self._raw = raw
        self._max_output = max_output
        self._max_read = max_read
        self._decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self._buffer = bytearray()
        self._produced = 0
        self._tail_checked = False
        self.last_read = b""

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def _fill(self, want: int) -> None:
        while len(self._buffer) < want and not self._decompressor.eof:
            data = self._decompressor.unconsumed_tail or self._raw.read(_CHUNK)
            if not data:
                raise PackageError(CODE_NOT_GZIP_TAR, "truncated gzip stream")
            try:
                self._buffer += self._decompressor.decompress(data, want - len(self._buffer))
            except zlib.error as exc:
                raise PackageError(CODE_NOT_GZIP_TAR, f"invalid gzip data: {exc}") from exc
        if self._decompressor.eof and not self._tail_checked:
            self._tail_checked = True
            if self._decompressor.unused_data.strip(b"\0") or self._raw.read(1):
                raise PackageError(CODE_NOT_GZIP_TAR, "trailing data after gzip stream")

    def _take(self, size: int, *, record: bool = True) -> bytes:
        if size > self._max_read:
            raise PackageError(
                CODE_HEADER_TOO_LARGE, f"archive header of {size} bytes exceeds the {self._max_read}-byte limit"
            )
        allowed = self._max_output - self._produced
        if allowed <= 0:
            raise PackageError(CODE_UNPACKED_TOO_LARGE, f"archive expands past the {self._max_output}-byte limit")
        want = min(size, allowed)
        self._fill(want)
        out = bytes(self._buffer[:want])
        del self._buffer[:want]
        self._produced += len(out)
        if len(out) == allowed and allowed < size and (self._buffer or not self._decompressor.eof):
            raise PackageError(CODE_UNPACKED_TOO_LARGE, f"archive expands past the {self._max_output}-byte limit")
        if record:
            self.last_read = out
        return out

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("unbounded read")
        return self._take(size)

    def readinto(self, buffer: Any) -> int:
        out = self._take(len(buffer))
        buffer[: len(out)] = out
        return len(out)

    def tell(self) -> int:
        return self._produced

    def seek(self, pos: int, whence: int = io.SEEK_SET) -> int:
        if whence != io.SEEK_SET:
            raise ValueError("only absolute seeks are supported")
        if pos < self._produced:
            raise PackageError(CODE_NOT_GZIP_TAR, "backward seek in a sequential archive read")
        while self._produced < pos:
            if not self._take(min(_CHUNK, pos - self._produced), record=False):
                raise PackageError(CODE_NOT_GZIP_TAR, "archive ends inside member padding")
        return self._produced

    def finish(self) -> None:
        # zlib only verifies the gzip trailer (CRC32, length) once the whole
        # stream has been consumed, and tarfile stops reading at the first
        # end-of-archive block; whatever follows it is otherwise never seen.
        while self._buffer or not self._decompressor.eof:
            chunk = self._take(_CHUNK, record=False)
            if not chunk:
                break
            if chunk.strip(b"\0"):
                raise PackageError(CODE_NOT_GZIP_TAR, "data after the end-of-archive marker")


@dataclass
class _Walk:
    root: str = ""
    names: list[str] = field(default_factory=list)
    files: dict[str, tuple[str, int]] = field(default_factory=dict)
    metadata: bytes | None = None


def _segments(name: str) -> list[str]:
    segments = name.split("/")
    if name.startswith("/") or "\\" in name or any(part in _UNSAFE_SEGMENTS for part in segments):
        raise PackageError(CODE_BAD_MEMBER_NAME, f"unsafe member name: {name!r}")
    return segments


def _safe_relative(path: Any) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    return not any(part in _UNSAFE_SEGMENTS for part in path.split("/"))


def _read_member(handle: Any, chunk: int) -> tuple[bytes, str, int]:
    pieces = bytearray()
    digest = hashlib.sha256()
    count = 0
    while True:
        piece = handle.read(chunk)
        if not piece:
            break
        pieces += piece
        digest.update(piece)
        count += len(piece)
    return bytes(pieces), digest.hexdigest(), count


def _hash_member(handle: Any, chunk: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    while True:
        piece = handle.read(chunk)
        if not piece:
            break
        digest.update(piece)
        count += len(piece)
    return digest.hexdigest(), count


def _walk(stream: _BoundedGunzip, limits: PackageLimits) -> _Walk:
    chunk = min(_CHUNK, limits.max_header_bytes)
    tar = tarfile.TarFile(fileobj=stream, mode="r")
    walk = _Walk()
    seen: set[str] = set()
    for member in tar:
        if len(walk.names) >= limits.max_members:
            raise PackageError(CODE_TOO_MANY_MEMBERS, f"more than {limits.max_members} members")
        if not (member.isreg() or member.isdir()):
            raise PackageError(CODE_BAD_MEMBER_TYPE, f"unsupported member type {member.type!r}: {member.name!r}")
        name = member.name
        segments = _segments(name)
        if not walk.names:
            walk.root = segments[0]
            if not is_report_id(walk.root):
                raise PackageError(CODE_BAD_MEMBER_NAME, f"archive root is not a report id: {walk.root!r}")
        elif segments[0] != walk.root:
            raise PackageError(CODE_BAD_MEMBER_NAME, f"member outside the archive root: {name!r}")
        if name in seen:
            raise PackageError(CODE_DUPLICATE_MEMBER, f"duplicate member: {name!r}")
        seen.add(name)
        walk.names.append(name)
        if member.isdir():
            # tarfile only skips the data of regular files and unknown types:
            # bytes after a directory header are parsed as the next header,
            # and a bad header there silently ends the archive.
            if member.size != 0:
                raise PackageError(CODE_BAD_MEMBER_TYPE, f"directory entry carries data: {name!r}")
            continue
        if len(segments) == 1:
            raise PackageError(CODE_BAD_MEMBER_NAME, f"a file may not use the root directory name: {name!r}")
        rel = "/".join(segments[1:])
        if member.size > limits.max_unpacked_bytes - stream.tell():
            raise PackageError(
                CODE_UNPACKED_TOO_LARGE,
                f"member {name!r} declares {member.size} bytes, past the {limits.max_unpacked_bytes}-byte archive limit",
            )
        handle = tar.extractfile(member)
        if rel == PACKAGE_METADATA_FILE:
            if member.size > limits.max_metadata_bytes:
                raise PackageError(
                    CODE_METADATA_TOO_LARGE,
                    f"{PACKAGE_METADATA_FILE} is {member.size} bytes; the limit is {limits.max_metadata_bytes}",
                )
            walk.metadata, _, count = _read_member(handle, chunk)
        else:
            sha256, count = _hash_member(handle, chunk)
            walk.files[rel] = (sha256, count)
        if count != member.size:
            raise PackageError(CODE_NOT_GZIP_TAR, f"member {name!r} is shorter than declared")
    if stream.last_read != _END_BLOCK:
        raise PackageError(CODE_NOT_GZIP_TAR, "archive is not terminated by an end-of-archive block")
    stream.finish()
    return walk


def _validate_contents(contents: Any) -> list[dict[str, Any]]:
    if not isinstance(contents, list):
        raise PackageError(CODE_CONTENTS_INVALID, "contents is not a list")
    seen: set[str] = set()
    for entry in contents:
        if not isinstance(entry, dict):
            raise PackageError(CODE_CONTENTS_INVALID, "contents entry is not an object")
        path = entry.get("path")
        size = entry.get("size_bytes")
        if not _safe_relative(path):
            raise PackageError(CODE_CONTENTS_INVALID, f"contents entry has an unsafe path: {path!r}")
        if not isinstance(entry.get("sha256"), str) or type(size) is not int or size < 0:
            raise PackageError(CODE_CONTENTS_INVALID, f"contents entry is malformed: {path!r}")
        if path in seen:
            raise PackageError(CODE_CONTENTS_INVALID, f"contents lists {path!r} twice")
        seen.add(path)
    return contents


def _check_metadata(walk: _Walk, expected_report_id: str | None) -> PackageSummary:
    if walk.metadata is None:
        raise PackageError(CODE_METADATA_MISSING, f"{PACKAGE_METADATA_FILE} is missing")
    try:
        text = walk.metadata.decode("utf-8")
        metadata = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(CODE_METADATA_INVALID, f"{PACKAGE_METADATA_FILE} is not UTF-8 JSON: {exc}") from exc
    if not isinstance(metadata, dict):
        raise PackageError(CODE_METADATA_INVALID, f"{PACKAGE_METADATA_FILE} is not a JSON object")
    schema = metadata.get("schema")
    version = metadata.get("schema_version")
    if schema != PACKAGE_SCHEMA or type(version) is not int or version != SCHEMA_VERSION:
        raise PackageError(
            CODE_SCHEMA_MISMATCH,
            f"expected {PACKAGE_SCHEMA} version {SCHEMA_VERSION}, got {schema!r} version {version!r}",
        )
    report_id = metadata.get("report_id")
    if not is_report_id(report_id) or report_id != walk.root:
        raise PackageError(CODE_REPORT_ID_MISMATCH, f"metadata names {report_id!r}, the archive root is {walk.root!r}")
    if expected_report_id is not None and report_id != expected_report_id:
        raise PackageError(CODE_REPORT_ID_MISMATCH, f"package is {report_id!r}, expected {expected_report_id!r}")
    contents = _validate_contents(metadata.get("contents"))
    expected = {entry["path"] for entry in contents}
    actual = set(walk.files) | {PACKAGE_METADATA_FILE}
    if expected != actual:
        raise PackageError(
            CODE_CONTENTS_MISMATCH,
            f"manifest mismatch: extra={sorted(actual - expected)} missing={sorted(expected - actual)}",
        )
    for entry in contents:
        if entry["path"] == PACKAGE_METADATA_FILE:
            continue
        sha256, size = walk.files[entry["path"]]
        if not _SHA256_HEX.fullmatch(entry["sha256"]) or entry["sha256"] != sha256 or entry["size_bytes"] != size:
            raise PackageError(CODE_CONTENTS_MISMATCH, f"manifest entry does not match the archive: {entry['path']!r}")
    redaction = metadata.get("redaction")
    if not isinstance(redaction, dict):
        raise PackageError(CODE_RISK_NOT_ACCEPTED, "redaction block missing")
    classification = redaction.get("classification")
    if classification not in (CLASSIFICATION_CLEAN, CLASSIFICATION_NEEDS_REVIEW):
        raise PackageError(CODE_RISK_NOT_ACCEPTED, f"unknown redaction classification {classification!r}")
    if classification == CLASSIFICATION_NEEDS_REVIEW and redaction.get("risk_accepted") is not True:
        raise PackageError(CODE_RISK_NOT_ACCEPTED, "a needs_review package must carry risk_accepted: true")
    notices = tuple(
        f"absolute path in {PACKAGE_METADATA_FILE}: {text[start:end][:80]}" for start, end in find_absolute_paths(text)
    )
    return PackageSummary(report_id, metadata, tuple(walk.names), notices)


def inspect_package(
    path: Path, *, limits: PackageLimits | None = None, expected_report_id: str | None = None
) -> PackageSummary:
    """Validate the package at ``path`` and describe it, or raise :class:`PackageError`.

    The archive is read once, front to back, without extracting anything;
    every member's bytes are hashed in passing and compared against the
    manifest inside ``bugreport.json``. ``expected_report_id`` pins the
    package to the id a caller was told to expect.
    """
    limits = limits or PackageLimits()
    size = path.stat().st_size
    if size > limits.max_package_bytes:
        raise PackageError(CODE_TOO_LARGE, f"package is {size} bytes; the limit is {limits.max_package_bytes}")
    if size == 0:
        raise PackageError(CODE_NOT_GZIP_TAR, "empty file")
    with path.open("rb") as raw:
        stream = _BoundedGunzip(raw, max_output=limits.max_unpacked_bytes, max_read=limits.max_header_bytes)
        try:
            walk = _walk(stream, limits)
        except PackageError:
            raise
        except (tarfile.TarError, EOFError) as exc:
            raise PackageError(CODE_NOT_GZIP_TAR, str(exc) or exc.__class__.__name__) from exc
    return _check_metadata(walk, expected_report_id)


__all__ = [
    "PackageError",
    "PackageLimits",
    "PackageSummary",
    "inspect_package",
]
