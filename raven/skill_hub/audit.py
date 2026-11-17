"""Append-only JSONL audit of Hub skill installs.

Every install that passes policy is recorded, so an operator can answer
"what third-party code landed on this machine, when, and why". Installs
come from ``use_skill``; records written with the ``auto_inject`` trigger
come from builds that injected Hub skills into context on their own.
Best-effort by design: an audit write failure must never take down the
install itself.

Two artifacts with one writer each per install:

- ``installs.jsonl`` (:func:`record_install`) — the append-only fact
  stream, one line per observed install event.
- ``.install-meta.json`` (:func:`write_install_meta`) — a single stamp
  inside the skill directory itself, the O(1) provenance source for
  ``raven skill list`` (no log scan needed). First install wins; later
  observations of the same bundle never rewrite it. ``installed_at`` is
  read off the directory mtime, so a bundle cached before stamping
  existed keeps its install date instead of the date it was first seen;
  ``installed_at_source`` says whether that date was measured (``mtime``)
  or fell back to the observation clock (``clock``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from raven.utils.atomic_io import locked_append

#: The stamp ``write_install_meta`` leaves in a bundle's skill directory. Named
#: here, beside the writer, so the two readers that decide "was this installed
#: from the hub" -- the page's skill list and ``skillhub.remove`` -- test for
#: the same file the installer wrote rather than a spelling of their own.
INSTALL_META = ".install-meta.json"

logger = logging.getLogger(__name__)


def record_install(
    path: Path | None,
    *,
    slug: str,
    version: str = "",
    trigger: str,
    score_safety: float | None = None,
    skill_dir: str | None = None,
) -> None:
    """Append one install record; ``path=None`` disables auditing."""
    if path is None:
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "slug": slug,
        "version": version,
        "trigger": trigger,
        "score_safety": score_safety,
        "dir": skill_dir,
    }
    try:
        locked_append(path, [json.dumps(record, ensure_ascii=False)])
    except OSError:
        logger.warning("failed to append skill install audit record to %s", path, exc_info=True)


def _installed_at(skill_dir: Path) -> tuple[str, str]:
    """When the bundle landed on disk, to the second, and which source dated it.

    The stamp is written the first time a bundle is observed, and a bundle
    cached before stamping existed is first observed long after it was
    installed. The directory's mtime is the last time its entries changed,
    which is the closest known approximation of the install date; the
    observation time is the fallback for a directory that cannot be read or
    whose timestamp the platform cannot convert (Windows rejects mtimes
    outside roughly 1970..3000 with ``OSError``). The second value names the
    source: ``"mtime"`` for a measured date, ``"clock"`` for the fallback,
    which first-install-wins freezes permanently and which would otherwise
    be indistinguishable from a real install date.

    The approximation holds only while nothing touches the bundle: an entry
    added or removed moves the mtime, and the stamp written here moves it to
    the observation moment. A directory dated in the future, from a bundle
    restored out of a backup or a clock left wrong at install time, records
    a future date, and the stamp is never rewritten.
    """
    try:
        mtime = skill_dir.stat().st_mtime
        return datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds"), "mtime"
    except (OSError, OverflowError, ValueError):
        return datetime.now(timezone.utc).isoformat(timespec="seconds"), "clock"


def write_install_meta(
    skill_dir: Path | str | None,
    *,
    slug: str,
    version: str = "",
    trigger: str,
    source: str = "hub",
    score_safety: float | None = None,
) -> None:
    """Stamp ``.install-meta.json`` into the skill directory, once.

    An existing stamp is never rewritten (first install wins), so
    ``installed_at`` keeps pointing at the original install even though
    both call sites re-observe cached bundles on later turns. The target
    directory is not created: no bundle directory means nothing was
    installed there, so there is nothing to stamp. ``installed_at`` comes
    from the directory mtime rather than the clock, so a bundle cached
    before stamping existed is dated to its install, not to the moment
    it was first observed; ``installed_at_source`` records which of the
    two produced the date.
    """
    if not skill_dir:
        return
    root = Path(skill_dir)
    path = root / INSTALL_META
    if path.exists():
        return
    installed_at, installed_at_source = _installed_at(root)
    record = {
        "slug": slug,
        "version": version,
        "source": source,
        "trigger": trigger,
        "score_safety": score_safety,
        "installed_at": installed_at,
        "installed_at_source": installed_at_source,
    }
    try:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("failed to write install meta to %s", path, exc_info=True)


__all__ = ["INSTALL_META", "record_install", "write_install_meta"]
