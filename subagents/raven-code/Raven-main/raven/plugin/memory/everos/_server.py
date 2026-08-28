"""EverOS server lifecycle manager: health probe + auto-start."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

from raven.config.paths import get_data_dir, get_logs_dir
from raven.utils.portable_lock import LockTimeoutError, file_lock

_POLL_INTERVAL = 0.5


def _extract_port(base_url: str) -> str:
    parsed = urlparse(base_url)
    return str(parsed.port or 80)


def _probe_health(base_url: str) -> bool:
    """Report whether an EverOS service answers at ``base_url``.

    Only a refused connection means "not running". Anything else -- a bad
    proxy, a TLS failure, a name that does not resolve -- is a
    misconfiguration that would otherwise be reported as a plain absence
    and then papered over by spawning a second server, so it propagates.
    """
    import httpx

    try:
        r = httpx.get(f"{base_url}/health", timeout=2.0)
        return r.status_code == 200
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False


def _lock_path() -> Path:
    return get_data_dir() / "everos-server.lock"


def _start_server_if_unlocked(port: str) -> bool:
    """Try to acquire the startup lock and launch the server.

    Returns True if this process launched the server, False if the lock
    was already held (another process is spawning).  Uses the cross-
    platform ``portable_lock`` so Windows does not crash on import.
    """
    everos = shutil.which("everos")
    if not everos:
        raise RuntimeError("everos not found. Please install the everos CLI.")

    try:
        with file_lock(_lock_path(), blocking=False):
            log_path = get_logs_dir() / "everos-server.log"
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


async def probe_everos_server(base_url: str) -> None:
    """Require an already-running EverOS service at ``base_url``.

    Used when the operator configured an address: raven does not own that
    process, and starting a local one would quietly serve a different store
    than the one the address names.
    """
    if await asyncio.to_thread(_probe_health, base_url):
        logger.info("everos server reachable at {}", base_url)
        return
    raise RuntimeError(
        f"EverOS is not reachable at the configured base_url {base_url}. "
        "raven does not start a server it was not asked to own -- start the "
        "service, or drop base_url to have raven run one locally."
    )


async def ensure_everos_server(
    base_url: str = "http://localhost:18791",
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
        f"(3) logs at {get_logs_dir() / 'everos-server.log'}"
    )


__all__ = ["ensure_everos_server"]
