"""EverOS server lifecycle manager: health probe + auto-start."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from urllib.parse import urlparse

from loguru import logger

from raven.config.paths import get_logs_dir

_POLL_INTERVAL = 0.5


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


def _start_server(port: str) -> None:
    everos = shutil.which("everos")
    if not everos:
        raise RuntimeError("everos not found. Please install the everos CLI.")

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


async def ensure_everos_server(
    base_url: str = "http://localhost:18791",
    *,
    timeout: float = 30.0,
) -> None:
    if await asyncio.to_thread(_probe_health, base_url):
        logger.info("everos server already running at {}", base_url)
        return

    port = _extract_port(base_url)
    await asyncio.to_thread(_start_server, port)

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
