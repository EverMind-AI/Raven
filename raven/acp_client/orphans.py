"""Agents a dead host left running, and the sweep that ends them.

An ACP agent is started with ``start_new_session=True``, so it receives none of
its host's signals; the only kill paths are ``AcpClient.close`` and the teardowns
that reach ``close_pool``. A host killed, crashed, or OOM-killed reaches neither,
and the agent stays -- reparented to init, holding its memory, until someone
kills it by hand (issue #928: two agents, 22 days, 523 MiB PSS).

The fix is a record and a sweep. Every launch records its process group with the
start times of both the agent and the host; the next launch sweeps the record,
ending the groups whose host is gone. The start times are what make "gone"
judgable across pid reuse: a pid matches only if the OS reports the same start
time the record holds, so a recycled pid never inherits an old record -- neither
to spare an orphan nor to kill a stranger.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.atomic_io import atomic_update

# Start times are compared only against reads from the same kernel, and the
# registry lives in this machine's temp dir, so linux's boot-relative value
# never meets darwin's clock-of-day one.
_Entry = dict[str, Any]


def registry_path() -> Path:
    """One registry per user in the temp dir: hosts and agents run as that user."""
    uid = str(os.getuid()) if hasattr(os, "getuid") else "win"
    return Path(tempfile.gettempdir()) / f"raven-acp-agents-{uid}.json"


def process_start(pid: int) -> float | None:
    """When ``pid`` started, in the host kernel's own units, or None if unknown.

    Unknown never kills: the sweep treats an unreadable start time as a record
    it cannot judge and leaves the group alone.
    """
    if pid <= 0:
        return None
    if sys.platform == "darwin":
        return _start_darwin(pid)
    if sys.platform.startswith("linux"):
        return _start_linux(pid)
    return None


def _start_darwin(pid: int) -> float | None:
    """The start time via ``sysctl`` ``KERN_PROC_PID``, the only table macOS has.

    The kernel answers a pid it does not know with success and a zeroed record
    (measured: a dead pid comes back tv_sec 0), so a non-positive time reads as
    absent rather than as an epoch.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        buf = ctypes.create_string_buffer(648)  # sizeof(struct kinfo_proc), LP64
        size = ctypes.c_int(ctypes.sizeof(buf))
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
            return None
        tv_sec = int.from_bytes(buf.raw[0:4], sys.byteorder, signed=True)
        tv_usec = int.from_bytes(buf.raw[4:8], sys.byteorder, signed=True)
        if tv_sec <= 0:
            return None
        return tv_sec + tv_usec / 1e6
    except OSError:
        return None


def _start_linux(pid: int) -> float | None:
    """Field 22 of ``/proc/<pid>/stat``: clock ticks since boot."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        # comm may carry spaces and parens; the fields after the last ')' are stable.
        after = stat.rpartition(")")[2].split()
        ticks = float(after[19])  # field 22 overall: the 20th after "pid" and "(comm)"
        return ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return None


def _is_recorded_process(pid: int, start: Any) -> bool:
    """Whether ``pid`` is running and is the process the record named."""
    if not isinstance(start, (int, float)) or pid <= 0:
        return False
    now = process_start(pid)
    return now is not None and abs(now - start) < 0.5


def record_agent(name: str, agent_pid: int) -> None:
    """Record one launched group so a later host can sweep it if this host dies.

    ``pgid`` is the agent pid itself: the launch sets a fresh session, so the
    child leads its own group.
    """

    def update(raw: str | None) -> tuple[str | None, None]:
        data = [e for e in _read(raw) if e.get("pgid") != agent_pid]
        data.append(
            {
                "name": name,
                "pgid": agent_pid,
                "agent_pid": agent_pid,
                "agent_start": process_start(agent_pid),
                "host_pid": os.getpid(),
                "host_start": process_start(os.getpid()),
            }
        )
        return json.dumps(data), None

    atomic_update(registry_path(), update)


def forget(agent_pid: int) -> None:
    """Drop this host's record of ``pgid`` -- the group was closed on purpose."""

    def update(raw: str | None) -> tuple[str | None, None]:
        data = [e for e in _read(raw) if e.get("pgid") != agent_pid]
        return json.dumps(data), None

    atomic_update(registry_path(), update)


def sweep_orphans() -> int:
    """End the recorded groups whose host is gone. Returns groups ended.

    A record is an orphan when its host is no longer the process it named --
    dead, or a reused pid with a different start time. Its agent is killed only
    if it too is still the process recorded: a finished agent's pid may already
    have been recycled, and a stranger never gets a ``killpg``. Records whose
    host still lives are kept -- that host may yet die unwound, and a later
    host must see them. Windows records ride along but are never judged here:
    no start-time source and no ``killpg``; closing them cleanly is the Job
    Object work tracked in #908.
    """
    if not hasattr(os, "killpg"):
        return 0

    def update(raw: str | None) -> tuple[str | None, int]:
        kept: list[_Entry] = []
        ended = 0
        me = os.getpid()
        for entry in _read(raw):
            host_pid = entry.get("host_pid", 0)
            host_start = entry.get("host_start")
            # A start time that matches keeps the record, whoever it names:
            # a live host, or this process's own launch. The pid alone proves
            # nothing -- this process may be wearing a dead host's recycled
            # pid, which is exactly the record the start time must veto.
            if _is_recorded_process(host_pid, host_start):
                kept.append(entry)
                continue
            # Unjudgeable is kept: this process's own record whose start time
            # could not be read at launch. Killing it would be self-harm.
            if host_pid == me and host_start is None:
                kept.append(entry)
                continue
            if _is_recorded_process(entry.get("agent_pid", 0), entry.get("agent_start")):
                try:
                    os.killpg(entry["pgid"], signal.SIGKILL)
                    ended += 1
                    logger.info(
                        "acp orphan sweep: ended agent {!r} (group {}) of dead host pid {}",
                        entry.get("name"),
                        entry["pgid"],
                        host_pid,
                    )
                except (ProcessLookupError, PermissionError):
                    pass
        return json.dumps(kept), ended

    return atomic_update(registry_path(), update)


def _read(raw: str | None) -> list[_Entry]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []
