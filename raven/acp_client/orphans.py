"""Reaping acp agents their host never closed.

:meth:`AcpClient.launch` starts every agent in a session of its own
(``start_new_session=True``) so the group can be killed as one unit, and the
only kill path is ``close()``. A host that dies without reaching it -- ``kill
-9``, the OOM killer, a crash -- leaves the agent running in a session nothing
belongs to anymore: it receives none of the host's signals and, if it does not
exit on stdin EOF, it never exits (issue #928: two third-party agents
survived 22 days at 523 MiB PSS). This module is the second line of defense.

Every launch records its agent (pgid, owner pid, process start time) in a
small registry under raven's data dir, and every launch first sweeps that
registry, killing the agents whose owner is gone. The start time is what
makes the sweep safe to run late: on POSIX a pid is recycled freely, so "a
process with that pid exists" proves nothing -- only "the process with that
pid is the same process we started" does. A reused pid reads as a different
start time and the stale record is dropped, not acted on.

Windows is a different shape and is deliberately not implemented here
(measured on neither): the right tool is a Job Object, not a registry file --
at launch, ``CreateJobObjectW`` + ``SetInformationJobObject`` with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` + ``AssignProcessToJobObject(job,
proc handle)``. When the host dies the kernel tears down its handle table,
the last job handle closes, and the job's every process is terminated with
it: the OS itself is the registry, no sweep is needed, and a clean close
keeps the ``taskkill /T /F`` path #908 already built. Until that lands the
sweep below reports nothing on Windows rather than guessing at identity.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from raven.utils.pid import pid_alive

__all__ = ["process_start_time", "record_agent", "registry_path", "sweep_orphans"]

logger = logging.getLogger(__name__)

#: The registry keeps the most recent launches only -- it is a breadcrumb
#: trail for a crashed host, not a history.
_MAX_RECORDS = 64

#: Start times are compared with a tolerance wide enough for the coarsest
#: clock in play (Linux reports ticks of ``USER_HZ``; darwin reports
#: microseconds), and no wider than a pid-reuse window needs to worry about.
_START_TIME_TOLERANCE_S = 2.0


def registry_path() -> Path:
    """Where the launch records live, under the instance's data dir."""
    from raven.config.paths import get_runtime_subdir

    return get_runtime_subdir("acp") / "agents.json"


def process_start_time(pid: int) -> float | None:
    """When the process ``pid`` started, as seconds since the epoch.

    ``None`` on a process that no longer exists, and on a platform whose
    start time cannot be read -- the sweep's cue to leave the pid alone, since
    without a start time a live pid cannot be told apart from a reused one.
    """
    if pid <= 0:
        return None
    if sys.platform == "darwin":
        return _start_time_darwin(pid)
    if sys.platform.startswith("linux"):
        return _start_time_linux(pid)
    return None  # pragma: no cover - win32 has no sweep to serve


def _start_time_darwin(pid: int) -> float | None:
    """``proc_pidinfo``'s ``PROC_PIDTBSDINFO``, whose start time is exact.

    Read as ctypes against libproc: a dead pid makes the call return 0, which
    is the "gone" answer the sweep wants, distinct from a reused pid that
    simply reports a different start time.
    """
    import ctypes

    MAXCOMLEN = 16  # noqa: N806 - reads as the libproc constant it mirrors

    class ProcBsdinfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * MAXCOMLEN),
            ("pbi_name", ctypes.c_char * (2 * MAXCOMLEN)),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    libproc = ctypes.CDLL("libproc.dylib", use_errno=True)
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int

    info = ProcBsdinfo()
    PROC_PIDTBSDINFO = 3  # noqa: N806 - reads as the libproc constant it mirrors
    got = libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    if got != ctypes.sizeof(info):
        return None
    return float(info.pbi_start_tvsec) + float(info.pbi_start_tvusec) / 1e6


def _start_time_linux(pid: int) -> float | None:
    """Field 22 of ``/proc/<pid>/stat``, in ticks since boot.

    Converted to wall time with ``/proc/uptime`` the way ``ps`` does: the
    boot time itself is never read, only the offset -- exact enough for the
    tolerance above.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes()
        # The comm field may contain spaces and parentheses; everything after
        # its final ')' is fixed order.
        fields = stat[stat.rindex(b")") + 2 :].split()
        starttime_ticks = int(fields[20])  # field 22 overall; 21 fields precede it after comm
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return max(0.0, uptime - starttime_ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return None


def record_agent(name: str, pgid: int) -> None:
    """Record a freshly launched agent for a later sweep to find.

    Best-effort by design: a failed record only means one agent can outlive a
    crashed host exactly as it does today, so the launch never fails here.
    """
    try:
        records = _read_registry()
        records.append(
            {
                "name": name,
                "pgid": pgid,
                "owner_pid": os.getpid(),
                "start_time": process_start_time(pgid),
                "recorded_at": time.time(),
            }
        )
        _write_registry(records[-_MAX_RECORDS:])
    except Exception:  # noqa: BLE001 - see docstring
        logger.debug("acp orphan registry: cannot record agent {!r} (pgid {})", name, pgid)


def sweep_orphans() -> list[int]:
    """Kill the recorded agents whose owner is gone; drop the rest's records.

    Returns the pgids killed. A record is dropped in every case its owner is
    dead: the agent was just killed, is already gone, or its pid was reused
    since -- and a dead owner means no later sweep could tell those apart any
    better than this one. Records whose owner still lives are kept: that host
    is responsible for its agents and will kill them through ``close()``.
    """
    records = _read_registry()
    if not records:
        return []
    killed: list[int] = []
    kept: list[dict] = []
    for record in records:
        pgid = int(record.get("pgid", 0))
        owner = int(record.get("owner_pid", 0))
        if not pgid or pgid == os.getpid():
            continue
        if pid_alive(owner):
            kept.append(record)
            continue
        started = process_start_time(pgid)
        recorded = record.get("start_time")
        if started is not None and recorded is not None and abs(started - float(recorded)) <= _START_TIME_TOLERANCE_S:
            try:
                os.killpg(pgid, signal.SIGKILL)
                killed.append(pgid)
                logger.info("acp orphan sweep: killed agent {!r} (pgid {})", record.get("name"), pgid)
            except (ProcessLookupError, PermissionError):
                pass
    _write_registry(kept)
    return killed


def _read_registry() -> list[dict]:
    try:
        data = json.loads(registry_path().read_text())
    except (OSError, ValueError):
        return []
    return [r for r in data.get("agents", []) if isinstance(r, dict)]


def _write_registry(records: list[dict]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"agents": records}, indent=2))
    os.replace(tmp, path)  # atomic: a concurrent launch reads all or nothing
