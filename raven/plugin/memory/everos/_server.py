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

_API_PROBE_PATH = "/api/v2/memory/search"
"""One route off the prefix the backend client uses (see ``backend.py``). Kept
beside the client's own constant in spirit: if that prefix ever moves, this is
the other place that has to move with it, or the handshake stops handshaking."""


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


def _speaks_our_api(base_url: str) -> bool:
    """Whether the server on ``base_url`` serves the prefix our client uses.

    ``/health`` answers on every EverOS version, so it proves the port is alive
    and nothing more. An older server passes it and then 404s every call the
    client makes -- which is silent: a store failure is swallowed per turn, so
    the pairing can run for days looking healthy while nothing is written and
    nothing is recalled.

    Probed with a deliberately empty body: a served route rejects that with 422
    (or 400), a missing one answers 404. Either way no memory is written.
    """
    import httpx

    try:
        r = httpx.post(f"{base_url}{_API_PROBE_PATH}", json={}, timeout=3.0)
    except Exception:  # noqa: BLE001 — an unreachable server is handled by the health probe
        return False
    return r.status_code != 404


def _lock_path() -> Path:
    return get_data_dir() / "everos-server.lock"


def server_log_path() -> Path:
    """Where the detached server's stdout and stderr land.

    Named here rather than spelled out at each site: the wizard and doctor both
    point users at this file, and a name that drifts sends them to one that does
    not exist.
    """
    return get_logs_dir() / "everos-server.log"


def _everos_executable() -> str | None:
    """The EverOS server binary to launch, or None when there is none.

    Our own environment first, ``PATH`` second. EverOS is a pinned dependency of
    raven, so the server we start has to be that copy: ``uv tool install`` links
    only the named package's entry points, which means a clean machine has no
    ``everos`` on PATH at all, and a machine that does have one got it from some
    other install whose version need not match the client here. Both readings of
    ``which`` were wrong -- one refuses to start, the other starts a stranger.
    """
    local = Path(sys.executable).parent / ("everos.exe" if os.name == "nt" else "everos")
    if local.exists():
        return str(local)
    return shutil.which("everos")


def _start_server_if_unlocked(port: str) -> bool:
    """Try to acquire the startup lock and launch the server.

    Returns True if this process launched the server, False if the lock
    was already held (another process is spawning).  Uses the cross-
    platform ``portable_lock`` so Windows does not crash on import.
    """
    everos = _everos_executable()
    if not everos:
        raise RuntimeError(
            "no everos executable found beside this interpreter or on PATH. "
            "EverOS is a raven dependency, so this usually means the installation "
            "is incomplete -- reinstalling raven should restore it."
        )

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
        if await asyncio.to_thread(_speaks_our_api, base_url):
            logger.info("everos server already running at {}", base_url)
            return
        # Alive, ours by address, and unable to serve us. Adopting it is what
        # makes the failure silent, so refuse instead and say which port.
        raise RuntimeError(
            f"an EverOS server is running at {base_url} but does not serve "
            f"{_API_PROBE_PATH}, so it is too old for this raven. Stop it and let "
            f"raven start its own, or upgrade that server to match."
        )

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
