"""EverOS server lifecycle manager: health probe + auto-start."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any
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
_SERVER_CMDLINE = "everos server start"


def _pidfile_path() -> Path:
    return get_data_dir() / "everos-server.pid"


def _write_pidfile(pid: int, *, base_url: str, root: Path) -> None:
    """Record the server raven just started.

    Answers "is the process serving this root mine, and may I stop it?" without
    scanning the process table or depending on ``lsof``. Best-effort: failing to
    record must not fail the start.
    """
    try:
        _pidfile_path().write_text(
            json.dumps({"pid": pid, "base_url": base_url, "root": str(root)}),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("could not record everos server pidfile: {}", exc)


def _read_pidfile() -> dict[str, Any] | None:
    try:
        data = json.loads(_pidfile_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_everos_server(pid: int) -> bool:
    """Verify ``pid`` is still an everos server before signalling it.

    A pidfile is stale information: the process it names may have exited and the
    number been handed to something unrelated. Checking the command line is what
    keeps a port-convergence restart from killing an innocent process. ``ps -p``
    is POSIX and needs no extra dependency; the EverOS path is POSIX-only anyway.
    """
    ps = shutil.which("ps") or "/bin/ps"
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [ps, "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return _SERVER_CMDLINE in out.stdout


def find_recorded_server(root: Path | str) -> dict[str, Any] | None:
    """The server raven started for ``root``, if it is still that server."""
    record = _read_pidfile()
    if not record:
        return None
    if str(record.get("root")) != str(Path(root).expanduser()):
        return None
    pid = record.get("pid")
    if not isinstance(pid, int) or not _is_everos_server(pid):
        return None
    return record


class StopOutcome(str, Enum):
    """Why a stop attempt ended the way it did.

    A bare bool collapsed three situations a caller must tell apart: a process
    raven never started, a signal that could not be delivered, and a server that
    is shutting down but still draining work. Reporting them as one made the
    wizard tell a user whose memory tasks were mid-flight that raven had not
    started the process -- an explanation that is simply untrue.
    """

    STOPPED = "stopped"
    NOT_OURS = "not_ours"
    SIGNAL_FAILED = "signal_failed"
    STILL_DRAINING = "still_draining"


def stop_recorded_server(root: Path | str, *, timeout: float = 35.0) -> StopOutcome:
    """Ask the server raven started for ``root`` to shut down, and wait for it.

    SIGTERM rather than SIGKILL: uvicorn's graceful shutdown runs the OME
    engine's ``stop()``, which drains in-flight strategy runs (up to 30s) before
    releasing the jobstore lock. Killing outright would leave that work to crash
    recovery for no reason -- which is also why ``STILL_DRAINING`` is a distinct
    answer rather than a failure: the server is doing exactly what it should.
    """
    record = find_recorded_server(root)
    if record is None:
        return StopOutcome.NOT_OURS
    pid = int(record["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        logger.debug("could not signal everos server {}: {}", pid, exc)
        return StopOutcome.SIGNAL_FAILED
    waited = 0.0
    while waited < timeout:
        if not _is_everos_server(pid):
            _pidfile_path().unlink(missing_ok=True)
            return StopOutcome.STOPPED
        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
    logger.warning("everos server {} did not exit within {}s", pid, timeout)
    return StopOutcome.STILL_DRAINING


def ome_lock_held(root: Path | str) -> bool:
    """Is an OME engine already serving the data under ``root``?

    EverOS admits one offline engine per data directory and enforces it with a
    non-blocking exclusive ``flock`` on ``<root>/.index/sqlite/ome.db.lock``. The
    lock is the only reliable answer to "is this data already being served",
    because it is keyed on the directory rather than on a port: a second server
    on a different port dies here, which is exactly the failure that looked like
    a mysterious 30s startup timeout.

    Acquire-and-release, so this reports on *other* holders. Two caveats worth
    knowing: ``flock`` is held per open file description, so a raven process that
    itself holds the lock would see its own -- callers run this from the wizard
    and doctor, which do not; and a missing lock file means nobody has ever
    started an engine here, which is not the same as "free after a crash" but
    answers the same way.
    """
    lock = Path(root).expanduser() / ".index" / "sqlite" / "ome.db.lock"
    if not lock.exists():
        return False
    try:
        with file_lock(lock, blocking=False):
            return False
    except LockTimeoutError:
        return True
    except OSError as exc:
        # Unreadable lock file: refuse to claim the data is free, since acting on
        # that would spawn an instance that cannot start.
        logger.debug("could not test the OME lock at {}: {}", lock, exc)
        return True


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


def _child_env() -> dict[str, str]:
    """The environment the spawned server gets.

    EverOS resolves settings as ``init_args > env_vars > everos.toml``, so an
    ``EVEROS_API__PORT`` inherited from raven's own environment would outrank the
    ``[api]`` section this module just wrote -- and the whole point of dropping
    ``--port`` was to make that section the single authority on where a server
    for this root listens. Anything that could re-open that gap is removed;
    everything else is passed through, EVEROS_ROOT included, since the child
    still needs it for the imports that do not read ``--root``.
    """
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("EVEROS_API__"):
            del env[key]
    return env


def _start_server_if_unlocked(base_url: str) -> subprocess.Popen | None:
    """Try to acquire the startup lock and launch the server.

    Returns the child process when this process launched it, or ``None`` when
    the lock was already held (another process is spawning, so there is no child
    of ours to watch). Uses the cross-platform ``portable_lock`` so Windows does
    not crash on import.

    The handle is returned rather than discarded so the caller can tell "still
    booting" apart from "already dead" -- see :func:`ensure_everos_server`.

    The address is written into ``<root>/everos.toml`` rather than passed as
    ``--port``. A command-line override left the file describing an address
    nobody was listening on, which is how the wizard came to probe one port while
    the backend talked to another. Writing it makes the root self-describing, and
    both the child and any later reader agree by construction.
    """
    from raven.config.update_everos import everos_root, set_everos_api

    everos = _everos_executable()
    root = everos_root()
    parsed = urlparse(base_url)

    try:
        with file_lock(_lock_path(), blocking=False):
            # Inside the lock: losing the race means another process is already
            # spawning, and rewriting the declared address on the way out would
            # move the goalposts for a server that is starting or already up.
            set_everos_api(host=parsed.hostname or "127.0.0.1", port=int(parsed.port or 80))
            log_path = server_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as log_file:
                proc = subprocess.Popen(
                    # --root on the command line rather than only inherited via
                    # EVEROS_ROOT: a server that names its own root is one `ps`
                    # away from being identified, which matters when a stale
                    # instance has to be found and stopped.
                    [everos, "server", "start", "--root", str(root)],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=_child_env(),
                )
            logger.info("started everos server for {} at {} (log: {})", root, base_url, log_path)
            _write_pidfile(proc.pid, base_url=base_url, root=root)
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

    proc = await asyncio.to_thread(_start_server_if_unlocked, base_url)

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
        f"The process is still running; check that port {_extract_port(base_url)} "
        f"is not held by something else, and see {server_log_path()}"
    )


__all__ = [
    "DEFAULT_EVEROS_BASE_URL",
    "EverosNotConfiguredError",
    "StopOutcome",
    "ensure_everos_server",
]
