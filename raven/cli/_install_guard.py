"""Whether this environment is one Raven may run out of.

``system.upgrade`` makes the serving process exit on purpose and leaves a
detached helper to run ``uv tool install --force``, which deletes the old
environment before it writes the new one. A supervisor that respawns the
process it just watched exit therefore races the installer, and a start that
lands inside that window is not merely slow -- it is permanently wrong:
``build_app`` picks the page route once, so a serve that came up before
``raven/ui/dist`` existed answers ``/`` with the placeholder for the rest of
its life, on an installation that is by then perfectly good.

Two facts are needed to close that window, and neither is enough alone. The
marker says an upgrade is mid-flight even while the environment still looks
whole (uv has not started deleting yet). The completeness check says the
environment is not whole even when no marker survives (the helper was killed).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Optional

MARKER_NAME = "upgrade.json"

_UNPARENTED_GRACE_S = 120.0
"""How long a marker with no helper pid stays believable.

The helper stamps its pid as its first act, so a marker still missing one this
long after it was written means the spawn itself never got that far."""

_MAX_MARKER_AGE_S = 3600.0
"""Ceiling on any marker, pid alive or not. A helper wedged on a dead network
socket must not lock every later start out of the installation for good."""


@dataclass(frozen=True)
class UpgradeMarker:
    started_at: float
    pid: Optional[int] = None
    to_version: Optional[str] = None


@dataclass(frozen=True)
class InstallFault:
    reason: str
    detail: str


def marker_path() -> Path:
    from raven.config.loader import raven_home

    return raven_home() / MARKER_NAME


def write_marker(*, to_version: str | None = None, pid: int | None = None) -> Path:
    """Record that the environment is about to be replaced.

    Written by the side that still has a working Raven, before the helper is
    spawned: the gap between the spawn and the helper's first instruction is
    exactly when a supervisor sees its child exit and respawns it.
    """
    path = marker_path()
    payload: dict[str, object] = {"started_at": time.time()}
    if to_version is not None:
        payload["to_version"] = to_version
    if pid is not None:
        payload["pid"] = pid
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def clear_marker() -> None:
    try:
        marker_path().unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def read_marker() -> UpgradeMarker | None:
    try:
        payload = json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    started_at = payload.get("started_at")
    if not isinstance(started_at, (int, float)):
        return None
    pid = payload.get("pid")
    to_version = payload.get("to_version")
    return UpgradeMarker(
        started_at=float(started_at),
        pid=pid if isinstance(pid, int) and pid > 0 else None,
        to_version=to_version if isinstance(to_version, str) else None,
    )


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def marker_is_live(marker: UpgradeMarker, *, now: float | None = None) -> bool:
    """Whether the recorded upgrade can still be running."""
    moment = time.time() if now is None else now
    age = moment - marker.started_at
    if age > _MAX_MARKER_AGE_S:
        return False
    if marker.pid is None:
        return age <= _UNPARENTED_GRACE_S
    return _process_alive(marker.pid)


PAGE_RECORD_ENTRY = "raven/ui/dist/index.html"


def _packaged_page_is_owed() -> Optional[Path]:
    """Where this build promised a packaged page, or None if it shipped none.

    Read rather than assumed: a wheel built without ``ui/dist`` (``hatch_build``
    warns and continues) legitimately has no page, and refusing to serve one of
    those would break installations that were never broken.

    RECORD is parsed as text on purpose. ``importlib.metadata.files`` drops
    entries whose files are not on disk, so asking it what was promised answers
    only for what survived -- and every file this check exists to miss is
    exactly one it would have dropped.
    """
    try:
        dist = metadata.distribution("raven")
        record = dist.read_text("RECORD")
    except (metadata.PackageNotFoundError, OSError):
        return None
    if not record:
        return None
    for line in record.splitlines():
        if line.split(",", 1)[0].strip() == PAGE_RECORD_ENTRY:
            try:
                return Path(dist.locate_file(PAGE_RECORD_ENTRY))
            except Exception:
                return None
    return None


def missing_pieces() -> list[str]:
    """What this environment should contain and does not."""
    missing: list[str] = []
    try:
        metadata.version("raven")
    except metadata.PackageNotFoundError:
        missing.append("its own package metadata")
        return missing
    page = _packaged_page_is_owed()
    if page is not None and not page.exists():
        missing.append("the packaged page (raven/ui/dist)")
    return missing


def inspect_install(*, now: float | None = None) -> InstallFault | None:
    """Why this environment must not be run out of, or None when it is sound.

    A marker left by a helper that died is not itself a fault: the install may
    well have completed before the kill. It is a reason to look, and the
    environment's own contents decide.
    """
    marker = read_marker()
    if marker is not None and marker_is_live(marker, now=now):
        target = f" to {marker.to_version}" if marker.to_version else ""
        return InstallFault(
            "upgrading",
            f"an upgrade{target} is replacing this installation",
        )

    missing = missing_pieces()
    if missing:
        return InstallFault(
            "incomplete",
            "this installation is missing " + ", ".join(missing),
        )
    if marker is not None:
        # The install outlived the helper that was running it. Nothing is
        # missing, so the marker is only noise now, and leaving it would make
        # `raven doctor` report a fault that no longer exists.
        clear_marker()
    return None


__all__ = [
    "InstallFault",
    "UpgradeMarker",
    "clear_marker",
    "inspect_install",
    "marker_is_live",
    "marker_path",
    "missing_pieces",
    "read_marker",
    "write_marker",
]
