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
from contextlib import suppress
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
    # Where this gateway's control plane answers, published after the server
    # binds. Carried here rather than in config because it is a runtime fact,
    # not a setting: the port may be ephemeral and the token is minted per boot,
    # so writing either into config.json would leave a stale secret behind on
    # every exit. Written under control_* and, for one release, under the
    # older web_* spelling as well: the payload is the one file two raven
    # versions may read at once, and the reader below accepts either.
    control_host: str = ""
    control_port: int = 0
    control_token: str = ""

    @property
    def control_url(self) -> str:
        return f"ws://{self.control_host}:{self.control_port}/ws" if self.control_host and self.control_port else ""


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
            control_host=str(data.get("control_host") or data.get("web_host", "")),
            control_port=int(data.get("control_port") or data.get("web_port", 0)),
            control_token=str(data.get("control_token") or data.get("web_token", "")),
        )
    except (OSError, ValueError, TypeError):
        return LockInfo(pid=-1, started_at=0.0, config_path="")


def publish_control_endpoint(host: str, port: int, token: str) -> None:
    """Record where this gateway's control plane answers, for local clients to find.

    Called after the server binds, so the port is the one it actually got. The
    payload carries a credential, and ``O_CREAT``'s mode applies only to a file
    the open itself creates -- ``acquire()`` already made this one -- so the
    open descriptor is narrowed to owner-only before the token is written,
    never after it. The anchor beside it still carries the lock; this file
    stays readable by its owner only.
    """
    payload = _lock_path()
    try:
        data = json.loads(payload.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    endpoint = {"control_host": host, "control_port": int(port), "control_token": token}
    data.update(endpoint)
    data.update({"web_host": host, "web_port": int(port), "web_token": token})
    body = json.dumps(data).encode("utf-8")
    fd = os.open(payload, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with suppress(OSError):  # a filesystem without POSIX modes
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:  # Windows has no fchmod
                payload.chmod(0o600)
        os.write(fd, body)
    finally:
        os.close(fd)


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
    body = json.dumps(
        {
            "pid": os.getpid(),
            "started_at": now,
            "config_path": str(get_config_path()),
        }
    ).encode("utf-8")
    # This file later receives the web token, so it is born owner-only
    # rather than trusting the umask.
    pfd = os.open(payload, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(pfd, body)
    finally:
        os.close(pfd)
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


__all__ = ["acquire", "read_status", "publish_control_endpoint", "GatewayAlreadyRunningError", "LockInfo"]
