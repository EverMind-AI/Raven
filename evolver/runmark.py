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
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Mapping

from raven.utils.atomic_io import atomic_replace, write_transaction

MARK = ".trial_complete"

VERSION = 1

#: Payload keys this module derives; supplemental metadata cannot replace them.
RESERVED = ("version", "run_id", "at", "harness")

#: How long :func:`mark_complete` keeps retrying a denied rename, and the
#: first backoff. Foreign handles (scanners, indexers, backups) hold the
#: marker or its tmp only briefly, so the total wait stays well under a second.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF = 0.05


def _ignored(rel: Path) -> bool:
    """Whether ``rel``, relative to the harness root, is not harness content.

    The path must be relative: ``rglob`` yields absolute paths, so judging
    their parts would let any ancestor named ``.git``, ``__pycache__`` or
    ``.lock`` hide the whole tree and collapse its digest.
    """
    name = rel.name
    return (
        ".git" in rel.parts
        or "__pycache__" in rel.parts
        or rel.suffix == ".pyc"
        or name == ".DS_Store"
        or name == MARK
        or name.startswith(MARK + ".")
        or ".lock" in rel.parts
    )


def fingerprint(harness_dir: Path | None) -> str:
    """A stable digest of the harness tree that produced a trial.

    ``None`` is the inherited/base harness and digests to the shared value
    ``"base"``. A path that is not a directory digests to ``"absent"``;
    :func:`is_complete` never matches a marker against such a path, because a
    missing tree cannot prove it produced the marker. Paths are hashed in
    POSIX spelling and sorted on that spelling, so the digest does not change
    with the host's path separator or the case-folding of the platform's path
    ordering.
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
        if not p.is_file() or _ignored(Path(rel(p))):
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

    The ``.lock/`` sidecar left beside the marker is kept on purpose: it is
    the anchor readers and later writers acquire, so removing it would race
    their next acquire. It is one empty file per trial directory.
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
    _replace_with_retry(path, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True))
    return path


def _replace_with_retry(path: Path, data: str) -> None:
    """Write ``path``, retrying a rename that a foreign handle denied.

    The sidecar lock only serializes this module's own readers and writers. A
    virus scanner, indexer or backup tool can hold the marker or its tmp open
    and make ``os.replace`` fail with ``PermissionError`` on Windows; the
    trial has already been paid for by then, so a transient denial is retried
    with exponential backoff instead of failing the caller outright.
    :func:`raven.utils.atomic_io.atomic_replace` discards a leftover tmp on
    every failed attempt, and the rename removes it on success.
    """
    delay = _REPLACE_BACKOFF
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            atomic_replace(path, data)
            return
        except PermissionError:
            if attempt + 1 == _REPLACE_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2


def read_mark(where: Path) -> dict[str, Any] | None:
    """Return the marker at ``where``, or ``None`` when it is absent or invalid.

    Reads under the same sidecar lock ``atomic_replace`` writes under: on
    Windows a replace onto a file another handle holds open fails with a
    sharing violation, so a bare read here could make a concurrent
    ``mark_complete`` fail after the trial has already been paid for.

    The lock is advisory. On a filesystem that cannot hold one (a read-only
    mount, or a network filesystem without flock) the marker is read without
    it: the marker is replaced atomically, so a lockless read still sees
    either the previous or the next complete marker. The wait for a lock has
    no timeout; a writer holds it only across the in-memory write and the
    rename, so this blocks only while a live writer is inside that window.

    The ``.lock/`` sidecar is kept on purpose, as in :func:`mark_complete`.
    """
    path = Path(where) / MARK
    if not path.is_file():
        return None
    try:
        text = _read_under_lock(path)
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _read_under_lock(path: Path) -> str:
    with ExitStack() as stack:
        try:
            stack.enter_context(write_transaction(path))
        except OSError:
            # A read-only or lockless filesystem must not turn a finished
            # trial into an unfinished one; the marker is replaced atomically.
            return path.read_text(encoding="utf-8")
        return path.read_text(encoding="utf-8")


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
    check. A ``harness_dir`` that is not a directory never matches: the tree
    cannot prove it produced the marker, so the check fails closed instead of
    comparing against the shared ``"absent"`` digest.
    ``expected`` checks supplemental metadata the caller recorded in
    ``extra`` (for example ``split``, ``n``, ``tasks``, and the subject
    config's content digest).
    """
    data = read_mark(where)
    if data is None:
        return False
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != VERSION:
        return False
    if run_id is not None and data.get("run_id") != run_id:
        return False
    if harness_dir is None:
        expected_harness = "base"
    else:
        root = Path(harness_dir)
        if not root.is_dir():
            return False
        expected_harness = fingerprint(root)
    if data.get("harness") != expected_harness:
        return False
    for key, value in (expected or {}).items():
        if key not in data or data[key] != value:
            return False
    return True


__all__ = ["MARK", "VERSION", "RESERVED", "fingerprint", "mark_complete", "read_mark", "is_complete"]
