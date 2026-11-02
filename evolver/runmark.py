"""Trial identity: did this trial finish, and on which harness?

A trial directory is only complete when the code that observed the run return
writes a marker into it. The marker records the trial id it belongs to and a
digest of the harness tree that produced it, so a resumed run can reuse a
finished trial only when it was produced by the same configuration.

The marker is an on-disk contract: ``.trial_complete`` inside the trial's
output directory. It is written through :mod:`raven.utils.atomic_io`, which
serializes concurrent writers and renames into place, so a crash never leaves
a half-written marker that a later run would misread.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from raven.utils.atomic_io import atomic_replace

MARK = ".trial_complete"

VERSION = 1

#: Payload keys this module derives; supplemental metadata cannot replace them.
RESERVED = ("version", "run_id", "at", "harness")


def _ignored(p: Path) -> bool:
    """Paths that are not harness content and must not change its identity."""
    name = p.name
    return (
        ".git" in p.parts
        or "__pycache__" in p.parts
        or p.suffix == ".pyc"
        or name == ".DS_Store"
        or name == MARK
        or name.startswith(MARK + ".")
        or (".lock" in p.parts)
    )


def fingerprint(harness_dir: Path | None) -> str:
    """A stable digest of the harness tree that produced a trial.

    ``None`` is the inherited/base harness and digests to the shared value
    ``"base"``. Paths are hashed in POSIX spelling and sorted on that spelling,
    so the digest does not change with the host's path separator or the
    case-folding of the platform's path ordering.
    """
    if harness_dir is None:
        return "base"
    root = Path(harness_dir)
    if not root.is_dir():
        return "absent"

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    digest = hashlib.sha256()
    for p in sorted(root.rglob("*"), key=rel):
        if not p.is_file() or _ignored(p):
            continue
        digest.update(rel(p).encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(p.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def mark_complete(
    where: Path,
    *,
    run_id: str,
    harness_dir: Path | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Path:
    """Record that the trial ``run_id`` finished in ``where``.

    Call this only after the trial runner returned normally. ``extra`` is
    supplemental metadata; it cannot replace the reserved identity fields
    (:data:`RESERVED`) this function derives.
    """
    payload: dict[str, Any] = dict(extra or {})
    for key in RESERVED:
        payload.pop(key, None)
    payload.update(
        {
            "version": VERSION,
            "run_id": run_id,
            "at": time.time(),
            "harness": fingerprint(harness_dir),
        }
    )

    where = Path(where)
    where.mkdir(parents=True, exist_ok=True)
    path = where / MARK
    atomic_replace(path, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True))
    return path


def read_mark(where: Path) -> dict[str, Any] | None:
    """Return the marker at ``where``, or ``None`` when it is absent or invalid."""
    path = Path(where) / MARK
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def is_complete(
    where: Path,
    *,
    run_id: str | None = None,
    harness_dir: Path | None = None,
    expected: Mapping[str, Any] | None = None,
) -> bool:
    """Whether ``where`` holds a marker that matches this trial.

    ``run_id`` must match when given. ``harness_dir=None`` means the inherited
    harness, so a marker must have been written for the inherited harness too;
    a marker produced by a concrete harness tree does not satisfy a cwd-less
    check. ``expected`` checks supplemental metadata the caller recorded in
    ``extra`` (for example ``split``, ``k`` and the requested task count).
    """
    data = read_mark(where)
    if data is None or data.get("version") != VERSION:
        return False
    if run_id is not None and data.get("run_id") != run_id:
        return False
    if data.get("harness") != fingerprint(harness_dir):
        return False
    for key, value in (expected or {}).items():
        if data.get(key) != value:
            return False
    return True


__all__ = ["MARK", "VERSION", "RESERVED", "fingerprint", "mark_complete", "read_mark", "is_complete"]
