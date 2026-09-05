"""JSONL + artifact storage for the trace core (audit.span.v1).

Dependency-free (stdlib only). Copied from the shared tracing-plugin core so
this package stays self-contained for a clean ``pip install``.

Layout under the state dir::

    <state_dir>/logs/audit-events.log      # one JSON event record per line
    <state_dir>/logs/audit-spans.log       # one JSON span per line
    <state_dir>/logs/audit-artifacts/...    # large payloads, SHA1-deduped
    <state_dir>/logs/archive/<date>/...     # rotated logs
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 50 * 1024 * 1024

_KIND_FILES = {
    "events": "audit-events.log",
    "spans": "audit-spans.log",
}


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
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", str("" if value is None else value).strip())
    normalized = normalized.strip("-")
    return (normalized or fallback)[:80]


class TraceStore:
    """Append-only JSONL store + artifact persistence for one state dir."""

    def __init__(self, state_dir: str | os.PathLike[str], max_bytes: int | None = None) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.logs_dir = self.state_dir / "logs"
        self.artifacts_dir = self.logs_dir / "audit-artifacts"
        self.archive_dir = self.logs_dir / "archive"
        self.max_bytes = max_bytes or int(os.environ.get("TRACE_LOG_MAX_BYTES", DEFAULT_MAX_BYTES))

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
            file_path.write_text(text, encoding="utf-8")
            return {
                "kind": kind,
                "path": str(file_path),
                "sha1": sha1,
                "bytes": len(text.encode("utf-8")),
                "preview": preview_text(text, preview_length),
            }
        except OSError as exc:
            return {"kind": kind, "path": None, "sha1": None, "bytes": None, "preview": "", "error": str(exc)}

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
