"""The PATH a sub-agent launch on this computer gets, as the last capture of the login shell found it.

A launch runs on the login shell's environment (``raven.agent.subagent.backends.env``),
which only running that shell can tell. Loading a config starts no process, so what
config needs of that PATH -- which agent a launch would run -- it reads from the
record each capture leaves here, and has nothing to read before the first one.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from raven.home import raven_home
from raven.utils.atomic_io import atomic_replace


def _record() -> Path:
    return raven_home() / "cache" / "login-path.json"


def remember(path: str) -> None:
    """Record ``path`` as the PATH a launch on this computer gets. Best effort: a launch never waits on it."""
    record = _record()
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(record, json.dumps({"PATH": path}))
    except OSError as exc:
        logger.debug("the login shell's PATH could not be recorded at {}: {}", record, exc)


def recall() -> str | None:
    """The PATH the last capture recorded, or ``None`` when no capture has recorded one."""
    try:
        recorded = json.loads(_record().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    path = recorded.get("PATH") if isinstance(recorded, dict) else None
    return path if isinstance(path, str) and path else None
