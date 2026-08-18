"""Per-instance single-run guard for the gateway.

A cross-platform advisory lock (portalocker: POSIX ``fcntl`` + Windows
``LockFileEx``) is held for the whole process lifetime, so the OS releases it
automatically on death (incl. SIGKILL) — no stale-lock cleanup is ever needed.
The lock is anchored at ``<instance data dir>/gateway.lock`` so that a
``--config`` instance guards independently of the default one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import portalocker

from raven.config.loader import get_config_path
from raven.config.paths import get_data_dir

LOCK_FILENAME = "gateway.lock"


class GatewayAlreadyRunningError(RuntimeError):
    """Raised when another live gateway already holds this instance's lock."""

    def __init__(self, info: "LockInfo") -> None:
        self.info = info
        super().__init__(f"gateway already running for this instance (pid {info.pid})")


@dataclass
class LockInfo:
    pid: int
    started_at: float
    config_path: str


def _lock_path() -> Path:
    return get_data_dir() / LOCK_FILENAME


def _read_payload(path: Path) -> LockInfo:
    """Best-effort read of the lock payload; never raises on missing/corrupt."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(
            pid=int(data.get("pid", -1)),
            started_at=float(data.get("started_at", 0.0)),
            config_path=str(data.get("config_path", "")),
        )
    except (OSError, ValueError, TypeError):
        return LockInfo(pid=-1, started_at=0.0, config_path="")


def acquire(now: float):
    """Take the exclusive instance lock or raise :class:`GatewayAlreadyRunningError`.

    Returns an open file handle the caller MUST keep alive for the whole
    process — closing it (or letting it be garbage-collected) releases the lock.
    """
    payload = _lock_path()
    anchor = payload.with_name(payload.name + ".lck")
    anchor.parent.mkdir(parents=True, exist_ok=True)
    # Lock a separate anchor file, not the payload itself: on Windows the lock
    # is mandatory, so locking the payload would block doctor's read-back of the
    # owner pid. The anchor carries the lock; the payload stays readable.
    fd = anchor.open("a+")
    try:
        portalocker.lock(fd, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except portalocker.exceptions.LockException:
        info = _read_payload(payload)
        fd.close()
        raise GatewayAlreadyRunningError(info)
    payload.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_at": now,
                "config_path": str(get_config_path()),
            }
        ),
        encoding="utf-8",
    )
    return fd


def read_status(now: float) -> LockInfo | None:
    """Zero-network liveness probe for ``doctor``.

    Probe the lock non-blocking: acquiring it means nobody holds it (release
    immediately and report not-running); a blocked acquire means a live
    instance owns it, so return its payload.
    """
    payload = _lock_path()
    anchor = payload.with_name(payload.name + ".lck")
    if not anchor.exists():
        return None
    with anchor.open("a+") as fd:
        try:
            portalocker.lock(fd, portalocker.LOCK_EX | portalocker.LOCK_NB)
            portalocker.unlock(fd)
            return None
        except portalocker.exceptions.LockException:
            return _read_payload(payload)


__all__ = ["acquire", "read_status", "GatewayAlreadyRunningError", "LockInfo"]
