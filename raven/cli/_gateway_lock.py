"""Per-instance single-run guard for the gateway.

An advisory ``fcntl.flock`` is held for the whole process lifetime, so the
kernel releases it automatically on death (incl. SIGKILL) — no stale-lock
cleanup is ever needed. Degrades to lock-less on Windows, mirroring the cron
service. The lock is anchored at ``<instance data dir>/gateway.lock`` so that a
``--config`` instance guards independently of the default one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

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
        data = json.loads(path.read_text())
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
    On Windows (no ``fcntl``) returns the handle without locking.
    """
    path = _lock_path()
    fd = path.open("a+")
    if fcntl is None:
        return fd
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        info = _read_payload(path)
        fd.close()
        raise GatewayAlreadyRunningError(info)
    fd.seek(0)
    fd.truncate()
    fd.write(json.dumps({
        "pid": os.getpid(),
        "started_at": now,
        "config_path": str(get_config_path()),
    }))
    fd.flush()
    return fd


def read_status(now: float) -> LockInfo | None:
    """Zero-network liveness probe for ``doctor``.

    Probe the lock non-blocking: acquiring it means nobody holds it (release
    immediately and report not-running); a blocked acquire means a live
    instance owns it, so return its payload.
    """
    path = _lock_path()
    if not path.exists() or fcntl is None:
        return None
    with path.open("a+") as fd:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            return None
        except OSError:
            return _read_payload(path)


__all__ = ["acquire", "read_status", "GatewayAlreadyRunningError", "LockInfo"]
