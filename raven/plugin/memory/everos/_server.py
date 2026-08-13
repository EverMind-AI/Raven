"""EverOS server lifecycle manager: health probe + auto-start."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
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


class EverosNotConfiguredError(RuntimeError):
    """The memory LLM is missing, so no server could survive startup.

    A ``RuntimeError`` subclass on purpose: callers already treat that as
    "server unavailable", and this only narrows the reason so a caller that
    wants to say something more useful can.
    """


def _require_llm_configured() -> None:
    """Refuse to spawn a server that is guaranteed to die on startup.

    EverOS treats the LLM as a hard requirement: its lifespan provider builds
    the client eagerly and raises ``LLMNotConfiguredError`` when credentials are
    missing, which fails FastAPI startup outright. Spawning anyway costs the
    caller a full poll timeout waiting on a process that already exited, and
    leaves the real reason only in the server log.

    This is reachable out of the box, not just after a misconfiguration:
    ``memory.backend`` defaults to ``"everos"`` in the schema while the
    everos.toml template ships ``[llm]`` with an empty ``api_key``.
    """
    from raven.config.update_everos import everos_role_configured, get_everos_config_path

    if everos_role_configured("llm"):
        return
    raise EverosNotConfiguredError(
        f"EverOS memory LLM is not configured: [llm] in {get_everos_config_path()} needs both model and api_key."
    )


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
        f"everos not found next to {Path(sys.executable).parent} or on PATH. Please install the everos CLI."
    )


_LOG_TAIL_BYTES = 65536
_EXCEPTION_LINE = re.compile(r"^[\w.]+(Error|Exception)\b")


def _last_error_line() -> str:
    """The most recent exception line from the server log, or an empty string.

    The poll loop knows *that* the child died; the reason only exists in the
    log. Surfacing it beats telling the user to go read a file that is often
    hundreds of kilobytes of tracebacks. Only the tail is read, and any failure
    to read degrades to "no detail" rather than masking the original error.
    """
    try:
        path = server_log_path()
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _LOG_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if _EXCEPTION_LINE.match(stripped):
            return stripped
    return ""


def _start_server_if_unlocked(port: str) -> subprocess.Popen | None:
    """Try to acquire the startup lock and launch the server.

    Returns the child process when this process launched it, or ``None`` when
    the lock was already held (another process is spawning, so there is no child
    of ours to watch). Uses the cross-platform ``portable_lock`` so Windows does
    not crash on import.

    The handle is returned rather than discarded so the caller can tell "still
    booting" apart from "already dead" -- see :func:`ensure_everos_server`.
    """
    everos = _everos_executable()

    try:
        with file_lock(_lock_path(), blocking=False):
            log_path = server_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as log_file:
                proc = subprocess.Popen(
                    [everos, "server", "start", "--port", port],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            logger.info("started everos server on port {} (log: {})", port, log_path)
            return proc
    except LockTimeoutError:
        logger.debug("everos server startup lock held by another process; skipping spawn")
        return None


async def ensure_everos_server(
    base_url: str,
    *,
    timeout: float = 30.0,
    on_wait: Callable[[], None] | None = None,
) -> None:
    """Make sure a server is answering at ``base_url``, starting one if not.

    ``base_url`` is required on purpose. It used to default to
    ``DEFAULT_EVEROS_BASE_URL``, which let the onboard wizard probe 18791 while
    the memory backend read ``plugins.config`` and used whatever address was
    configured there. On a machine that had moved everos off the default port
    the wizard then decided nothing was running, spawned a second instance, and
    that instance died on the OME jobstore lock the first one already held --
    reported to the user as a 30s timeout blaming a missing install. A default
    here means "forgot to read the config" is a silent runtime bug rather than a
    signature error, so there is none.

    ``on_wait`` fires once, only when an actual boot is about to be waited on --
    never when a server is already answering. That lets a caller narrate the
    wait without adding noise to the common case where there is nothing to wait
    for.
    """
    if await asyncio.to_thread(_probe_health, base_url):
        logger.info("everos server already running at {}", base_url)
        return

    # Only on the spawn path: a server that answers /health has already built
    # its LLM client, so its credentials are proven by the probe above.
    _require_llm_configured()

    if on_wait is not None:
        on_wait()

    port = _extract_port(base_url)
    proc = await asyncio.to_thread(_start_server_if_unlocked, port)

    elapsed = 0.0
    while elapsed < timeout:
        await asyncio.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL
        if await asyncio.to_thread(_probe_health, base_url):
            logger.info("everos server ready at {}", base_url)
            return
        # A dead child will never answer, so stop waiting on it. Without this
        # the caller paid the full timeout for a process that exited in under a
        # second -- once per session, silently. ``proc`` is None only when
        # another process holds the startup lock, in which case there is no
        # child of ours to inspect and polling health is all we can do.
        if proc is not None and proc.poll() is not None:
            detail = await asyncio.to_thread(_last_error_line)
            raise RuntimeError(
                f"EverOS server exited with code {proc.returncode} while starting at {base_url}. "
                + (f"{detail} " if detail else "")
                + f"Full log: {server_log_path()}"
            )

    raise RuntimeError(
        f"EverOS server did not become healthy within {timeout}s at {base_url}. "
        f"The process is still running; check that port {port} is not held by "
        f"something else, and see {server_log_path()}"
    )


__all__ = ["DEFAULT_EVEROS_BASE_URL", "EverosNotConfiguredError", "ensure_everos_server"]
