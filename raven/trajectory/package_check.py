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
header is) and the total output, and refusing to go past either limit; the
chain of extended headers tarfile holds at once before a member is bounded
as well, so no header shape can make the walk recurse or hold more than a
few reads.

Checks run in archive order — size, gzip/tar framing, each member's type and
name, the metadata file, the manifest against the hashed members, the
redaction invariant — and the first failure is raised as
:class:`PackageError` with a stable ``code``.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import tarfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from raven.trajectory.bugreport import (
    CLASSIFICATION_CLEAN,
    CLASSIFICATION_NEEDS_REVIEW,
    PACKAGE_METADATA_FILE,
    PACKAGE_SCHEMA,
    SCHEMA_VERSION,
    is_report_id,
)
from raven.trajectory.sanitize import find_absolute_paths_in_json

CODE_TOO_LARGE = "too_large"
CODE_NOT_GZIP_TAR = "not_gzip_tar"
CODE_HEADER_TOO_LARGE = "header_too_large"
CODE_HEADER_CHAIN_TOO_LONG = "header_chain_too_long"
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
_PLAIN_FILE_TYPES = (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.CONTTYPE)
_SPARSE_PREFIX = "GNU.sparse."
_EXTENDED_TYPES = (
    tarfile.XHDTYPE,
    tarfile.XGLTYPE,
    tarfile.SOLARIS_XHDTYPE,
    tarfile.GNUTYPE_LONGNAME,
    tarfile.GNUTYPE_LONGLINK,
)
# A PAX archive carries one extended header per member; a global one, a long
# name and a long link on top of it is already more than any writer emits.
_MAX_EXTENDED_HEADERS = 4
MAX_NOTICES = 16


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


class InspectionStoppedError(Exception):
    """Raised inside :func:`inspect_package` once its ``should_stop`` hook returns true."""


class _BoundedGunzip(io.RawIOBase):
    """A forward-only view of a gzip stream's output with hard volume limits.

    ``max_read`` bounds one call: tarfile reads headers in 512-byte blocks and
    :func:`inspect_package` reads member data in pieces no larger than it, so
    the only bigger request a well-formed archive can make is a PAX/GNU
    extended header — the per-call cap *is* the extended-header cap, and it
    is checked before any buffer proportional to the request exists. A chain
    of such headers is held at once by tarfile; :class:`_StrictTarInfo`
    bounds its length, so the headers alive together stay within a few caps.
    ``max_output`` bounds everything ever produced — headers, data, padding
    and skipped bytes alike — and is checked before decompressing.
    """

    def __init__(
        self, raw: BinaryIO, *, max_output: int, max_read: int, should_stop: Callable[[], bool] | None = None
    ) -> None:
        self._raw = raw
        self._max_output = max_output
        self._max_read = max_read
        self._should_stop = should_stop
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
        if self._should_stop is not None and self._should_stop():
            raise InspectionStoppedError("inspection stopped")
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


def _no_sparse() -> PackageError:
    return PackageError(CODE_BAD_MEMBER_TYPE, "sparse members are not supported")


class _StrictTarInfo(tarfile.TarInfo):
    """A TarInfo that refuses sparse members before tarfile builds their maps.

    Every sparse flavour (old GNU type ``S``, PAX ``GNU.sparse.*`` 0.0/0.1
    and 1.0) is expanded by tarfile *before* the member is handed back, and
    a 1.0 map is read from the data stream in 512-byte pieces into a list
    whose size the per-read cap cannot see. A bug report never carries a
    sparse file, so each handler is cut off at its entry.

    Extended headers (PAX ``x``/``g``, GNU ``L``/``K``) each describe the
    header after them, which tarfile fetches by recursing once per header
    and holding every header of the chain at once. The chain before a member
    is cut off at ``_MAX_EXTENDED_HEADERS``, so neither the recursion depth
    nor the memory held grows with what the archive declares.
    """

    def _proc_member(self, tar: Any) -> Any:
        if self.type in _EXTENDED_TYPES:
            tar.extended_headers += 1
            if tar.extended_headers > _MAX_EXTENDED_HEADERS:
                raise PackageError(
                    CODE_HEADER_CHAIN_TOO_LONG, f"more than {_MAX_EXTENDED_HEADERS} extended headers before a member"
                )
        else:
            tar.extended_headers = 0
        return super()._proc_member(tar)

    def _proc_sparse(self, *args: Any) -> Any:
        raise _no_sparse()

    def _proc_gnusparse_00(self, *args: Any) -> None:
        raise _no_sparse()

    def _proc_gnusparse_01(self, *args: Any) -> None:
        raise _no_sparse()

    def _proc_gnusparse_10(self, *args: Any) -> None:
        raise _no_sparse()

    def _apply_pax_info(self, pax_headers: Any, encoding: Any, errors: Any) -> None:
        if any(key.startswith(_SPARSE_PREFIX) for key in pax_headers):
            raise _no_sparse()
        super()._apply_pax_info(pax_headers, encoding, errors)


class _StrictTarFile(tarfile.TarFile):
    """A TarFile that reads :class:`_StrictTarInfo` headers and counts the chain they form."""

    tarinfo = _StrictTarInfo
    extended_headers = 0


@contextlib.contextmanager
def _tar_boundary() -> Iterator[None]:
    """Turn what tarfile raises on malformed input into a :class:`PackageError`.

    ``RecursionError`` is a ``RuntimeError``, not a ``TarError``: the chain
    bound keeps tarfile from reaching it, and this keeps the contract if some
    other header shape ever does.
    """
    try:
        yield
    except PackageError:
        raise
    except (tarfile.TarError, EOFError, ValueError, RecursionError) as exc:
        raise PackageError(CODE_NOT_GZIP_TAR, str(exc) or exc.__class__.__name__) from exc


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


def _read_member(handle: Any, chunk: int, *, keep: bool) -> tuple[bytes, str, int]:
    pieces = bytearray()
    digest = hashlib.sha256()
    count = 0
    while True:
        with _tar_boundary():
            piece = handle.read(chunk)
        if not piece:
            break
        if keep:
            pieces += piece
        digest.update(piece)
        count += len(piece)
    return bytes(pieces), digest.hexdigest(), count


def _members(tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    iterator = iter(tar)
    while True:
        with _tar_boundary():
            member = next(iterator, None)
        if member is None:
            return
        yield member


def _walk(stream: _BoundedGunzip, limits: PackageLimits) -> _Walk:
    chunk = min(_CHUNK, limits.max_header_bytes)
    with _tar_boundary():
        tar = _StrictTarFile(fileobj=stream, mode="r")
    walk = _Walk()
    seen: set[str] = set()
    for member in _members(tar):
        if len(walk.names) >= limits.max_members:
            raise PackageError(CODE_TOO_MANY_MEMBERS, f"more than {limits.max_members} members")
        if not (member.type in _PLAIN_FILE_TYPES or member.isdir()) or member.sparse is not None:
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
            walk.metadata, _, count = _read_member(handle, chunk, keep=True)
        else:
            _, sha256, count = _read_member(handle, chunk, keep=False)
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
    except (ValueError, RecursionError) as exc:
        # UnicodeDecodeError and JSONDecodeError, the plain ValueError the
        # decoder raises for an integer longer than its digit limit, and the
        # RecursionError it raises for nesting deeper than the interpreter
        # allows: all of them describe the sender's bytes, not this process.
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
    hits = find_absolute_paths_in_json(metadata, limit=MAX_NOTICES + 1)
    notices = tuple(
        f"absolute path in {PACKAGE_METADATA_FILE} {where[:120]}: {sample[:80]}" for where, sample in hits[:MAX_NOTICES]
    )
    if len(hits) > MAX_NOTICES:
        notices += (f"more than {MAX_NOTICES} absolute paths in {PACKAGE_METADATA_FILE}; only the first are listed",)
    return PackageSummary(report_id, metadata, tuple(walk.names), notices)


def inspect_package(
    path: Path,
    *,
    limits: PackageLimits | None = None,
    expected_report_id: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> PackageSummary:
    """Validate the package at ``path`` and describe it, or raise :class:`PackageError`.

    The archive is read once, front to back, without extracting anything;
    every member's bytes are hashed in passing and compared against the
    manifest inside ``bugreport.json``. ``expected_report_id`` pins the
    package to the id a caller was told to expect. ``should_stop`` is polled
    before every read; once it returns true the walk ends with
    :class:`InspectionStoppedError`, which is how a caller running this in a
    worker thread stops it cooperatively.
    """
    limits = limits or PackageLimits()
    size = path.stat().st_size
    if size > limits.max_package_bytes:
        raise PackageError(CODE_TOO_LARGE, f"package is {size} bytes; the limit is {limits.max_package_bytes}")
    if size == 0:
        raise PackageError(CODE_NOT_GZIP_TAR, "empty file")
    with path.open("rb") as raw:
        stream = _BoundedGunzip(
            raw, max_output=limits.max_unpacked_bytes, max_read=limits.max_header_bytes, should_stop=should_stop
        )
        walk = _walk(stream, limits)
    return _check_metadata(walk, expected_report_id)


__all__ = [
    "InspectionStoppedError",
    "PackageError",
    "PackageLimits",
    "PackageSummary",
    "inspect_package",
]
