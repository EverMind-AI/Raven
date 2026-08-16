"""Append-only JSONL audit of Hub skill installs.

Every install that passes policy is recorded — both the silent
auto-inject path and explicit ``use_skill`` calls — so an operator can
answer "what third-party code landed on this machine, when, and why".
Best-effort by design: an audit write failure must never take down the
install itself.
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


__all__ = ["record_install"]
