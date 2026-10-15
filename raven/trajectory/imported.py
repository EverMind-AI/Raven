"""Imported Conversations: packages read back in and kept as read-only conversations.

A package a page uploads -- a Trajectory Export, a Bug Report Package or a
Trajectory Report -- is unpacked and normalized (:mod:`raven.trajectory.imported_package`)
into its own directory under the raven home::

    <raven home>/imports/<id>/
      meta.json        # what it is, where it came from, its title
      package/<root>/  # the package as unpacked (a report's inner bundle expanded in place)
      state/logs/...   # the trace store the trajectory index reads
      session.jsonl    # the session record, when the package carried one

and is addressed as ``imported:<id>`` (32 lowercase hex). It never enters the
SessionManager or ``<workspace>/sessions/``, so nothing that picks or sweeps
sessions -- delivery targets, auto-archive, the session file scans -- can see
it.

An import is built in ``.staging/<id>/`` and renamed into place whole, so a
crash or a refusal leaves nothing that lists; a delete renames the directory
aside before removing it, so a half-deleted one never lists either. Imports
run one at a time on a thread of their own (:class:`ImportRunner`): the slot is
released by that thread when it ends, not by the caller that asked, so a
caller that stops waiting cannot let a second import race the first through
the duplicate check.
"""

from __future__ import annotations

import base64
import binascii
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from raven.home import raven_home
from raven.session.manager import IMPORTED_CHANNEL, Session, read_session_file
from raven.session.title import TITLE_STORAGE_MAX, collapse_to_line
from raven.trajectory import imported_package as ipkg
from raven.trajectory.imported_package import ImportCancelledError, ImportLimits
from raven.trajectory.package_check import CODE_NOT_GZIP_TAR, CODE_TOO_LARGE, PackageError

SCHEMA = "raven_imported_conversation"
FORMAT_VERSION = 1
CHANNEL = IMPORTED_CHANNEL
KEY_PREFIX = f"{CHANNEL}:"
IMPORTS_DIR = "imports"
STAGING_DIR = ".staging"
TRASH_PREFIX = ".trash-"
META_FILE = "meta.json"
SESSION_FILE = ipkg.SESSION_FILE
PACKAGE_DIR = "package"
UPLOAD_FILE = "upload.tar.gz"
DEFAULT_SOURCE_NAME = "package.tar.gz"

STATUS_IMPORTED = "imported"
STATUS_DUPLICATE = "duplicate"

_ID = re.compile(r"^[0-9a-f]{32}$")
_KEY = re.compile(r"^imported:([0-9a-f]{32})$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_STALE_STAGING_SECONDS = 24 * 3600


def imports_root() -> Path:
    return raven_home() / IMPORTS_DIR


def is_imported_key(value: Any) -> bool:
    """Whether ``value`` claims to address an Imported Conversation, well-formed or not."""
    return isinstance(value, str) and value.startswith(KEY_PREFIX)


def parse_key(key: Any) -> str | None:
    match = _KEY.fullmatch(key) if isinstance(key, str) else None
    return match.group(1) if match else None


def key_of(conversation_id: str) -> str:
    return f"{KEY_PREFIX}{conversation_id}"


def _root(root: Path | None) -> Path:
    return root if root is not None else imports_root()


@dataclass(frozen=True)
class ImportedConversation:
    id: str
    path: Path
    meta: dict[str, Any]

    @property
    def key(self) -> str:
        return key_of(self.id)

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or "")

    @property
    def kind(self) -> str:
        return str(self.meta.get("kind") or "")

    @property
    def source_name(self) -> str:
        return str(self.meta.get("source_name") or "")

    @property
    def imported_at(self) -> str:
        return str(self.meta.get("imported_at") or "")

    @property
    def message_count(self) -> int:
        count = self.meta.get("message_count")
        return count if type(count) is int and count >= 0 else 0

    @property
    def original_session_key(self) -> str | None:
        original = self.meta.get("original")
        key = original.get("session_key") if isinstance(original, dict) else None
        return key if isinstance(key, str) and key else None


@dataclass(frozen=True)
class ImportOutcome:
    status: str
    conversation: ImportedConversation


def _read_meta(path: Path, conversation_id: str) -> dict[str, Any] | None:
    try:
        meta = json.loads((path / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        logger.warning("imported conversation {}: meta.json unreadable: {}", conversation_id, exc)
        return None
    if (
        not isinstance(meta, dict)
        or meta.get("schema") != SCHEMA
        or meta.get("format_version") != FORMAT_VERSION
        or meta.get("id") != conversation_id
    ):
        logger.warning("imported conversation {}: meta.json is not a {} v{}", conversation_id, SCHEMA, FORMAT_VERSION)
        return None
    return meta


def _load(path: Path) -> ImportedConversation | None:
    meta = _read_meta(path, path.name)
    return ImportedConversation(path.name, path, meta) if meta is not None else None


def list_conversations(root: Path | None = None) -> list[ImportedConversation]:
    """Every intact Imported Conversation, newest import first."""
    base = _root(root)
    try:
        entries = list(os.scandir(base))
    except OSError:
        return []
    out: list[ImportedConversation] = []
    for entry in entries:
        if not _ID.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
            continue
        loaded = _load(Path(entry.path))
        if loaded is not None:
            out.append(loaded)
    out.sort(key=lambda item: (item.imported_at, item.id), reverse=True)
    return out


def get(key: Any, root: Path | None = None) -> ImportedConversation | None:
    conversation_id = parse_key(key)
    if conversation_id is None:
        return None
    path = _root(root) / conversation_id
    if not path.is_dir() or path.is_symlink():
        return None
    return _load(path)


def find_by_sha256(sha256: str, root: Path | None = None) -> ImportedConversation | None:
    return next((item for item in list_conversations(root) if item.meta.get("sha256") == sha256), None)


def read_session(key: Any, root: Path | None = None) -> Session | None:
    conversation = get(key, root)
    if conversation is None:
        return None
    return read_session_file(conversation.path / SESSION_FILE, conversation.key)


def trajectory_target(key: Any, root: Path | None = None) -> tuple[Path, str | None] | None:
    """The trace store an Imported Conversation's trajectory is read from, and the key its spans carry."""
    conversation = get(key, root)
    if conversation is None:
        return None
    return conversation.path / ipkg.STATE_DIR, conversation.original_session_key


def _write_meta(path: Path, meta: dict[str, Any]) -> None:
    tmp = path / f".{META_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path / META_FILE)
    finally:
        tmp.unlink(missing_ok=True)


def rename(key: Any, title: str, root: Path | None = None) -> ImportedConversation:
    """Give an Imported Conversation a title of the reader's; the default is never derived again."""
    conversation = get(key, root)
    if conversation is None:
        raise LookupError(f"no imported conversation {key!r}")
    cleaned = collapse_to_line(title if isinstance(title, str) else "")
    if not cleaned:
        raise ValueError("session title is empty")
    if len(cleaned) > TITLE_STORAGE_MAX:
        raise ValueError(f"session title is {len(cleaned)} characters; the maximum is {TITLE_STORAGE_MAX}")
    meta = {**conversation.meta, "title": cleaned, "title_auto": False}
    _write_meta(conversation.path, meta)
    return ImportedConversation(conversation.id, conversation.path, meta)


def delete(key: Any, root: Path | None = None) -> bool:
    conversation = get(key, root)
    if conversation is None:
        return False
    trash = conversation.path.parent / f"{TRASH_PREFIX}{conversation.id}"
    os.rename(conversation.path, trash)
    shutil.rmtree(trash, ignore_errors=True)
    return True


def sweep(root: Path | None = None, *, now: Callable[[], float] = time.time) -> int:
    """Remove staging left over a day ago by an import that never finished, and every set-aside delete."""
    base = _root(root)
    removed = 0
    staging = base / STAGING_DIR
    cutoff = now() - _STALE_STAGING_SECONDS
    try:
        stale = [p for p in staging.iterdir() if p.lstat().st_mtime < cutoff]
    except OSError:
        stale = []
    try:
        trash = [p for p in base.iterdir() if p.name.startswith(TRASH_PREFIX)]
    except OSError:
        trash = []
    for path in stale + trash:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        removed += 1
    return removed


# ── import ─────────────────────────────────────────────────────────────


def clean_source_name(name: Any) -> str:
    base = re.split(r"[/\\]", name if isinstance(name, str) else "")[-1]
    cleaned = _CONTROL.sub("", base).strip()
    return cleaned or DEFAULT_SOURCE_NAME


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _first_line(value: Any) -> str:
    for line in _text(value).splitlines():
        collapsed = collapse_to_line(line)
        if collapsed:
            return collapsed[:TITLE_STORAGE_MAX]
    return ""


def _pick_title(*candidates: Any) -> str:
    for candidate in candidates:
        collapsed = collapse_to_line(_text(candidate))
        if collapsed:
            return collapsed[:TITLE_STORAGE_MAX]
    return ""


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _build_meta(
    conversation_id: str,
    source_name: str,
    data_sha256: str,
    size: int,
    unpacked: ipkg.Unpacked,
    normalized: ipkg.Normalized,
    transcript: Session | None,
) -> dict[str, Any]:
    metadata = unpacked.metadata
    record_title = _text((transcript.metadata or {}).get("title")) if transcript is not None else ""
    turns: int | None = None
    unmatched: list[str] = []
    report: dict[str, Any] | None = None
    if unpacked.kind == ipkg.KIND_EXPORT:
        source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
        listed = metadata.get("turns") if isinstance(metadata.get("turns"), list) else []
        first = listed[0] if listed and isinstance(listed[0], dict) else {}
        original_title = _text(source.get("title"))
        title = _pick_title(source.get("title"), first.get("input_preview"), metadata.get("export_id"))
        created_at = _text(metadata.get("created_at"))
        raven_version = _text(metadata.get("raven_version"))
        turns = len(listed)
        listed_unmatched = metadata.get("transcript_unmatched")
        unmatched = (
            [item for item in listed_unmatched if isinstance(item, str)] if isinstance(listed_unmatched, list) else []
        )
        missing = {
            "artifacts": _count(metadata.get("missing_artifacts")),
            "messages": _count(metadata.get("missing_messages")),
        }
    else:
        manifest = unpacked.manifest or {}
        original_title = record_title
        missing = {
            "artifacts": _count(manifest.get("missing_artifacts")),
            "messages": _count(manifest.get("missing_messages")),
        }
        if unpacked.kind == ipkg.KIND_BUGREPORT:
            problem = metadata.get("problem") if isinstance(metadata.get("problem"), dict) else {}
            first_line = _first_line(problem.get("description"))
            title = _pick_title(first_line, metadata.get("report_id"))
            created_at = _text(metadata.get("created_at"))
            raven_version = _text(metadata.get("raven_version"))
            report = {
                "report_id": _text(metadata.get("report_id")),
                "description_first_line": first_line,
                "severity": _text(problem.get("severity")),
            }
        else:
            title = _pick_title(record_title, manifest.get("attempt_id"), unpacked.root.name)
            raven_version = _text(manifest.get("raven_version"))
            try:
                redaction = json.loads((unpacked.root / ipkg.REDACTION_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                redaction = None
            created_at = _text(redaction.get("redacted_at")) if isinstance(redaction, dict) else ""
    return {
        "schema": SCHEMA,
        "format_version": FORMAT_VERSION,
        "id": conversation_id,
        "title": title or unpacked.root.name,
        "title_auto": True,
        "kind": unpacked.kind,
        "source_name": source_name,
        "sha256": data_sha256,
        "size_bytes": size,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "original": {
            "session_key": normalized.original_session_key,
            "title": original_title,
            "raven_version": raven_version,
            "created_at": created_at,
        },
        "message_count": len(transcript.messages) if transcript is not None else 0,
        "transcript_included": normalized.transcript_included,
        "turns": turns,
        "transcript_unmatched": unmatched,
        "missing": missing,
        "converted_shells": normalized.converted_shells,
        "skipped_shells": normalized.skipped_shells,
        "report": report,
    }


def _never() -> bool:
    return False


def import_bytes(
    data: bytes,
    source_name: str,
    *,
    force: bool = False,
    root: Path | None = None,
    limits: ImportLimits | None = None,
    should_stop: Callable[[], bool] = _never,
) -> ImportOutcome:
    """Import one package's bytes, or find the import of the same bytes; raise :class:`PackageError` when refused."""
    limits = limits or ImportLimits()
    if not data:
        raise PackageError(CODE_NOT_GZIP_TAR, "empty file")
    if len(data) > limits.max_package_bytes:
        raise PackageError(CODE_TOO_LARGE, f"package is {len(data)} bytes; the limit is {limits.max_package_bytes}")
    base = _root(root)
    digest = hashlib.sha256(data).hexdigest()
    if not force:
        existing = find_by_sha256(digest, base)
        if existing is not None:
            return ImportOutcome(STATUS_DUPLICATE, existing)
    conversation_id = uuid.uuid4().hex
    staging = base / STAGING_DIR / conversation_id
    try:
        staging.mkdir(parents=True)
        upload = staging / UPLOAD_FILE
        upload.write_bytes(data)
        package_dir = staging / PACKAGE_DIR
        package_dir.mkdir()
        unpacked = ipkg.unpack(upload, package_dir, limits=limits, should_stop=should_stop)
        normalized = ipkg.normalize(unpacked, staging, limits=limits, should_stop=should_stop)
        transcript = read_session_file(staging / SESSION_FILE, key_of(conversation_id))
        meta = _build_meta(
            conversation_id, clean_source_name(source_name), digest, len(data), unpacked, normalized, transcript
        )
        _write_meta(staging, meta)
        upload.unlink()
        if should_stop():
            raise ImportCancelledError()
        os.rename(staging, base / conversation_id)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    conversation = get(key_of(conversation_id), base)
    if conversation is None:
        raise OSError(f"imported conversation {conversation_id} did not land")
    return ImportOutcome(STATUS_IMPORTED, conversation)


class ImportBusyError(Exception):
    """Another import holds the one slot."""


@dataclass(frozen=True)
class _Job:
    """One granted import: its thread, the answer it will give, and the signal that calls it off."""

    thread: threading.Thread
    future: concurrent.futures.Future[ImportOutcome]
    stop: threading.Event


class ImportRunner:
    """Runs imports one at a time, each on a thread of its own that owns the slot until it ends.

    There is no closed state. The gateway's page is torn down and mounted again
    in the same process on a reload, and every generation of it shares this
    runner: :meth:`stop` calls off the import in progress and waits for it, and
    the slot opens again only once that import's thread has ended -- so a new
    generation is turned away while an old import is still finishing, and takes
    imports again as soon as it has.
    """

    def __init__(
        self,
        *,
        root: Callable[[], Path] = imports_root,
        limits: ImportLimits | None = None,
    ) -> None:
        self._root = root
        self._limits = limits
        self._lock = threading.Lock()
        self._active: _Job | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active is not None

    def try_submit(
        self, content_b64: str, source_name: str, *, force: bool = False
    ) -> concurrent.futures.Future[ImportOutcome]:
        future: concurrent.futures.Future[ImportOutcome] = concurrent.futures.Future()
        # Running before anyone can see it: a running future refuses cancel(),
        # so a caller that stops waiting -- even before the thread is scheduled
        # -- cannot call off an import it was granted. Only stop() calls one off.
        future.set_running_or_notify_cancel()
        stop = threading.Event()
        thread = threading.Thread(
            target=self._run, args=(future, stop, content_b64, source_name, force), name="raven-import", daemon=True
        )
        job = _Job(thread, future, stop)
        with self._lock:
            if self._active is not None:
                raise ImportBusyError("an import is already running")
            self._active = job
            try:
                job.thread.start()
            except BaseException:
                self._active = None
                raise
        return future

    def _run(
        self,
        future: concurrent.futures.Future[ImportOutcome],
        stop: threading.Event,
        content_b64: str,
        source_name: str,
        force: bool,
    ) -> None:
        try:
            try:
                data = base64.b64decode(content_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise PackageError(ipkg.CODE_BAD_ENCODING, f"content is not base64: {exc}") from None
            outcome = import_bytes(
                data,
                source_name,
                force=force,
                root=self._root(),
                limits=self._limits,
                should_stop=stop.is_set,
            )
            future.set_result(outcome)
        except (PackageError, ImportCancelledError) as exc:
            logger.info("import of {!r} refused or stopped: {}", source_name, exc)
            future.set_exception(exc)
        except BaseException as exc:  # noqa: BLE001 -- the caller may have gone; the failure must still be seen
            logger.exception("import of {!r} failed", source_name)
            future.set_exception(exc)
        finally:
            with self._lock:
                if self._active is not None and self._active.future is future:
                    self._active = None

    def stop(self, timeout: float) -> bool:
        """Call off the import in progress and wait for its thread; whether nothing is running after ``timeout``.

        The runner keeps taking imports afterwards: a slot whose import has not
        ended by then stays taken until it does.
        """
        with self._lock:
            job = self._active
        if job is None:
            return True
        job.stop.set()
        job.thread.join(timeout)
        return not job.thread.is_alive()


RUNNER = ImportRunner()


__all__ = [
    "CHANNEL",
    "FORMAT_VERSION",
    "KEY_PREFIX",
    "RUNNER",
    "SCHEMA",
    "STATUS_DUPLICATE",
    "STATUS_IMPORTED",
    "ImportBusyError",
    "ImportOutcome",
    "ImportRunner",
    "ImportedConversation",
    "clean_source_name",
    "delete",
    "find_by_sha256",
    "get",
    "import_bytes",
    "imports_root",
    "is_imported_key",
    "key_of",
    "list_conversations",
    "parse_key",
    "read_session",
    "rename",
    "sweep",
    "trajectory_target",
]
