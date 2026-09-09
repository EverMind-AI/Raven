"""The effort tier a deck run is on, and what it caps.

The host's session tier (medium / high / max) reaches a plugin's hooks as the
mode overlay; the deck tools run outside the hook chain, so the hook writes the
overlay's deck knobs to a small file in the session's deck folder and the tools
read it per call. Two caps are the whole vocabulary: how many whole-deck builds
a deck gets before it is released as it stands, and how many readings the
second reader takes. Six measured runs put the plateau of useful fixes inside
ten whole builds and three readings; what came after was churn on taste
findings and misread pages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven_ppt.contracts import Project

MODE_FILENAME = ".deck-mode.json"
BUILDS_FILENAME = "builds.json"
BUILD_CAP_KEY = "buildCap"
READING_CAP_KEY = "readingCap"


@dataclass(frozen=True)
class Caps:
    """What the tier allows; None is no cap, the max tier's shape."""

    mode: str = ""
    build_cap: int | None = None
    reading_cap: int | None = None


def _cap(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def write_mode(workspace: Path, overlay: dict[str, Any] | None, mode: str | None) -> Caps:
    """Record the session's tier for the tools; an empty overlay clears the caps."""
    overlay = overlay if isinstance(overlay, dict) else {}
    caps = Caps(
        mode=str(mode or ""), build_cap=_cap(overlay.get(BUILD_CAP_KEY)), reading_cap=_cap(overlay.get(READING_CAP_KEY))
    )
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / MODE_FILENAME).write_text(
            json.dumps({"mode": caps.mode, BUILD_CAP_KEY: caps.build_cap, READING_CAP_KEY: caps.reading_cap}),
            encoding="utf-8",
        )
    except OSError:
        pass
    return caps


def read_caps(workspace: Path) -> Caps:
    try:
        held = json.loads((workspace / MODE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Caps()
    if not isinstance(held, dict):
        return Caps()
    return Caps(
        mode=str(held.get("mode") or ""),
        build_cap=_cap(held.get(BUILD_CAP_KEY)),
        reading_cap=_cap(held.get(READING_CAP_KEY)),
    )


def _builds_path(project: Project) -> Path:
    return project.state_dir / BUILDS_FILENAME


def whole_builds_taken(project: Project) -> int:
    """How many whole-deck (non-draft) builds this deck has run so far."""
    try:
        held = json.loads(_builds_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    value = held.get("whole") if isinstance(held, dict) else None
    return value if isinstance(value, int) and value >= 0 else 0


def count_whole_build(project: Project) -> int:
    """One more whole-deck build; returns the count including this one."""
    taken = whole_builds_taken(project) + 1
    try:
        _builds_path(project).parent.mkdir(parents=True, exist_ok=True)
        _builds_path(project).write_text(json.dumps({"whole": taken}), encoding="utf-8")
    except OSError:
        pass
    return taken
