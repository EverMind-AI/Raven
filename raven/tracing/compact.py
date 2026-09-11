"""Fold already-written audit artifacts onto one copy per distinct payload.

The writer (:meth:`raven.tracing.store.TraceStore.persist_artifact`) hard-links
every new artifact at a blob under ``audit-artifacts/_blobs``. Artifacts written
before that landed are standalone files; this module rewrites them in place as
links to the same blobs, and removes blobs nothing references any more.

Paths never change and no referenced artifact is removed, so a run is a pure
convergence: idempotent, and safe against a live raven. A few properties carry
that guarantee. A blob's identity here is its full file name, sha1 plus
extension, not the sha1 alone: the same bytes stored once as ``.json`` and once
as ``.txt`` are two distinct blob files, so every cache and plan a run builds
is keyed on that full name to avoid conflating them. A blob is trusted to
replace an artifact's own copy only after its content is confirmed to hash to
its own name, so a torn or corrupt blob is reported in
:attr:`CompactResult.errors` instead of destroying the last good copy; that
confirmation is cached per run so a blob shared by many artifacts is hashed at
most once. Replacement goes through ``os.replace`` within one directory, so a
reader sees either the old file or the new link and never a partial one, and
the temp link staged for it is removed in a ``finally`` so a fold that fails or
is interrupted leaves nothing behind for the next walk to mistake for an
artifact. A temp link that outlives the run that staged it - its artifact was
removed, or already folded before the walk reached it - is swept once it ages
past :data:`FRESH_SECONDS`, judged by ctime rather than mtime since linking
leaves a blob's content-modification time untouched. Anything modified or
linked inside :data:`FRESH_SECONDS` is left alone, which keeps the walk off a
file still being written, keeps the orphan sweep off the gap in which the
writer has published a blob but not yet linked a span path to it, and keeps
the tmp sweep off a link a concurrently running compact just staged. A
directory this process cannot list - e.g. one it lacks permission to read -
is reported in :attr:`CompactResult.errors` the same way a torn blob is, and
skipped, so one bad directory costs only itself rather than the whole run. A
blob already holding the filesystem's maximum hard-link count answers a
further link attempt with ``EMLINK``; that is not treated as an error, since
the artifact simply keeps its own copy and every other file in the run still
converges, and reporting it would only repeat the same entry on every later
run once a blob is popular enough to hit the ceiling. ``result.errors`` itself
is capped at :data:`_MAX_ERRORS` entries, past which a failure is only counted
in :attr:`CompactResult.errors_dropped`, so a systemic problem cannot grow the
report without bound.
"""

from __future__ import annotations

import errno
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from raven.tracing import artifact_v2
from raven.tracing.store import BLOBS_DIR_NAME

# Directories under ``audit-artifacts/`` that are content-addressed stores, not
# ``<kind>/<day>/`` artifact trees. The walk has to name them because the two
# shapes are indistinguishable: ``_messages/<sha1[:2]>/<sha1>.json`` matches
# ``<kind>/<day>/<file>`` exactly. A message store left in is rehashed whole on
# every run - unbounded against a corpus that only grows - and the first run
# hard-links each message into ``_blobs/``, taking its link count to 2 and so
# putting it inside the store whose orphan sweep it must stay out of.

FRESH_SECONDS = 60
_READ_CHUNK = 1 << 20
_TMP_SUFFIX = ".compact.tmp"
_MAX_ERRORS = 500


_CONTENT_STORES = frozenset({BLOBS_DIR_NAME, artifact_v2.MESSAGES_DIR_NAME})


@dataclass
class CompactResult:
    """What one compaction run did.

    Byte counts are apparent size (``st_size``), not disk blocks. Under
    ``dry_run`` they describe what folding and sweeping would free without
    writing anything - except when a stale ``.compact.tmp`` is present: the
    tmp sweep honours ``dry_run`` too, so the blob it still pins reads
    ``st_nlink == 2`` for the orphan sweep in that same dry run, and
    ``blobs_removed``/``bytes_reclaimed`` under-report what a real run would
    free. That is the safe direction (dry-run never overstates a gain), not a
    bug to fix.

    ``errors`` holds at most :data:`_MAX_ERRORS` messages; ``errors_dropped``
    counts how many more were suppressed past that cap so a systemic failure
    cannot grow the report without bound.
    """

    scanned: int = 0
    folded: int = 0
    skipped_fresh: int = 0
    blobs_removed: int = 0
    bytes_reclaimed: int = 0
    errors: list[str] = field(default_factory=list)
    errors_dropped: int = 0


def _record_error(result: CompactResult, message: str) -> None:
    """Append to ``result.errors`` up to :data:`_MAX_ERRORS`, else count it.

    Keeps a systemic failure (e.g. a permission error hitting every
    directory) from growing the report without bound; past the cap,
    ``errors_dropped`` still tells the caller how many were cut.
    """
    if len(result.errors) < _MAX_ERRORS:
        result.errors.append(message)
    else:
        result.errors_dropped += 1


def _sha1_of(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_fresh(stat_result: os.stat_result, now: float) -> bool:
    return now - stat_result.st_mtime < FRESH_SECONDS


def _is_recently_linked(stat_result: os.stat_result, now: float) -> bool:
    return now - stat_result.st_ctime < FRESH_SECONDS


def _is_compact_tmp(name: str) -> bool:
    return name.startswith(".") and name.endswith(_TMP_SUFFIX)


def _iter_day_dirs(artifacts_dir: Path, result: CompactResult):
    """Yield each kind's day directories, skipping one this process cannot list.

    A directory unreadable to this process (EACCES) is reported in
    ``result.errors`` and skipped rather than raised, so it costs only itself
    and the rest of the tree is still walked.
    """
    try:
        kind_dirs = sorted(artifacts_dir.iterdir())
    except OSError as exc:
        _record_error(result, f"{artifacts_dir}: {exc}")
        return
    for kind_dir in kind_dirs:
        if not kind_dir.is_dir() or kind_dir.name in _CONTENT_STORES:
            continue
        try:
            day_dirs = sorted(kind_dir.iterdir())
        except OSError as exc:
            _record_error(result, f"{kind_dir}: {exc}")
            continue
        for day_dir in day_dirs:
            if day_dir.is_dir():
                yield day_dir


def _artifact_files(artifacts_dir: Path, result: CompactResult):
    for day_dir in _iter_day_dirs(artifacts_dir, result):
        try:
            paths = sorted(day_dir.iterdir())
        except OSError as exc:
            _record_error(result, f"{day_dir}: {exc}")
            continue
        for path in paths:
            if path.is_file() and not _is_compact_tmp(path.name):
                yield path


def _blob_matches(blob: Path, sha1: str, verified: dict[str, bool], result: CompactResult) -> bool:
    """Confirm blob's content hashes to sha1, caching the answer for this run.

    Cached by blob.name, not by sha1: two artifacts with the same content but
    different extensions resolve to two distinct blob files, and a verdict on
    one must never answer for the other. A mismatch is recorded once in
    result.errors instead of being trusted, so a torn or corrupt blob is never
    used to replace an artifact's own copy.
    """
    key = blob.name
    cached = verified.get(key)
    if cached is not None:
        return cached
    ok = _sha1_of(blob) == sha1
    verified[key] = ok
    if not ok:
        _record_error(result, f"{blob}: content does not match its own name")
    return ok


def _fold_one(
    path: Path,
    blob: Path,
    sha1: str,
    stat_result: os.stat_result,
    verified: dict[str, bool],
    result: CompactResult,
) -> None:
    """Replace ``path`` with a link to ``blob``, or publish ``path`` as ``blob``.

    A pre-existing ``blob`` is trusted only once its content is confirmed to
    hash to ``sha1``; on a mismatch ``result.errors`` gets the report and
    ``path`` is left untouched. The temp link staged for the replace is
    removed in a ``finally``, so a failure or interruption between the two
    syscalls leaves no leftover for the next walk to mistake for an artifact.
    """
    if not blob.exists():
        blob.parent.mkdir(parents=True, exist_ok=True)
        os.link(path, blob)
        verified[blob.name] = True
        return
    if not _blob_matches(blob, sha1, verified, result):
        return
    tmp = path.parent / f".{path.name}{_TMP_SUFFIX}"
    try:
        os.link(blob, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    result.folded += 1
    if stat_result.st_nlink == 1:
        result.bytes_reclaimed += stat_result.st_size


def _sweep_stale_tmp_files(artifacts_dir: Path, now: float, dry_run: bool, result: CompactResult) -> None:
    """Remove ``.compact.tmp`` leftovers once they age past FRESH_SECONDS.

    ``_fold_one`` cleans up its own temp link in a ``finally``, but that only
    runs when a later walk reaches the same artifact path again. If the
    artifact is removed, or the path already short-circuits as folded before
    ``_fold_one`` runs, nothing else ever reaches it, so it is swept here once
    it is too old to belong to a compact run still in flight.

    Staleness is judged by ctime, not mtime. The tmp is always a fresh
    ``os.link`` to an already-existing blob, and linking bumps an inode's
    ctime while leaving its mtime exactly as it was; mtime here would read as
    however old the blob's content is, not how long ago this link was made,
    and would wrongly condemn a tmp a concurrently running compact just staged.
    """
    for day_dir in _iter_day_dirs(artifacts_dir, result):
        try:
            paths = sorted(day_dir.iterdir())
        except OSError as exc:
            _record_error(result, f"{day_dir}: {exc}")
            continue
        for path in paths:
            if not path.is_file() or not _is_compact_tmp(path.name):
                continue
            try:
                stat_result = path.stat()
            except OSError as exc:
                _record_error(result, f"{path}: {exc}")
                continue
            if _is_recently_linked(stat_result, now):
                continue
            if not dry_run:
                try:
                    path.unlink()
                except OSError as exc:
                    _record_error(result, f"{path}: {exc}")


def _sweep_orphan_blobs(blobs_dir: Path, now: float, dry_run: bool, result: CompactResult, planned: set[str]) -> None:
    """Remove blobs nothing references, skipping any blob.name in ``planned``.

    Compared by the blob's full file name, not its stem: the stem is the sha1
    alone, and a folded ``<sha1>.json`` must not shield an orphaned
    ``<sha1>.txt`` that happens to share it.
    """
    if not blobs_dir.is_dir():
        return
    try:
        shards = sorted(blobs_dir.iterdir())
    except OSError as exc:
        _record_error(result, f"{blobs_dir}: {exc}")
        return
    for shard in shards:
        if not shard.is_dir():
            continue
        try:
            blobs = sorted(shard.iterdir())
        except OSError as exc:
            _record_error(result, f"{shard}: {exc}")
            continue
        for blob in blobs:
            if blob.name in planned:
                continue
            try:
                stat_result = blob.stat()
            except OSError as exc:
                _record_error(result, f"{blob}: {exc}")
                continue
            if stat_result.st_nlink != 1 or _is_fresh(stat_result, now):
                continue
            if not dry_run:
                try:
                    blob.unlink()
                except OSError as exc:
                    _record_error(result, f"{blob}: {exc}")
                    continue
            result.blobs_removed += 1
            result.bytes_reclaimed += stat_result.st_size


def compact(artifacts_dir: Path, *, dry_run: bool = False) -> CompactResult:
    """Fold every artifact under ``artifacts_dir`` onto one blob per payload."""
    result = CompactResult()
    artifacts_dir = Path(artifacts_dir)
    if not artifacts_dir.is_dir():
        return result
    blobs_dir = artifacts_dir / BLOBS_DIR_NAME
    now = time.time()
    planned: set[str] = set()
    verified: dict[str, bool] = {}

    for path in _artifact_files(artifacts_dir, result):
        try:
            stat_result = path.stat()
        except OSError as exc:
            _record_error(result, f"{path}: {exc}")
            continue
        result.scanned += 1
        if _is_fresh(stat_result, now):
            result.skipped_fresh += 1
            continue
        try:
            sha1 = _sha1_of(path)
        except OSError as exc:
            _record_error(result, f"{path}: {exc}")
            continue
        blob = blobs_dir / sha1[:2] / f"{sha1}{path.suffix}"
        try:
            if blob.exists() and blob.stat().st_ino == stat_result.st_ino:
                planned.add(blob.name)
                continue
            if dry_run:
                if blob.exists() and not _blob_matches(blob, sha1, verified, result):
                    continue
                if blob.exists() or blob.name in planned:
                    result.folded += 1
                    if stat_result.st_nlink == 1:
                        result.bytes_reclaimed += stat_result.st_size
                planned.add(blob.name)
                continue
            _fold_one(path, blob, sha1, stat_result, verified, result)
            if verified.get(blob.name):
                planned.add(blob.name)
        except OSError as exc:
            if exc.errno == errno.EMLINK:
                # The blob already carries the filesystem's max hard-link
                # count (ext4: ~65,000). The artifact keeps its own copy and
                # the run moves on instead of recording a failure that would
                # only repeat itself on every future run.
                continue
            _record_error(result, f"{path}: {exc}")

    _sweep_stale_tmp_files(artifacts_dir, now, dry_run, result)
    _sweep_orphan_blobs(blobs_dir, now, dry_run, result, planned)
    return result
