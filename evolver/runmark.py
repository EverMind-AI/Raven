"""Trial identity: was this rollout finished, and by this configuration?

Completion is recorded explicitly by the code that observed the run return;
presence of output is not enough. The marker also fingerprints the harness
that produced the rollout, so a rollout from a different configuration is
never reused.

A rollout is only considered finished when the runner that observed it
records a completion marker, and the marker carries a digest of the harness
tree that produced it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

MARK = ".adapt_complete"

VERSION = 1


def _ignored(p: Path) -> bool:
    """Byproducts that are not part of the harness and must not change its identity."""
    return "__pycache__" in p.parts or p.suffix == ".pyc" or p.name == ".DS_Store"


def fingerprint(harness_dir: Path | None) -> str:
    """A stable digest of the harness tree that produced a rollout.

    ``None`` means a base rollout on the inherited harness and digests to the
    shared value ``"base"``.
    """
    if harness_dir is None:
        return "base"
    root = Path(harness_dir)
    if not root.is_dir():
        return "absent"
    h = hashlib.sha1()
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name == MARK or _ignored(p):
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()[:16]


def mark_complete(
    where: Path,
    *,
    run_id: str,
    harness_dir: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Record that ``run_id`` finished. Call only after the runner returned normally.

    Writes atomically via a temp file + ``os.replace`` so a crash mid-write
    never leaves a half-written marker.
    """
    d = Path(where)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": VERSION,
        "run_id": run_id,
        "at": time.time(),
        "harness": fingerprint(harness_dir),
        **(extra or {}),
    }
    tmp = d / f"{MARK}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    final = d / MARK
    os.replace(tmp, final)
    return final


def read_mark(where: Path) -> dict[str, Any] | None:
    """Return the parsed marker at ``where``, or ``None`` if absent/invalid."""
    path = Path(where) / MARK
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def is_complete(
    where: Path,
    *,
    run_id: str | None = None,
    harness_dir: Path | None = None,
) -> bool:
    """Whether ``where`` holds a valid completion marker.

    When ``run_id`` is given, the marker must record that same run id.
    When ``harness_dir`` is given, the marker's harness fingerprint must
    match the digest of that directory (so a rollout produced by a different
    harness configuration is never reused).
    """
    data = read_mark(where)
    if data is None:
        return False
    if data.get("version") != VERSION:
        return False
    if run_id is not None and data.get("run_id") != run_id:
        return False
    if harness_dir is not None and data.get("harness") != fingerprint(harness_dir):
        return False
    return True


__all__ = ["MARK", "VERSION", "fingerprint", "mark_complete", "read_mark", "is_complete"]
