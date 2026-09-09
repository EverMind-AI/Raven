"""Harness custody for a long local command: a tracked child, a managed log.

``exec`` runs a command synchronously and caps it, because a command that
outlives the call has nowhere to be while the loop waits. A download, an
``rsync`` of a code tree, a ``tar`` of a log directory -- long, mechanical,
and spending no GPU budget -- fits neither the cap (it is genuinely minutes)
nor ``ops_submit`` (that is for GPU jobs on registered machines, with a
ledger and a budget). This is that missing home: the command runs detached
on THIS computer, its output goes to a file the harness names, and the
process is tracked so it can be looked at and is reaped when the session
ends.

Why not ``nohup`` in the command string: nohup leaves work running but hands
back nothing. No task to look at, a log path the model invents and then
loses (measured 2026-09-02: a 257-byte crash log nobody was routed to), and
a child that outlives the session with no one tracking it (measured
2026-09-02: an orphan held a GPU at 100% after its caller was gone). The
custody here is the value; the boolean is only its handle.

Not a wake. The main loop is turn-based, so a task that finishes after the
turn ends has no live turn to notify; a caller keeps its turn alive and reads
the log, or checks back on a later turn. Work that needs to WAKE a watching
loop when it lands is a job for the on-call agent's ops_submit, not this.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from raven.home import raven_home

_DIRNAME = "background"


def _root() -> Path:
    root = raven_home() / _DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass(frozen=True)
class BackgroundTask:
    task_id: str
    command: str
    pid: int
    log_path: str
    started_at: float


def _meta_path(task_id: str) -> Path:
    return _root() / f"{task_id}.json"


def _log_path(task_id: str) -> Path:
    return _root() / f"{task_id}.log"


_REAP_REGISTERED = False


def _register_reaper() -> None:
    """Reap this process's own live tasks when the interpreter exits.

    Registered on the first start rather than at import, so a process that
    never launches a background task pays nothing. Only tasks THIS process
    started are reaped (the pid file records the launcher implicitly through
    liveness): a fresh session must not kill a survivor the operator chose to
    leave running from an earlier one -- ``running()`` shows those instead.
    """
    global _REAP_REGISTERED
    if _REAP_REGISTERED:
        return
    import atexit

    atexit.register(_reap_own)
    _REAP_REGISTERED = True


_OWN: list[str] = []

# This process's own Popen handles. Liveness must go through ``poll()`` for
# them: we are the parent, so a finished child sits as a zombie until waited
# on, and ``kill(pid, 0)`` reports a zombie alive forever. ``poll()`` reaps it
# on the way past. A task from another process has another parent to reap it,
# so the signal probe answers there -- but only about the pid, not about whose
# it is: a record no one ever retires goes on naming its pid after the kernel
# has handed that number to something else (reviewed 2026-09-07). So a foreign
# pid is believed only while it still leads its own process group, which is
# how ``start`` launched it and how a recycled pid almost never sits, and a
# record whose process is gone is retired the first time that is seen.
_PROCS: dict[str, subprocess.Popen] = {}


def _reap_own() -> None:
    for task_id in _OWN:
        reap(task_id)


def start(command: str, *, cwd: str | None = None, env: dict[str, str] | None = None) -> BackgroundTask:
    """Spawn ``command`` detached, logging to a managed file, and record it.

    ``start_new_session`` puts the child in its own process group, so a stray
    signal to this process does not take the job with it, and reaping can
    target the whole group. Output is line-buffered to the log via the shell,
    the same ``shell=True`` shape ``exec`` uses locally so pipes and ``&&``
    chains keep working.
    """
    _register_reaper()
    task_id = f"bg-{int(time.time() * 1000):x}-{os.getpid():x}"
    log = _log_path(task_id)
    handle = log.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd or None,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        handle.close()
    _OWN.append(task_id)
    _PROCS[task_id] = proc
    task = BackgroundTask(task_id, command, proc.pid, str(log), time.time())
    _meta_path(task_id).write_text(
        json.dumps(
            {
                "task_id": task_id,
                "command": command,
                "pid": proc.pid,
                "log_path": str(log),
                "started_at": task.started_at,
            }
        ),
        encoding="utf-8",
    )
    return task


def _alive(task_id: str, pid: int) -> bool:
    proc = _PROCS.get(task_id)
    if proc is not None:
        return proc.poll() is None
    if pid <= 0:
        return False
    return _leads_its_own_group(pid)


def _leads_its_own_group(pid: int) -> bool:
    """Whether ``pid`` exists and is the leader of its own process group.

    ``start`` gives every task a session of its own, so its pid is its group
    id for as long as it lives. A recycled pid is some other program's child,
    and sits in that program's group. Where there is no ``getpgid`` (Windows)
    the existence probe is all there is.
    """
    if not hasattr(os, "getpgid"):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    try:
        return os.getpgid(pid) == pid
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _retire(task_id: str) -> None:
    """Drop the record of a task whose process is gone. Its log stays readable."""
    try:
        _meta_path(task_id).unlink()
    except OSError:
        pass


def status(task_id: str) -> dict | None:
    """The task's record plus whether its process is still alive, or None."""
    meta = _meta_path(task_id)
    if not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    data["running"] = _alive(task_id, int(data.get("pid") or -1))
    return data


def tail(task_id: str, *, max_bytes: int = 20000) -> str:
    """The end of a task's log, or "" when there is none yet."""
    log = _log_path(task_id)
    if not log.exists():
        return ""
    text = log.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= max_bytes else text[-max_bytes:]


def running() -> list[dict]:
    """Every recorded task whose process is still alive.

    A record whose process is gone is retired here, so the set this globs
    does not grow for the life of the install and a stale record never gets
    to vouch for a recycled pid. This process's own finished tasks are kept
    until the session ends: their ``status`` is still asked for.
    """
    out: list[dict] = []
    for meta in sorted(_root().glob("bg-*.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task_id = str(data.get("task_id") or "")
        if _alive(task_id, int(data.get("pid") or -1)):
            out.append(data)
        elif task_id not in _PROCS:
            _retire(task_id)
    return out


def reap(task_id: str) -> bool:
    """Kill a task's whole process group if it is still alive; return True if it was."""
    data = status(task_id)
    if not data:
        return False
    pid = int(data.get("pid") or -1)
    if not _alive(task_id, pid):
        return False
    if hasattr(os, "killpg"):
        # The group is the task's own (see _alive), so this reaches the whole
        # tree and nothing else. No fallback to a bare kill(pid): a pid that
        # no longer leads its group is not this task any more.
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def reap_all() -> int:
    """Terminate every live background task, survivors from earlier sessions
    included. Returns how many were signalled.

    An in-process API, not yet an operator action: nothing in the product --
    no CLI command, RPC method or tool -- calls this at present (reviewed
    2026-09-07). Session exit reaps only this process's own tasks
    (``_reap_own``); a survivor from an earlier session is listed by
    ``running()`` and stopped by ``reap(task_id)`` from whatever door gets
    wired to them. Until one is, the operator's recourse is the process itself
    (the pid is in the record and the task leads its own group).
    """
    return sum(1 for data in running() if reap(str(data.get("task_id") or "")))


def start_note(task: BackgroundTask) -> str:
    """What a background launch tells the model: the handle, and how to read it."""
    return (
        f"Started in the background as {task.task_id} (pid {task.pid}). It runs on THIS "
        f"computer, detached, logging to {task.log_path}. This turn is not held open for "
        f"it: read the log with exec (`tail {shlex.quote(task.log_path)}`) when you next "
        f"want to know -- later in this turn, or on a later one (a one-shot reminder "
        f"naming {task.task_id} brings you back if nothing else will). It is tracked and "
        f"will be reaped if the session ends."
    )
