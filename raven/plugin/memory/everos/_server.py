"""EverOS server lifecycle manager: health probe + auto-start."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

from raven.config.paths import get_data_dir, get_logs_dir
from raven.utils.portable_lock import LockTimeoutError, file_lock

_POLL_INTERVAL = 0.5


DEFAULT_EVEROS_BASE_URL = "http://localhost:18791"


def _extract_port(base_url: str) -> str:
    parsed = urlparse(base_url)
    return str(parsed.port or 80)


def _probe_health(base_url: str) -> bool:
    import httpx

    try:
        r = httpx.get(f"{base_url}/health", timeout=2.0)
        return r.status_code == 200
    except httpx.ConnectError:
        return False
    except Exception:
        return False


def _lock_path() -> Path:
    return get_data_dir() / "everos-server.lock"


def server_log_path() -> Path:
    """Where the detached server's stdout and stderr land.

    Named here rather than spelled out at each site: the wizard and doctor both
    point users at this file, and a name that drifts sends them to one that does
    not exist.
    """
    return get_logs_dir() / "everos-server.log"


def _everos_executable() -> str:
    """Locate the everos CLI, preferring the one installed alongside raven.

    ``everos`` is a hard dependency of raven, so it always lives in the same
    environment as the running interpreter -- but not necessarily on PATH:
    ``uv tool install`` exposes only the requested package's entry points, so
    ``~/.local/bin`` gets ``raven`` and not ``everos``. Checking the
    interpreter's own directory first therefore fixes more than a lookup
    failure: when PATH carries an everos from a *different* environment,
    ``shutil.which`` would hand back a version that does not match the one
    raven pins.

    POSIX only -- the EverOS path is gated off on native Windows by both
    callers (``onboard_everos._step4_memory`` and ``EverosBackend.start``).
    """
    sibling = Path(sys.executable).parent / "everos"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which("everos")
    if found:
        return found
    raise RuntimeError(
        f"everos not found next to {Path(sys.executable).parent} or on PATH. "
        "Please install the everos CLI."
    )


def _start_server_if_unlocked(port: str) -> bool:
    """Try to acquire the startup lock and launch the server.

    Returns True if this process launched the server, False if the lock
    was already held (another process is spawning).  Uses the cross-
    platform ``portable_lock`` so Windows does not crash on import.
    """
    everos = _everos_executable()

    try:
        with file_lock(_lock_path(), blocking=False):
            log_path = server_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as log_file:
                subprocess.Popen(
                    [everos, "server", "start", "--port", port],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            logger.info("started everos server on port {} (log: {})", port, log_path)
            return True
    except LockTimeoutError:
        logger.debug("everos server startup lock held by another process; skipping spawn")
        return False


async def ensure_everos_server(
    base_url: str = DEFAULT_EVEROS_BASE_URL,
    *,
    timeout: float = 30.0,
) -> None:
    if await asyncio.to_thread(_probe_health, base_url):
        logger.info("everos server already running at {}", base_url)
        return

    port = _extract_port(base_url)
    await asyncio.to_thread(_start_server_if_unlocked, port)

    elapsed = 0.0
    while elapsed < timeout:
        await asyncio.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL
        if await asyncio.to_thread(_probe_health, base_url):
            logger.info("everos server ready at {}", base_url)
            return

    raise RuntimeError(
        f"EverOS server failed to start within {timeout}s at {base_url}. "
        f"Check: (1) everos is installed (`uv run everos --help`), "
        f"(2) port {port} is not occupied, "
        f"(3) logs at {server_log_path()}"
    )


__all__ = ["DEFAULT_EVEROS_BASE_URL", "ensure_everos_server"]
