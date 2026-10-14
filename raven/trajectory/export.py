"""Trajectory Export: the selected turns of one session, packed as a versioned tar.gz.

The package is what the web page's "Export" hands the reader, and the input
``web-traj-import`` reads back, so its layout is a contract carried by
``EXPORT_SCHEMA`` and ``EXPORT_FORMAT_VERSION``::

    <export-id>/export.json          # what was exported, from where, and what is missing
    <export-id>/spans.jsonl          # every physical record of the selection, in write order
    <export-id>/session.jsonl        # the session record cut to those turns (omitted without one)
    <export-id>/artifacts/<path>     # each referenced artifact, at its path under audit-artifacts
    <export-id>/messages/<aa>/<sha1>.json  # every Message Blob an audit.artifact.v2 shell references

Unlike a Trajectory Bundle nothing is resolved or rewritten beyond the
artifact pointers: v2 shells stay shells and travel with their blobs, and the
content is neither redacted nor trimmed. Pointers the package carries are
rewritten to ``artifacts/...``; a pointer to a file that was not exported
keeps its original value and is listed under ``missing_artifacts``.

Members are regular files only, named under ``<export-id>/``, with owner and
mode normalized; content is copied byte for byte, so links in the store never
reach the archive. Member order is not part of the contract. Changing the
layout, a manifest key or the meaning of a field means bumping
``EXPORT_FORMAT_VERSION``.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import secrets
import stat
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from raven import __version__
from raven.tracing import artifact_v2
from raven.tracing import config as tracing_config
from raven.trajectory.selection import Selection, SessionCut, store_artifact

EXPORT_SCHEMA = "raven_trajectory_export"
EXPORT_FORMAT_VERSION = 1
EXPORT_ID = re.compile(r"^ex-\d{8}-[0-9a-f]{6}$")
MANIFEST_FILE = "export.json"
SPANS_FILE = "spans.jsonl"
SESSION_FILE = "session.jsonl"
ARTIFACTS_DIR = "artifacts"
MESSAGES_DIR = "messages"
MANIFEST_KEYS = (
    "schema",
    "format_version",
    "export_id",
    "created_at",
    "raven_version",
    "source",
    "turns",
    "traces",
    "counts",
    "missing_artifacts",
    "missing_messages",
    "transcript_unmatched",
)

DEFAULT_FILE_NAME = "raven-export.tar.gz"
_SUFFIX = ".tar.gz"
_NAME_LIMIT = 120
_ILLEGAL = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


class ExportCancelledError(Exception):
    """The export was asked to stop; nothing it wrote is left behind."""


@dataclass(frozen=True)
class ExportSummary:
    export_id: str
    counts: dict[str, int]
    missing_artifacts: tuple[str, ...]
    missing_messages: tuple[str, ...]
    transcript_unmatched: tuple[str, ...]
    size_bytes: int


def new_export_id() -> str:
    return f"ex-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3)}"


def clean_file_name(name: str) -> str:
    """A download name safe for any file system, ending in ``.tar.gz``.

    Only the last path segment counts; characters a file system or a header
    rejects are dropped. Non-ASCII letters stay: the download header encodes
    them.
    """
    base = _ILLEGAL.sub("", str(name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    stem = base[: -len(_SUFFIX)] if base.lower().endswith(_SUFFIX) else base
    stem = stem.strip(" .")[: _NAME_LIMIT - len(_SUFFIX)].rstrip(" .")
    return f"{stem}{_SUFFIX}" if stem else DEFAULT_FILE_NAME


def _blob_refs(payload: Any) -> list[str]:
    """Every message address a v2 shell references, in the fields v2 defines."""
    if not artifact_v2.is_v2(payload):
        return []
    refs: list[str] = []
    messages = payload.get("messages")
    for item in messages if isinstance(messages, list) else []:
        if (sha1 := artifact_v2.ref_sha1(item)) is not None:
            refs.append(sha1)
    for name in artifact_v2.TEXT_FIELDS:
        if (sha1 := artifact_v2.ref_sha1(payload.get(name))) is not None:
            refs.append(sha1)
    return refs


def _member(name: str, size: int, mtime: float) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.size = size
    info.mtime = int(mtime)
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _read(path: Path, root: Path) -> tuple[bytes, float] | None:
    """(bytes, mtime) of a regular file inside ``root``, else None.

    The path is resolved first, so a file or directory link leading out of the
    store is refused, and the file is opened without blocking and checked by
    descriptor before anything is read: a FIFO or a device swapped in after a
    check must not hang the export, which cannot be cancelled mid-read.
    """
    try:
        target = path.resolve()
        if not target.is_relative_to(root):
            return None
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read(), info.st_mtime
    except OSError:
        return None
    finally:
        if fd != -1:
            os.close(fd)


def write_export(
    selection: Selection,
    cut: SessionCut,
    out_file: Path,
    *,
    export_id: str,
    state_dir: Path | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> ExportSummary:
    """Write the package for ``selection`` and ``cut`` to ``out_file``.

    Built under a hidden ``.part`` name beside ``out_file`` and moved into
    place only when complete; a cancel (``should_stop``, polled after every
    member) or any failure removes the partial file.
    """
    if not EXPORT_ID.fullmatch(export_id):
        raise ValueError(f"not an export id: {export_id!r}")
    state = state_dir or tracing_config.state_dir()
    artifacts_root = (state / "logs" / "audit-artifacts").resolve()
    out_file = Path(out_file)
    tmp = out_file.with_name(f".{out_file.name}.part")

    archive_of: dict[str, str] = {}
    source_of: dict[str, Path] = {}
    missing_artifacts: list[str] = []
    for record in selection.spans:
        attrs = record.get("attributes")
        for key, value in attrs.items() if isinstance(attrs, dict) else ():
            if not key.endswith(".artifact_path") or not isinstance(value, str) or value in archive_of:
                continue
            target = store_artifact(value, state)
            if target is None:
                if value not in missing_artifacts:
                    missing_artifacts.append(value)
                continue
            name = f"{ARTIFACTS_DIR}/{target.relative_to(artifacts_root).as_posix()}"
            archive_of[value] = name
            source_of[name] = target

    def stop_check() -> None:
        if should_stop():
            raise ExportCancelledError()

    written_artifacts: set[str] = set()
    blob_names: dict[str, str] = {}
    missing_messages: set[str] = set()
    try:
        with tarfile.open(tmp, "w:gz") as tar:

            def add(name: str, data: bytes, mtime: float) -> None:
                tar.addfile(_member(f"{export_id}/{name}", len(data), mtime), io.BytesIO(data))
                stop_check()

            for name, source in sorted(source_of.items()):
                read = _read(source, artifacts_root)
                if read is None:
                    continue
                data, mtime = read
                add(name, data, mtime)
                written_artifacts.add(name)
                if source.suffix == ".json":
                    try:
                        payload = json.loads(data.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        payload = None
                    for sha1 in _blob_refs(payload):
                        blob_names.setdefault(sha1, f"{MESSAGES_DIR}/{sha1[:2]}/{sha1}.json")

            written_blobs = 0
            for sha1, name in sorted(blob_names.items()):
                read = _read(artifact_v2.message_path(artifacts_root, sha1), artifacts_root)
                if read is None:
                    missing_messages.add(sha1)
                    continue
                add(name, *read)
                written_blobs += 1

            for value, name in archive_of.items():
                if name not in written_artifacts and value not in missing_artifacts:
                    missing_artifacts.append(value)
            spans: list[dict[str, Any]] = []
            for record in selection.spans:
                out = copy.deepcopy(record)
                attrs = out.get("attributes")
                if isinstance(attrs, dict):
                    for key, value in attrs.items():
                        name = archive_of.get(value) if isinstance(value, str) else None
                        if key.endswith(".artifact_path") and name in written_artifacts:
                            attrs[key] = name
                spans.append(out)
            now = time.time()
            add(SPANS_FILE, "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in spans).encode("utf-8"), now)
            if cut.included:
                add(SESSION_FILE, cut.jsonl().encode("utf-8"), now)

            plan = selection.plan
            inner = (cut.metadata or {}).get("metadata")
            title = inner.get("title") if isinstance(inner, dict) else None
            counts = {"spans": len(spans), "artifacts": len(written_artifacts), "messages": written_blobs}
            manifest = {
                "schema": EXPORT_SCHEMA,
                "format_version": EXPORT_FORMAT_VERSION,
                "export_id": export_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "raven_version": __version__,
                "source": {
                    "session_key": plan.session_key,
                    "title": title if isinstance(title, str) else "",
                    "transcript_included": cut.included,
                },
                "turns": [
                    {
                        "number": turn.number,
                        "trace_id": turn.trace_id,
                        "start": turn.start,
                        "input_preview": turn.input_preview,
                    }
                    for turn in plan.turns
                ],
                "traces": {"selected": list(plan.selected), "dispatched": list(plan.dispatched)},
                "counts": counts,
                "missing_artifacts": list(missing_artifacts),
                "missing_messages": sorted(missing_messages),
                "transcript_unmatched": list(cut.unmatched),
            }
            add(MANIFEST_FILE, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), now)
        os.replace(tmp, out_file)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return ExportSummary(
        export_id=export_id,
        counts=counts,
        missing_artifacts=tuple(missing_artifacts),
        missing_messages=tuple(sorted(missing_messages)),
        transcript_unmatched=tuple(cut.unmatched),
        size_bytes=out_file.stat().st_size,
    )


__all__ = [
    "ARTIFACTS_DIR",
    "DEFAULT_FILE_NAME",
    "EXPORT_FORMAT_VERSION",
    "EXPORT_ID",
    "EXPORT_SCHEMA",
    "MANIFEST_FILE",
    "MANIFEST_KEYS",
    "MESSAGES_DIR",
    "SESSION_FILE",
    "SPANS_FILE",
    "ExportCancelledError",
    "ExportSummary",
    "clean_file_name",
    "new_export_id",
    "write_export",
]
