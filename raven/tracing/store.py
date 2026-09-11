"""JSONL + artifact storage for the trace core (audit.span.v1).

Dependency-free (stdlib only). Copied from the shared tracing-plugin core so
this package stays self-contained for a clean ``pip install``.

Layout under the state dir::

    <state_dir>/logs/audit-events.log      # one JSON event record per line
    <state_dir>/logs/audit-spans.log       # one JSON span per line
    <state_dir>/logs/audit-artifacts/_blobs/<sha1[:2]>/<sha1>.<ext>   # one copy per payload
    <state_dir>/logs/audit-artifacts/<kind>/<date>/...      # hard links to those blobs
    <state_dir>/logs/archive/<date>/...     # rotated logs
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import artifact_v2

DEFAULT_MAX_BYTES = 50 * 1024 * 1024

_KIND_FILES = {
    "events": "audit-events.log",
    "spans": "audit-spans.log",
}

BLOBS_DIR_NAME = "_blobs"
_BLOB_READ_CHUNK = 1 << 20
_VERIFIED_BLOBS_MAX = 4096


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_key(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def to_json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def preview_text(value: Any, max_len: int = 400) -> str:
    text = value if isinstance(value, str) else to_json_text(value)
    if not text:
        return ""
    return text if len(text) <= max_len else f"{text[:max_len]}..."


def safe_segment(value: Any, fallback: str = "unknown") -> str:
    # Deliberately its own scheme, not utils.paths.mint_slug: the kernel may
    # not import raven.utils ("the kernel stands alone"), and trace labels
    # keep case and dots for readability.
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", str("" if value is None else value).strip())
    normalized = normalized.strip("-")
    return (normalized or fallback)[:80]


class TraceStore:
    """Append-only JSONL store + artifact persistence for one state dir."""

    def __init__(self, state_dir: str | os.PathLike[str], max_bytes: int | None = None) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.logs_dir = self.state_dir / "logs"
        self.artifacts_dir = self.logs_dir / "audit-artifacts"
        self.blobs_dir = self.artifacts_dir / BLOBS_DIR_NAME
        self.messages_dir = artifact_v2.messages_dir(self.artifacts_dir)
        self.archive_dir = self.logs_dir / "archive"
        self.max_bytes = max_bytes or int(os.environ.get("TRACE_LOG_MAX_BYTES", DEFAULT_MAX_BYTES))
        self._verified_blobs: dict[str, tuple[int, int, int]] = {}

    # -- paths -------------------------------------------------------------

    def _ensure(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _active_log(self, kind: str) -> Path:
        return self._ensure(self.logs_dir) / _KIND_FILES[kind]

    # -- append + rotation -------------------------------------------------

    def _rotate_if_needed(self, kind: str, next_text: str) -> Path:
        path = self._active_log(kind)
        if not path.exists():
            return path
        stat = path.stat()
        current_day = _date_key(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc))
        next_bytes = len(next_text.encode("utf-8"))
        rotate_by_date = current_day != _date_key()
        rotate_by_size = stat.st_size + next_bytes > self.max_bytes
        if not rotate_by_date and not rotate_by_size:
            return path
        day_dir = self._ensure(self.archive_dir / current_day)
        suffix = datetime.now(timezone.utc).strftime("%H%M%S%f")
        base = _KIND_FILES[kind].replace(".log", "")
        path.rename(day_dir / f"{base}-{current_day}-{suffix}.log")
        return path

    def append(self, kind: str, record: dict[str, Any]) -> None:
        text = f"{to_json_text(record)}\n"
        path = self._rotate_if_needed(kind, text)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)

    def append_event(self, record: dict[str, Any]) -> None:
        try:
            self.append("events", record)
        except OSError:
            pass

    def append_span(self, span: dict[str, Any]) -> None:
        try:
            self.append("spans", span)
        except OSError:
            pass

    # -- artifacts ---------------------------------------------------------

    def _blob_path(self, sha1: str, extension: str) -> Path:
        return self.blobs_dir / sha1[:2] / f"{sha1}.{extension}"

    def _blob_token(self, blob: Path) -> tuple[int, int, int]:
        stat_result = blob.stat()
        return (stat_result.st_ino, stat_result.st_mtime_ns, stat_result.st_size)

    def _remember_blob(self, blob: Path) -> None:
        if len(self._verified_blobs) >= _VERIFIED_BLOBS_MAX:
            for stale in list(self._verified_blobs)[: _VERIFIED_BLOBS_MAX // 4]:
                del self._verified_blobs[stale]
        self._verified_blobs[blob.name] = self._blob_token(blob)

    def _blob_is_intact(self, blob: Path, sha1: str) -> bool:
        """True when ``blob`` still holds the bytes its name claims.

        Keyed by the blob's full file name, sha1 plus extension: the same
        bytes stored once as ``.json`` and once as ``.txt`` are two files, and
        a verdict on one must not answer for the other. A hit on the cached
        (inode, mtime, size) skips the re-read, and any in-place edit moves
        the mtime of the shared inode, so a mutation through some other
        artifact path invalidates the entry rather than hiding behind it.
        """
        token = self._blob_token(blob)
        if self._verified_blobs.get(blob.name) == token:
            return True
        digest = hashlib.sha1()
        with blob.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_BLOB_READ_CHUNK), b""):
                digest.update(chunk)
        if digest.hexdigest() != sha1:
            return False
        self._verified_blobs[blob.name] = token
        return True

    def _publish_blob(self, blob: Path, text: str, sha1: str) -> None:
        """Create ``blob`` holding ``text``, losing gracefully to a racing writer.

        The temp file is named with a ``uuid4`` (not just the pid), so two
        writers in the same process publishing the same new payload never
        share a temp path and truncate each other's write; the write and the
        publish ``os.link`` share one ``try``/``finally`` so the temp file is
        always removed, even when the write itself fails. Publishing with
        ``os.link`` rather than ``os.replace`` means a concurrent writer never
        swaps the inode out from under a link already handed out.
        """
        self._ensure(blob.parent)
        tmp = blob.parent / f"{sha1}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            tmp.write_text(text, encoding="utf-8")
            os.link(tmp, blob)
            self._remember_blob(blob)
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)

    def _repair_blob(self, blob: Path, text: str, sha1: str) -> None:
        """Point ``blob`` back at the bytes its name claims.

        ``os.replace`` here, unlike the publish path: swapping the directory
        entry gives every later reference the intended bytes while artifacts
        already linked to the damaged inode keep exactly what they hold. They
        were edited, and silently rewriting them would be the audit trail
        lying a second time; what must not happen is the damage spreading to
        records written from now on.
        """
        tmp = blob.parent / f"{sha1}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, blob)
            self._remember_blob(blob)
        finally:
            tmp.unlink(missing_ok=True)

    def _materialize(self, file_path: Path, text: str, sha1: str, extension: str) -> None:
        """Link ``file_path`` at the single stored copy of ``text``.

        Hard link, not copy: the payload occupies its blocks once however many
        spans reference it, and the link count the filesystem keeps is the
        reference count :mod:`raven.tracing.compact` later reads.

        That sharing is why an existing blob is checked before it is linked
        rather than trusted on its name. Every published artifact path is a
        writable link to the blob, so an edit through any one of them changes
        what every later matching record would receive; without the check a
        single mutated file makes each subsequent artifact report a sha1 its
        own bytes do not have. The check costs one read per distinct payload
        per process, not per span, and none at all when the cached
        (inode, mtime, size) still matches.
        """
        blob = self._blob_path(sha1, extension)
        try:
            if not blob.exists():
                self._publish_blob(blob, text, sha1)
            if blob.exists() and not self._blob_is_intact(blob, sha1):
                self._repair_blob(blob, text, sha1)
            os.link(blob, file_path)
        except FileExistsError:
            pass
        except OSError:
            file_path.write_text(text, encoding="utf-8")

    def persist_artifact(
        self,
        kind: str,
        meta: dict[str, Any],
        payload: Any,
        *,
        label: str | None = None,
        preview_length: int = 400,
    ) -> dict[str, Any]:
        try:
            if payload is None:
                text, extension = "", "json"
            elif isinstance(payload, str):
                text, extension = payload, "txt"
            else:
                text, extension = json.dumps(payload, ensure_ascii=False, indent=2, default=str), "json"
            sha1 = hash_text(text)
            day = _date_key()
            dir_path = self._ensure(self.artifacts_dir / safe_segment(kind) / day)
            file_name = "-".join(
                [
                    datetime.now(timezone.utc).strftime("%H%M%S%f"),
                    safe_segment(meta.get("traceId") or meta.get("runId") or "trace"),
                    safe_segment(meta.get("sessionId") or meta.get("sessionKey") or "session"),
                    safe_segment(label or kind),
                    sha1[:10],
                ]
            )
            file_path = dir_path / f"{file_name}.{extension}"
            self._materialize(file_path, text, sha1, extension)
            return {
                "kind": kind,
                "path": str(file_path),
                "sha1": sha1,
                "bytes": len(text.encode("utf-8")),
                "preview": preview_text(text, preview_length),
            }
        except OSError as exc:
            return {"kind": kind, "path": None, "sha1": None, "bytes": None, "preview": "", "error": str(exc)}

    def address_items(self, items: list[Any]) -> list[Any]:
        """Publish each item under ``_messages/``; return ``{"$msg": sha1}`` refs.

        An item whose blob cannot be written comes back verbatim, so one
        filesystem failure costs that item its sharing rather than the record.
        The published blob is never hard-linked: it is the only file holding
        that content, and the shell references it by name from its JSON text.
        That is why these live beside ``_blobs/`` and not inside it -
        :mod:`raven.tracing.compact` sweeps a blob whose link count is 1.
        """
        refs: list[Any] = []
        for item in items:
            try:
                text = artifact_v2.message_text(item)
                sha1 = artifact_v2.message_sha1(item)
                blob = artifact_v2.message_path(self.artifacts_dir, sha1)
                if not blob.exists():
                    self._publish_blob(blob, text, sha1)
                if blob.exists() and not self._blob_is_intact(blob, sha1):
                    self._repair_blob(blob, text, sha1)
                refs.append(artifact_v2.make_ref(sha1))
            except OSError:
                refs.append(item)
        return refs

    @staticmethod
    def artifact_attributes(prefix: str, artifact: dict[str, Any] | None) -> dict[str, Any]:
        if not artifact:
            return {}
        attrs = {
            f"{prefix}.artifact_path": artifact.get("path"),
            f"{prefix}.artifact_sha1": artifact.get("sha1"),
            f"{prefix}.artifact_bytes": artifact.get("bytes"),
        }
        if artifact.get("error"):
            attrs[f"{prefix}.artifact_error"] = artifact["error"]
        return attrs
