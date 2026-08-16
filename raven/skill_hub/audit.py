"""Append-only JSONL audit of Hub skill installs.

Every install that passes policy is recorded — both the silent
auto-inject path and explicit ``use_skill`` calls — so an operator can
answer "what third-party code landed on this machine, when, and why".
Best-effort by design: an audit write failure must never take down the
install itself.

Two artifacts with one writer each per install:

- ``installs.jsonl`` (:func:`record_install`) — the append-only fact
  stream, one line per observed install event.
- ``.install-meta.json`` (:func:`write_install_meta`) — a single stamp
  inside the skill directory itself, the O(1) provenance source for
  ``raven skill list`` (no log scan needed). First install wins; later
  observations of the same bundle never rewrite it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

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
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("failed to append skill install audit record to %s", path, exc_info=True)


def write_install_meta(
    skill_dir: Path | str | None,
    *,
    slug: str,
    version: str = "",
    trigger: str,
    source: str = "hub",
) -> None:
    """Stamp ``.install-meta.json`` into the skill directory, once.

    An existing stamp is never rewritten (first install wins), so
    ``installed_at`` keeps pointing at the original install even though
    both call sites re-observe cached bundles on later turns. The target
    directory is not created: no bundle directory means nothing was
    installed there, so there is nothing to stamp.
    """
    if not skill_dir:
        return
    path = Path(skill_dir) / ".install-meta.json"
    if path.exists():
        return
    record = {
        "slug": slug,
        "version": version,
        "source": source,
        "trigger": trigger,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("failed to write install meta to %s", path, exc_info=True)


__all__ = ["record_install", "write_install_meta"]
