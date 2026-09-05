"""EverOS server lifecycle manager: health probe + auto-start."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
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


_PROBE_TIMEOUT_S = 1.0


class ProbeResult(Enum):
    """Why a health probe ended the way it did.

    A bare bool collapsed two answers a caller must tell apart. ``REFUSED``
    comes back instantly and means nothing is listening, so retrying costs
    nothing. ``TIMEOUT`` means something *is* listening but not answering, and
    it charges the full budget every time -- a caller that retries it on every
    turn makes the user pay for a server that will not respond.
    """

    OK = "ok"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    ERROR = "error"


def probe_health(base_url: str, *, timeout: float = _PROBE_TIMEOUT_S) -> ProbeResult:
    """Ask ``{base_url}/health`` whether a server is answering there."""
    import httpx

    try:
        r = httpx.get(f"{base_url}/health", timeout=timeout)
    except httpx.ConnectError:
        return ProbeResult.REFUSED
    except httpx.TimeoutException:
        return ProbeResult.TIMEOUT
    except Exception:
        return ProbeResult.ERROR
    return ProbeResult.OK if r.status_code == 200 else ProbeResult.ERROR


def _probe_health(base_url: str) -> bool:
    """Liveness alone, for callers that have nothing to do with the reason.

    A wrapper rather than a changed return type: every ``ProbeResult`` member is
    truthy, so handing the enum to an existing ``if _probe_health(...)`` would
    turn a refused connection into a pass.
    """
    return probe_health(base_url) is ProbeResult.OK


def _lock_path() -> Path:
    return get_data_dir() / "everos-server.lock"


def server_log_path() -> Path:
    """Where the detached server's stdout and stderr land.

    Named here rather than spelled out at each site: the wizard and doctor both
    point users at this file, and a name that drifts sends them to one that does
    not exist.
    """
    return get_logs_dir() / "everos-server.log"


_INOTIFY_LIMIT_PATH = Path("/proc/sys/fs/inotify/max_user_instances")
_INOTIFY_LIMIT_FLOOR = 1024
_PROC_ROOT = Path("/proc")


def _inotify_usage() -> tuple[int, int] | None:
    """(inotify instances held by this user, the kernel cap), or ``None``.

    ``max_user_instances`` limits instances per real user id, and the spawned
    server inherits this process's uid, so only same-uid instances count. An
    instance is an fd whose ``/proc/<pid>/fd`` link reads ``anon_inode:inotify``;
    fdinfo cannot be the marker because a freshly created instance carries no
    ``inotify`` lines until its first watch, yet still consumes the cap. When
    procfs is absent or unreadable (non-Linux), ``None`` lets callers skip the
    check rather than guess.
    """
    try:
        limit = int(_INOTIFY_LIMIT_PATH.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    me = os.getuid()
    used = 0
    for pid_dir in _PROC_ROOT.glob("[0-9]*"):
        try:
            if pid_dir.stat().st_uid != me:
                continue
            for entry in (pid_dir / "fd").iterdir():
                try:
                    if os.readlink(entry) == "anon_inode:inotify":
                        used += 1
                except OSError:
                    continue
        except OSError:
            continue
    return used, limit


def _try_raise_inotify_limit() -> int | None:
    """Raise the per-user inotify cap when the kernel allows it, else ``None``.

    Writing ``/proc/sys`` needs root (or a dedicated capability); when that
    fails the caller falls back to spelling out the command, because the cap is
    the documented remedy and there is no raven-side substitute for it.
    """
    try:
        current = int(_INOTIFY_LIMIT_PATH.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    target = max(_INOTIFY_LIMIT_FLOOR, current * 2)
    try:
        _INOTIFY_LIMIT_PATH.write_text(str(target), encoding="ascii")
    except OSError:
        return None
    return target


def _inotify_gate() -> str | None:
    """``None`` when a spawn can proceed, else the diagnosis + fix text.

    With the cap exhausted a spawned server dies immediately on a cryptic
    OSError, so exhaustion is caught here instead: raise the cap when the
    kernel lets us (root), otherwise hand back the fix so the caller fails
    before spawning a process that cannot start.
    """
    usage = _inotify_usage()
    if usage is None:
        return None
    used, limit = usage
    if used < limit:
        return None
    raised = _try_raise_inotify_limit()
    after = _inotify_usage()
    if after is not None and after[0] < after[1]:
        if raised is not None:
            logger.info("raised fs.inotify.max_user_instances to {} so EverOS can start", raised)
        return None
    used, limit = after if after is not None else (used, limit)
    target = _INOTIFY_LIMIT_FLOOR if limit < _INOTIFY_LIMIT_FLOOR else limit * 2
    return (
        f"EverOS needs an inotify instance to watch its memory directory, but this user "
        f"already holds {used} of the {limit} allowed "
        f"(fs.inotify.max_user_instances). Raise it with:\n"
        f"  sudo sysctl -w fs.inotify.max_user_instances={target}\n"
        f"To make the change permanent:\n"
        f"  echo 'fs.inotify.max_user_instances={target}' | sudo tee /etc/sysctl.d/99-inotify-limits.conf"
    )


class EverosBinaryMissingError(RuntimeError):
    """The everos CLI is not installed where raven can reach it.

    Distinct from a startup failure: no retry, probe, or wait resolves it, so a
    caller that lumps it in with "server did not come up" keeps trying against
    something that was never there.
    """


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
    raise EverosBinaryMissingError(
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
    return stop_pid(int(record["pid"]), timeout=timeout)


def stop_pid(pid: int, *, timeout: float = 35.0) -> StopOutcome:
    """Stop a specific everos server and wait for it.

    Takes the pid rather than re-deriving it, so a caller that identified the
    process some other way -- :func:`lock_holder`, which asks the OS -- can act
    on what it found. Going back through the pidfile there would report
    ``NOT_OURS`` for a process the caller had just named, and losing the pidfile
    is precisely the case the lock lookup exists to recover.

    The caller is responsible for having established that this pid is an everos
    serving the root in question; both routes in do.
    """
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
    on a different port dies here, which is the failure that used to surface as a
    silent startup timeout with no mention of a lock.

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


@dataclass(frozen=True)
class LockHolder:
    """The process serving a root's data, and where it can be reached.

    ``port`` is ``None`` for a holder that takes the lock without serving HTTP
    (``everos demo``, an embedded engine, a server still binding). That is a
    real answer, not a missing one: it means the data is occupied and there is
    nowhere to connect, which needs a different response from "it is over
    there instead".
    """

    pid: int
    cmdline: str
    port: int | None


def _lsof_lock_pid(lock: Path) -> int | None:
    """The pid holding ``lock``, via ``lsof``. macOS ships it; Linux may not."""
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [lsof, "-t", "--", str(lock)], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = out.stdout.split()
    return int(first[0]) if first and first[0].isdigit() else None


def _proc_locks_pid(lock: Path) -> int | None:
    """The pid holding ``lock``, via Linux ``/proc/locks``.

    Preferred over ``lsof`` on Linux because it needs no external binary --
    minimal container images routinely omit one. Matching is by inode, which is
    what ``/proc/locks`` records; the path never appears there.
    """
    locks = Path("/proc/locks")
    if not locks.exists():
        return None
    try:
        target = lock.stat().st_ino
        lines = locks.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        # 3: FLOCK  ADVISORY  WRITE 1550263 fc:00:4980767 0 EOF
        #                          ^pid     ^maj:min:inode
        #
        # A process blocked waiting on the same lock gets a row of its own,
        # prefixed with "->", which shifts every field right by one:
        #
        # 2: -> FLOCK  ADVISORY  WRITE 1550264 fc:00:4980768 0 EOF
        #
        # Reading position 4 there yields the lock type rather than a pid, so
        # int() rejects it and the waiter is skipped. That is the intended
        # outcome and not a lucky accident to preserve by hand: the holder is
        # who may be signalled, and handing back a waiter's pid would have the
        # caller stop the wrong process.
        if len(fields) < 6:
            continue
        try:
            pid = int(fields[4])
            inode = int(fields[5].rsplit(":", 1)[-1])
        except ValueError:
            continue
        if inode == target:
            return pid
    return None


def _cmdline_of(pid: int) -> str:
    """The full command line of ``pid``, or an empty string."""
    ps = shutil.which("ps") or "/bin/ps"
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [ps, "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def _proc_net_rows() -> str:
    """``/proc/net/tcp`` and ``tcp6`` concatenated, or an empty string."""
    out = []
    for name in ("tcp", "tcp6"):
        try:
            out.append(Path(f"/proc/net/{name}").read_text(encoding="utf-8"))
        except OSError:
            continue
    return "".join(out)


def _socket_inodes_of(pid: int) -> set[int]:
    """The socket inodes open in ``pid``, from ``/proc/<pid>/fd``."""
    inodes: set[int] = set()
    try:
        entries = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return inodes
    for fd in entries:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith("socket:["):
            with contextlib.suppress(ValueError):
                inodes.add(int(target[8:-1]))
    return inodes


def _proc_net_listening_port(pid: int) -> int | None:
    """The port ``pid`` listens on, via ``/proc``. No external binary needed.

    Exists because ``_proc_locks_pid`` is right that minimal container images
    omit lsof, and a port lookup that needs it reports ``None`` there -- which
    the type documents as "holds the lock but serves no HTTP", so a healthy
    raven-managed server gets described as a squatter to be stopped.
    """
    rows = _proc_net_rows()
    if not rows:
        return None
    mine = _socket_inodes_of(pid)
    if not mine:
        return None
    for line in rows.splitlines()[1:]:
        fields = line.split()
        # sl local_address rem_address st ... inode
        if len(fields) < 10:
            continue
        # 0A is TCP_LISTEN.
        if fields[3] != "0A":
            continue
        try:
            inode = int(fields[9])
            port = int(fields[1].rsplit(":", 1)[-1], 16)
        except ValueError:
            continue
        if inode in mine:
            return port
    return None


def _listening_port(pid: int) -> int | None:
    """The TCP port ``pid`` listens on, or ``None`` if it serves no HTTP."""
    port = _lsof_listening_port(pid)
    if port is not None:
        return port
    return _proc_net_listening_port(pid)


def _lsof_listening_port(pid: int) -> int | None:
    """The TCP port ``pid`` listens on, or ``None``.

    ``-a`` is load-bearing: without it ``lsof`` ORs the ``-p`` and ``-i``
    selectors and returns every file the process has open alongside every
    socket on the machine.
    """
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [lsof, "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-P", "-n"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_listen_port(out.stdout)


def _parse_listen_port(lsof_output: str) -> int | None:
    """The port out of ``lsof -iTCP -sTCP:LISTEN`` output.

    Scans each row's fields from the right for the first ``host:port``. The
    address is not the last field -- ``(LISTEN)`` is -- and it is not at a fixed
    index either, since the COMMAND column is padded to its widest value.
    """
    for line in lsof_output.splitlines()[1:]:
        for field in reversed(line.split()):
            _, sep, port = field.rpartition(":")
            if sep and port.isdigit():
                return int(port)
    return None


def _lock_holder_pid(lock: Path, root: Path) -> int | None:
    """Who holds ``lock``, best source first.

    The OS knows the answer regardless of what raven remembers, so it is asked
    first; the pidfile is the fallback for when it cannot be reached. The
    pidfile is also the only source that can be wrong about the root, so its
    recorded root is checked before its pid is trusted.
    """
    # /proc/locks first where it exists: it is the source that distinguishes the
    # holder from a blocked waiter, which is the distinction the caller acts on.
    # Asking lsof first meant that branch never ran on a Linux box that has
    # lsof -- including the one it was validated on -- and ``lsof -t`` lists
    # holder and waiter alike, so its first pid is not reliably the holder.
    pid = _proc_locks_pid(lock) or _lsof_lock_pid(lock)
    if pid is not None:
        return pid
    record = _read_pidfile()
    if not record or str(record.get("root")) != str(root):
        return None
    recorded = record.get("pid")
    return recorded if isinstance(recorded, int) else None


def lock_holder(root: Path | str) -> LockHolder | None:
    """The process serving ``root``'s data, identified from the OS.

    Answers the question ``find_recorded_server`` cannot: not "did raven start
    this", but "who has the data". Losing the pidfile, moving the config
    directory, or upgrading from a raven that kept no pidfile all leave the
    lock intact, and the lock is what actually blocks a second instance.
    """
    resolved = Path(root).expanduser()
    lock = resolved / ".index" / "sqlite" / "ome.db.lock"
    if not lock.exists():
        return None
    pid = _lock_holder_pid(lock, resolved)
    if pid is None:
        return None
    cmdline = _cmdline_of(pid)
    # Both halves matter: the command has to be an everos server, and it has to
    # be serving *this* root. A pid can be recycled onto anything.
    if _SERVER_CMDLINE not in cmdline or str(resolved) not in cmdline:
        return None
    return LockHolder(pid=pid, cmdline=cmdline, port=_listening_port(pid))


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


def _require_written_port(root: Path, want: int) -> None:
    """Confirm ``[api].port`` really says ``want`` before spawning against it.

    Dropping ``--port`` made the toml the single authority on where a server
    listens, and that only holds while the file says what raven believes it
    says. Without this check a write that did not land -- a permission error
    swallowed upstream, a merge that dropped the section -- starts a server on
    the template's 8000 while every reader looks for the configured port. That
    is the exact drift the command-line override used to cause, arriving by a
    quieter route.
    """
    import tomllib

    path = Path(root) / "everos.toml"
    try:
        with path.open("rb") as fh:
            written = (tomllib.load(fh).get("api") or {}).get("port")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"could not read back [api] from {path}: {exc}") from exc
    if written != want:
        raise RuntimeError(f"[api] write to {path} did not take effect: expected port {want}, read back {written!r}")


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
            # Host and port come from the address the caller asked for, so what
            # gets written is what will be probed -- one spelling, no resolver
            # disagreement between the bind and the health check. The fallbacks
            # are taken from the default URL rather than restated, so there is
            # one place that decides how loopback is spelled.
            _default = urlparse(DEFAULT_EVEROS_BASE_URL)
            want_port = int(parsed.port or _default.port or 18791)
            try:
                set_everos_api(
                    host=parsed.hostname or _default.hostname or "localhost",
                    port=want_port,
                )
            except OSError as exc:
                # Callers guard the start path with ``except RuntimeError``,
                # which is what "could not start" has always meant here. An
                # unwritable root raises OSError from the atomic write and
                # walked past all of them, ending the wizard on a traceback for
                # something the user can simply fix and retry.
                raise RuntimeError(f"could not write [api] to {root}/everos.toml: {exc}") from exc
            _require_written_port(root, want_port)
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
    timeout: float = 10.0,
    on_wait: Callable[[], None] | None = None,
    on_proc: Callable[[subprocess.Popen], None] | None = None,
) -> subprocess.Popen | None:
    """Make sure a server is answering at ``base_url``, starting one if not.

    ``base_url`` is required on purpose. It used to default to
    ``DEFAULT_EVEROS_BASE_URL``, which let the onboard wizard probe 18791 while
    the memory backend read ``plugins.config`` and used whatever address was
    configured there. On a machine that had moved everos off the default port
    the wizard then decided nothing was running, spawned a second instance, and
    that instance died on the OME jobstore lock the first one already held --
    reported to the user as a startup timeout blaming a missing install. A default
    here means "forgot to read the config" is a silent runtime bug rather than a
    signature error, so there is none.

    ``timeout`` is a budget for how long a caller is willing to make the user
    wait, not a failure detector. Once the poll loop watches the child's exit
    code, a boot that cannot succeed is reported in about a second regardless of
    this value, so the only thing left to size is the wait itself. Overrunning it
    is cheap: the child keeps booting, this session goes without long-term
    memory, and the next one finds a healthy server -- so a small budget costs at
    most one session and heals itself, while a large one blocks every first
    session of a machine's uptime. Measured cold start on a small store is
    around two seconds.

    ``on_proc`` receives the spawned child the moment it exists, so a caller
    still holds it when this function goes on to raise.

    ``on_wait`` fires once, only when an actual boot is about to be waited on --
    never when a server is already answering. That lets a caller narrate the
    wait without adding noise to the common case where there is nothing to wait
    for.
    """
    if await asyncio.to_thread(_probe_health, base_url):
        logger.info("everos server already running at {}", base_url)
        return None

    # Only on the spawn path: a server that answers /health has already built
    # its LLM client, so its credentials are proven by the probe above.
    _require_llm_configured()

    # A machine whose per-user inotify cap is exhausted cannot hold a spawned
    # server: its watcher dies at boot with an OSError the log buries. Catch it
    # here -- raising the cap when this process may, failing with the commands
    # when it may not -- instead of spawning a child that cannot start.
    inotify_block = await asyncio.to_thread(_inotify_gate)
    if inotify_block:
        raise RuntimeError(inotify_block)

    if on_wait is not None:
        on_wait()

    proc = await asyncio.to_thread(_start_server_if_unlocked, base_url)
    # Handed over as soon as it exists, not only on the success return: a child
    # that dies during boot makes this function raise, and a caller that learns
    # of the handle only from the return value cannot then tell "already dead"
    # from "still starting" -- which is the distinction it needs most.
    if proc is not None and on_proc is not None:
        on_proc(proc)

    elapsed = 0.0
    while elapsed < timeout:
        await asyncio.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL
        if await asyncio.to_thread(_probe_health, base_url):
            logger.info("everos server ready at {}", base_url)
            return proc
        # A dead child will never answer, so stop waiting on it. Without this
        # the caller paid the full timeout for a process that exited in under a
        # second -- once per session, silently. ``proc`` is None only when
        # another process holds the startup lock, in which case there is no
        # child of ours to inspect and polling health is all we can do.
        if proc is not None and proc.poll() is not None:
            detail = await asyncio.to_thread(_last_error_line)
            if "inotify" in detail.lower():
                hint = await asyncio.to_thread(_inotify_gate)
                if hint:
                    detail = f"{detail} {hint}"
            raise RuntimeError(
                f"EverOS server exited with code {proc.returncode} while starting at {base_url}. "
                + (f"{detail} " if detail else "")
                + f"Full log: {server_log_path()}"
            )

    # Not phrased as a failure: the process is up and still booting, which is
    # what the caller should tell the user and what the next session will find.
    raise RuntimeError(
        f"EverOS server is still starting at {base_url} after {timeout}s. "
        f"This session runs without long-term memory; the next one should find it. "
        f"If it never comes up, check that port {_extract_port(base_url)} is free "
        f"and see {server_log_path()}"
    )


__all__ = [
    "DEFAULT_EVEROS_BASE_URL",
    "ProbeResult",
    "probe_health",
    "EverosBinaryMissingError",
    "EverosNotConfiguredError",
    "LockHolder",
    "StopOutcome",
    "ensure_everos_server",
    "lock_holder",
    "stop_pid",
]
