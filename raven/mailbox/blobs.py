"""Verified content-addressed artifacts serialized with mailbox admission and GC."""

import errno
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from raven.contracts.mailbox import MailboxError
from raven.mailbox.codec import envelope_digest
from raven.utils.portable_lock import LockTimeoutError, file_lock

_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _io_error(exc: OSError) -> MailboxError:
    if exc.errno == errno.ENOSPC:
        return MailboxError("storage_full")
    return MailboxError("storage_permission" if isinstance(exc, PermissionError) else "storage_error")


def _sync_directory(path: Path) -> None:
    if sys.platform.startswith("linux"):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def initialize(db) -> None:
    """Create artifact directories only during explicit mailbox initialization."""
    try:
        for folder in (db.root / "blobs", db.root / "blobs" / "sha256"):
            if folder.is_symlink():
                raise MailboxError("unsafe_storage_path")
            folder.mkdir(mode=0o700, exist_ok=True)
            if not folder.is_dir():
                raise MailboxError("unsafe_storage_path")
            _sync_directory(folder.parent)
        anchor = db.root / ".mailbox-blobs.lock"
        if anchor.is_symlink():
            raise MailboxError("unsafe_storage_path")
        fd = os.open(anchor, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        _sync_directory(db.root)
    except OSError as exc:
        raise _io_error(exc) from None


@contextmanager
def locked(db):
    """Hold the permanent artifact anchor before any short DB write transaction."""
    with db.connection():
        pass
    for path in (db.root / "blobs", db.root / "blobs" / "sha256", db.root / ".mailbox-blobs.lock"):
        if path.is_symlink() or not path.exists():
            raise MailboxError("unsafe_storage_path")
    deadline = time.monotonic() + db.busy_timeout_ms / 1000
    while True:
        lock = file_lock(db.root / ".mailbox-blobs.lock", blocking=False)
        try:
            lock.__enter__()
            break
        except LockTimeoutError:
            if time.monotonic() >= deadline:
                raise MailboxError("lock_busy", retryable=True) from None
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        except OSError as exc:
            raise _io_error(exc) from None
    try:
        yield
    except OSError as exc:
        raise _io_error(exc) from None
    finally:
        lock.__exit__(None, None, None)


def _open_regular(path: Path, code: str):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOENT, errno.EISDIR):
            raise MailboxError(code) from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise MailboxError(code)
    return os.fdopen(fd, "rb")


def _verify(path: Path, artifact, code: str) -> None:
    with _open_regular(path, code) as source:
        if os.fstat(source.fileno()).st_size != artifact.size:
            raise MailboxError(code)
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != artifact.sha256:
        raise MailboxError(code)


def verify_artifacts(root: Path, envelope) -> None:
    """Block consumers from receiving immutable envelopes with missing or corrupt blobs."""
    try:
        for artifact in envelope.artifacts:
            _verify(root / "blobs" / "sha256" / artifact.sha256, artifact, "storage_conflict")
    except (MailboxError, OSError):
        raise MailboxError("storage_conflict", message_id=envelope.message_id) from None


def import_artifacts(db, envelope, sources) -> None:
    """Verify explicit local sources and publish blobs while the caller holds the lock."""
    folder = db.root / "blobs" / "sha256"
    with db.connection() as conn:
        budget = int(conn.execute("SELECT value FROM metadata WHERE key='blob_bytes'").fetchone()[0])
    usage = sum(path.stat().st_size for path in folder.iterdir())
    usage += sum(path.stat().st_size for path in db.root.glob(".blob-import-*"))
    for artifact in envelope.artifacts:
        destination = folder / artifact.sha256
        source_path = sources.get(artifact.sha256)
        if source_path is None:
            if not destination.exists() and not destination.is_symlink():
                raise MailboxError("artifacts_unavailable")
            _verify(destination, artifact, "storage_conflict")
            continue
        temporary = None
        try:
            with _open_regular(Path(source_path), "unsafe_artifact_path") as source:
                if os.fstat(source.fileno()).st_size != artifact.size:
                    raise MailboxError("artifact_mismatch")
                if usage + artifact.size > budget:
                    raise MailboxError("quota_exceeded")
                fd, name = tempfile.mkstemp(prefix=".blob-import-", dir=db.root)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as output:
                    digest = hashlib.sha256()
                    copied = 0
                    while chunk := source.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > artifact.size:
                            raise MailboxError("artifact_mismatch")
                        output.write(chunk)
                        digest.update(chunk)
                    if copied != artifact.size or digest.hexdigest() != artifact.sha256:
                        raise MailboxError("artifact_mismatch")
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    _verify(destination, artifact, "storage_conflict")
                else:
                    usage += artifact.size
        finally:
            if temporary is not None:
                temporary.unlink()
    _sync_directory(folder)


def collect(db, *, now: int) -> list[str]:
    """Remove only aged orphan hashes; retained envelopes and receipt refs are authoritative."""
    with locked(db):
        referenced = set()
        with db.connection() as conn:
            try:
                for row in conn.execute(
                    "SELECT envelope_bytes,digest FROM messages UNION ALL "
                    "SELECT envelope_bytes,digest FROM receipt_outbox WHERE envelope_bytes IS NOT NULL"
                ):
                    envelope = json.loads(row[0])
                    if type(envelope) is not dict or envelope_digest(envelope) != row[1]:
                        raise MailboxError("storage_conflict")
                    referenced.update(item["sha256"] for item in envelope["artifacts"])
                for row in conn.execute("SELECT result_json FROM messages WHERE result_json IS NOT NULL"):
                    referenced.update(json.loads(row[0])["evidence"])
                for row in conn.execute(
                    "SELECT artifact_refs FROM receipt_outbox UNION ALL SELECT artifact_refs FROM tombstones"
                ):
                    referenced.update(json.loads(row[0]))
                if conn.execute("PRAGMA user_version").fetchone()[0] >= 3:
                    for row in conn.execute(
                        "SELECT artifact_refs FROM strict_roots UNION ALL "
                        "SELECT artifact_refs FROM strict_attempts UNION ALL "
                        "SELECT artifact_refs FROM strict_dispatch_outbox"
                    ):
                        referenced.update(json.loads(row[0]))
            except (ValueError, KeyError, TypeError):
                raise MailboxError("storage_conflict") from None
        deleted = []
        folder = db.root / "blobs" / "sha256"
        for path in folder.iterdir():
            info = path.lstat()
            if (
                _HASH.fullmatch(path.name)
                and stat.S_ISREG(info.st_mode)
                and path.name not in referenced
                and info.st_mtime < now - 86400
            ):
                path.unlink()
                deleted.append(path.name)
        if deleted:
            _sync_directory(folder)
        return sorted(deleted)
