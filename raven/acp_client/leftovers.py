"""ACP agents that outlive the host that launched them, and how they are ended.

An ACP agent runs in a session of its own (``start_new_session=True``), so it
gets none of the signals sent to its host's process group or terminal, and
while the host lives only ``AcpClient.close`` ends it. A host that dies before
its teardown -- killed, crashed, ended by the OOM killer -- leaves every agent
it launched running with nobody on the other end of its pipes, holding its
memory until someone kills it by hand.

Two layers close that on Linux. The agent starts through a one-line exec
wrapper that arms ``PR_SET_PDEATHSIG``, so its direct process ends with the
host. That reaches the direct child only, and an ``npx`` launcher's real server
is a grandchild, so each host also records the process groups it launched --
one file per host under ``<data dir>/acp-launches/`` -- and the first launch in
any process ends the groups recorded by hosts that have since died.

Ending a group by number is safe only while the number still names that group.
A record is honoured only inside the boot and PID namespace that wrote it, and a
group whose leader pid is alive with another start time has been reused and is
left alone. A leader that is gone proves less: Linux keeps its number taken while
the group lives on, but the group may have ended since the record was written
and the number gone to a new group. So every agent a host launches carries that
host's mark in its environment, which every process the agent starts inherits,
and a group is ended only while one of its members carries the mark of the host
that died. What cannot be read is reason to leave a group alone, never to end it.

Everything here is a no-op off Linux.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from raven.config.paths import get_runtime_subdir
from raven.utils.atomic_io import atomic_replace, remove_with_lock

# Run by the interpreter that becomes the agent. The signal is armed before the
# exec and survives it; a host that died between the fork and the prctl would
# never send it, so the parent is checked once the signal is armed. Arming is
# best effort: without ctypes or prctl the agent starts as it would have.
# The interpreter's own startup ignores SIGPIPE and SIGXFSZ, which the exec would
# hand on, so both go back to the default the spawn gave the wrapper. Its startup
# also coerces a C locale to C.UTF-8 (PEP 538); that LC_CTYPE reaches the agent
# and is left in place.
# PR_SET_PDEATHSIG is 1 and SIGKILL is 9 on every Linux architecture.
_WRAPPER = (
    "import os, signal, sys\n"
    "try:\n"
    "    import ctypes\n"
    "    armed = ctypes.CDLL(None).prctl(1, 9, 0, 0, 0) == 0\n"
    "except Exception:\n"
    "    armed = False\n"
    "if armed and os.getppid() != int(sys.argv[1]):\n"
    "    os._exit(1)\n"
    "signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
    "signal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n"
    "try:\n"
    "    os.execv(sys.argv[2], sys.argv[3:])\n"
    "except OSError as exc:\n"
    "    sys.stderr.write(f'raven: cannot start {sys.argv[2]}: {exc}\\n')\n"
    "    os._exit(127)\n"
)


def _on_linux() -> bool:
    return sys.platform.startswith("linux")


def bind_to_host(argv: list[str], env: Mapping[str, str], cwd: str | None) -> list[str]:
    """``argv`` started through the parent-death wrapper, where that is possible.

    Only a program that resolves to an executable file is wrapped. A missing one
    is left for the spawn to refuse in the host, which is what makes launching
    an absent agent raise at once instead of looking like an agent that started
    and then died. The resolution mirrors the spawn's own: a name with a slash
    is a path (a relative one from the child's ``cwd``), a bare name is looked
    up on the PATH the child is handed.
    """
    if not _on_linux() or not argv:
        return argv
    interpreter = sys.executable
    if not interpreter or not os.access(interpreter, os.X_OK):
        return argv
    program = _resolve(argv[0], env, cwd)
    if program is None:
        return argv
    return [interpreter, "-I", "-S", "-c", _WRAPPER, str(os.getpid()), program, *argv]


def _resolve(name: str, env: Mapping[str, str], cwd: str | None) -> str | None:
    try:
        if "/" in name:
            path = name if os.path.isabs(name) else os.path.join(cwd or os.getcwd(), name)
            if not os.path.isabs(path):
                path = os.path.join(os.getcwd(), path)
        else:
            path = shutil.which(name, path=env.get("PATH", os.defpath)) or ""
    except OSError:
        # The host's working directory is gone: nothing to resolve a relative
        # program against, so it is left to the spawn, as it was.
        return None
    if os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return None


_DIR_NAME = "acp-launches"
_VERSION = 1
# The environment variable naming the host an agent was launched by, as the stem
# of that host's record file; every process the agent starts inherits it.
_MARK = "RAVEN_ACP_HOST"
# How long a launch waits for the first sweep before it starts its agent anyway,
# and how often it looks.
_SWEEP_WAIT_S = 5.0
_SWEEP_POLL_S = 0.02

_lock = threading.Lock()
# pgid -> (the host file it went to, the host as that file names it, its entry).
# Both are fixed when the group is recorded, so each record stays with the file
# and the identity it was written under.
_records: dict[int, tuple[Path, dict[str, Any], dict[str, Any]]] = {}
_swept = False
_sweep_done = threading.Event()
_warned = False
_sweep_abandoned = False


def _stat_fields(pid: int) -> list[str] | None:
    """``/proc/<pid>/stat`` from field 3 on; None when there is no such process.

    Any other failure to read it raises: what decides whether to end a group
    must never take "could not tell" for "gone".
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            stat = f.read()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # The command name (field 2) may hold spaces and parentheses, so fields are
    # counted from its closing parenthesis.
    return stat[stat.rindex(")") + 2 :].split()


def _start_ticks(pid: int) -> int | None:
    """When ``pid`` started, in clock ticks since boot (field 22); None when there is no such process."""
    fields = _stat_fields(pid)
    return None if fields is None else int(fields[19])


def _boot_id() -> str | None:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _pid_namespace() -> str | None:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def _boot_time() -> float | None:
    """When this machine booted, in seconds since the epoch; None where /proc cannot say."""
    try:
        with open("/proc/stat", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _written_before_boot(path: Path) -> bool:
    booted = _boot_time()
    if booted is None:
        return False
    try:
        return path.stat().st_mtime < booted
    except OSError:
        return False


def _host() -> dict[str, Any] | None:
    """This host as its record file names it; None where /proc cannot name it."""
    start = _start_ticks(os.getpid())
    boot_id, pid_ns = _boot_id(), _pid_namespace()
    if start is None or boot_id is None or pid_ns is None:
        return None
    return {"version": _VERSION, "boot_id": boot_id, "pid_ns": pid_ns, "host_pid": os.getpid(), "host_start": start}


def _stem(host_pid: int, host_start: int) -> str:
    return f"{host_pid}-{host_start}"


def launch_env() -> dict[str, str]:
    """The mark an agent this host launches carries in its environment; empty where there is none."""
    if not _on_linux():
        return {}
    try:
        host = _host()
    except Exception:  # noqa: BLE001 - the mark must never fail a launch
        return {}
    return {} if host is None else {_MARK: _stem(host["host_pid"], host["host_start"])}


def record(pgid: int, agent: str) -> None:
    """Note a group this host launched, so a later host can end it if this one dies first."""
    if not _on_linux():
        return
    try:
        host = _host()
        leader_start = _start_ticks(pgid)
        if host is None or leader_start is None:
            return
        path = get_runtime_subdir(_DIR_NAME) / f"{_stem(host['host_pid'], host['host_start'])}.json"
        entry = {"leader_start": leader_start, "agent": agent, "launched_at": datetime.now(UTC).isoformat()}
        with _lock:
            _records[pgid] = (path, host, entry)
            _write(path)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never fail a launch
        _warn_once(exc)


def forget(pgid: int) -> None:
    """Drop the record of a group its connection has closed."""
    try:
        with _lock:
            held = _records.pop(pgid, None)
            if held is not None:
                _write(held[0])
    except Exception as exc:  # noqa: BLE001 - nor a close
        _warn_once(exc)


def _write(path: Path) -> None:
    held = [(pgid, host, entry) for pgid, (where, host, entry) in _records.items() if where == path]
    if not held:
        remove_with_lock(path)
        return
    payload = {**held[0][1], "groups": {str(pgid): entry for pgid, _, entry in held}}
    atomic_replace(path, json.dumps(payload, indent=1))


def _warn_once(exc: BaseException) -> None:
    global _warned
    if not _warned:
        _warned = True
        logger.warning("acp: cannot keep launch records ({}); this host's agents will not be swept if it dies", exc)


async def sweep_before_first_launch() -> None:
    """End what dead hosts left behind before a launch starts its agent; the sweep runs once per process.

    It runs in a daemon thread of its own. Every launch that arrives while it runs
    waits for it, none for longer than ``_SWEEP_WAIT_S``, and once one has waited
    that long the rest stop waiting: a sweep stuck reading a process holds neither
    the launches nor, being a daemon, the host's exit.
    """
    global _swept
    if not _on_linux():
        return
    done = _sweep_done
    if not _swept:
        _swept = True
        _start_sweep(done)
    deadline = time.monotonic() + _SWEEP_WAIT_S
    while not done.is_set() and not _sweep_abandoned:
        if time.monotonic() >= deadline:
            _abandon_sweep()
            return
        await asyncio.sleep(_SWEEP_POLL_S)


def _start_sweep(done: threading.Event) -> None:
    try:
        threading.Thread(target=_sweep_in_background, args=(done,), name="acp-sweep", daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - nor may the sweep fail a launch
        logger.debug("acp: could not start the launch-record sweep: {}", exc)
        done.set()


def _sweep_in_background(done: threading.Event) -> None:
    try:
        _sweep()
    except Exception as exc:  # noqa: BLE001 - nor may the sweep fail a launch
        logger.debug("acp: could not sweep launch records: {}", exc)
    finally:
        done.set()


def _abandon_sweep() -> None:
    global _sweep_abandoned
    if not _sweep_abandoned:
        _sweep_abandoned = True
        logger.warning(
            "acp: launch records still being swept after {} s; starting agents without waiting for it", _SWEEP_WAIT_S
        )


def _sweep() -> None:
    own = _host()
    if own is None:
        return
    own_stem = _stem(own["host_pid"], own["host_start"])
    try:
        paths = sorted(get_runtime_subdir(_DIR_NAME).glob("*.json"))
    except OSError:
        return
    for path in paths:
        if path.stem == own_stem:
            continue
        try:
            _sweep_file(path, own)
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the rest
            logger.debug("acp: could not sweep launch record {}: {}", path.name, exc)


def _sweep_file(path: Path, own: dict[str, Any]) -> None:
    # A file that is gone was swept by another host; any other failure to read it
    # raises, which leaves it for a later sweep.
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        remove_with_lock(path)
        return
    # Another version of Raven may shape its records differently, and only a
    # Raven that knows the shape may act on one or remove it.
    if data.get("version") != _VERSION:
        return
    try:
        host_pid, host_start = int(data["host_pid"]), int(data["host_start"])
        groups = {int(pgid): (int(e["leader_start"]), str(e.get("agent", "?"))) for pgid, e in data["groups"].items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        remove_with_lock(path)
        return
    if data.get("boot_id") != own["boot_id"]:
        # Another boot is this machine before a restart, or another machine that
        # shares this directory and whose host may still be running. Only a record
        # older than this boot is removed: this machine's own from before the
        # restart, and on a shared directory one from elsewhere just as old.
        if _written_before_boot(path):
            remove_with_lock(path)
        return
    # Another PID namespace sharing this directory: its host may well be alive,
    # and nothing here can tell, so its file is neither acted on nor removed.
    if data.get("pid_ns") != own["pid_ns"]:
        return
    if _start_ticks(host_pid) == host_start:
        return
    mark = f"{_MARK}={_stem(host_pid, host_start)}".encode()
    for pgid, (leader_start, agent) in groups.items():
        _end_group(pgid, leader_start, agent, host_pid, mark)
    remove_with_lock(path)


def _end_group(pgid: int, leader_start: int, agent: str, host_pid: int, mark: bytes) -> None:
    # killpg(0) signals the caller's own group, and group 1 is init's.
    if pgid <= 1 or pgid == os.getpgrp():
        return
    started = _start_ticks(pgid)
    if started is not None and started != leader_start:
        return
    # The number alone does not show the group is the one recorded: it may have
    # ended and the number gone to a new group. A member with the host's mark does.
    if not any(_carries(pid, mark) for pid in _members(pgid)):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.warning("acp: not allowed to end leftover agent {!r} (process group {})", agent, pgid)
        return
    logger.info(
        "acp: ended leftover agent {!r} (process group {}) from host {}, which is no longer running",
        agent,
        pgid,
        host_pid,
    )


def _members(pgid: int) -> list[int]:
    """The processes now in group ``pgid``."""
    members: list[int] = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            fields = _stat_fields(int(name))
        except PermissionError:
            # Closed to this user under hidepid (another user's, or an undumpable one):
            # nothing here can show it is this host's.
            continue
        if fields is not None and int(fields[2]) == pgid:
            members.append(int(name))
    return members


def _carries(pid: int, mark: bytes) -> bool:
    """Whether ``pid`` was started with ``mark`` in its environment.

    A process that is gone carries nothing, and neither does one whose environment
    this user may not read -- another user's, or one that made itself undumpable as
    ssh-agent does -- since nothing here can show it is this host's. Any other
    failure to read it raises, which leaves the record for a later sweep.
    """
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return mark in f.read().split(b"\0")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
