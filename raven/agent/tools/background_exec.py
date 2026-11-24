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

import asyncio
import itertools
import json
import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from raven.agent import turn_clock
from raven.contracts.tool import ToolOutput
from raven.home import raven_home

_DIRNAME = "background"

_CONFIRM_S = 0.9
"""How long a launch waits to be contradicted before it calls the task running.

A command that fails at once fails for a reason the model needs: a port already
bound, a missing binary, a path that does not exist. Reported as "started", the
reason is nowhere -- the model believes a server is up that never bound, and
looks for the fault in its client. Long enough for a ``http.server`` that loses
the port to say so, short enough that every successful launch still returns in
well under a second.

The check cannot prove the task will keep running, only that it has not already
stopped, and the note says so rather than promising more.
"""

_NOTE_TAIL_BYTES = 2000
"""How much of a dead task's log the launch note quotes back.

The failure is at the end, and a command that died in the first second cannot
have written much; 2000 covers a Python traceback whole, where a generous cap
would paste a wall of startup output into the model's context to say the same
thing."""

LOG_MAX_BYTES = 8 * 1024 * 1024
"""How large a harness-pumped log may grow before it is rotated.

A command adopted past its limit keeps writing for as long as it runs, and a
chatty one (a download's progress bar, a build's warnings) can write without
bound. The adopter keeps reading the pipe after this point -- a child blocked
on a full pipe would stall the command -- but drops the bytes on the floor and
writes one rotation marker, so the file stays bounded and its readable tail is
the recent output rather than the opening lines.
"""

LOG_ROTATE_MARK = b"\n[earlier output discarded; the log rotated at its size cap]\n"


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
    session_key: str | None = None
    execution_id: str | None = None


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

#: Distinguishes two tasks adopted or started in the same millisecond by one
#: process. The wall clock and pid alone collide there, and the second record
#: would overwrite the first one's log path while both commands keep running.
_TASK_IDS = itertools.count(1)


def _new_task_id() -> str:
    return f"bg-{int(time.time() * 1000):x}-{os.getpid():x}-{next(_TASK_IDS):x}"


def _reap_own() -> None:
    for task_id in _OWN:
        reap(task_id)


def start(
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    session_key: str | None = None,
) -> BackgroundTask:
    """Spawn ``command`` detached, logging to a managed file, and record it.

    ``start_new_session`` puts the child in its own process group, so a stray
    signal to this process does not take the job with it, and reaping can
    target the whole group. Output is line-buffered to the log via the shell,
    the same ``shell=True`` shape ``exec`` uses locally so pipes and ``&&``
    chains keep working.
    """
    _register_reaper()
    task_id = _new_task_id()
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
    task = BackgroundTask(
        task_id,
        command,
        proc.pid,
        str(log),
        time.time(),
        session_key or turn_clock.session_key() or None,
        turn_clock.execution_id(),
    )
    _record(task)
    return task


def _record(task: BackgroundTask) -> None:
    _meta_path(task.task_id).write_text(
        json.dumps(
            {
                "task_id": task.task_id,
                "command": task.command,
                "pid": task.pid,
                "log_path": task.log_path,
                "started_at": task.started_at,
                "session_key": task.session_key,
                "execution_id": task.execution_id,
            }
        ),
        encoding="utf-8",
    )


def adopt(
    command: str,
    pid: int,
    *,
    log_path: str | None = None,
    session_key: str | None = None,
    started_at: float | None = None,
) -> BackgroundTask:
    """Take custody of a process that is already running.

    A command that outlived its call is still this session's work. Whoever
    adopted it keeps writing its output to ``log_path``; this only records it,
    so ``running``/``status``/``tail``/``reap`` see it like any other task.
    """
    _register_reaper()
    task_id = _new_task_id()
    task = BackgroundTask(
        task_id,
        command,
        pid,
        log_path or str(_log_path(task_id)),
        started_at or time.time(),
        session_key or turn_clock.session_key() or None,
        turn_clock.execution_id(),
    )
    _OWN.append(task_id)
    _record(task)
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
    """The end of a task's log, or "" when there is none yet.

    Read from the record rather than derived from the id: an adopted task's
    output goes to the file its original caller was already writing. Sought to
    the end first, so a large log costs one read of ``max_bytes`` rather than a
    whole-file read whose front is then thrown away.
    """
    data = status(task_id) or {}
    log = Path(str(data.get("log_path") or _log_path(task_id)))
    if not log.exists():
        return ""
    try:
        with log.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - max_bytes))
            raw = handle.read(max_bytes)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def running(session_key: str | None = None, *, execution_id: str | None = None) -> list[dict]:
    """Every recorded task whose process is still alive, optionally one session's.

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
        if session_key is not None and data.get("session_key") != session_key:
            continue
        if execution_id is not None and data.get("execution_id") != execution_id:
            continue
        if _alive(task_id, int(data.get("pid") or -1)):
            out.append(data)
        elif task_id not in _PROCS:
            _retire(task_id)
    return out


def reap(task_id: str, *, force: bool = False) -> bool:
    """Kill a task's whole process group if it is still alive; return True if it was."""
    data = status(task_id)
    if not data:
        return False
    pid = int(data.get("pid") or -1)
    if not _alive(task_id, pid):
        return False
    sig = getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM
    if hasattr(os, "killpg"):
        # The group is the task's own (see _alive), so this reaches the whole
        # tree and nothing else. No fallback to a bare kill(pid): a pid that
        # no longer leads its group is not this task any more.
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True
    try:
        os.kill(pid, sig)
    except OSError:
        return False
    return True


async def reap_execution(session_key: str, execution_id: str) -> str:
    """Wait for this execution's background commands to stop and return their log tails."""
    tasks = running(session_key, execution_id=execution_id)
    if not tasks:
        return ""
    for task in tasks:
        reap(task["task_id"])

    async def wait() -> None:
        while running(session_key, execution_id=execution_id):
            await asyncio.sleep(0.05)

    try:
        async with asyncio.timeout(2):
            await wait()
    except TimeoutError:
        for task in running(session_key, execution_id=execution_id):
            reap(task["task_id"], force=True)
        try:
            async with asyncio.timeout(2):
                await wait()
        except TimeoutError:
            # A process that survives SIGKILL (an uninterruptible sleep, a child
            # outside the shell on Windows) must not fail the turn that asked for
            # its final answer: the tail below is still the best account of the
            # work, and the survivor is named for whoever reads the log.
            survivors = [str(task.get("task_id")) for task in running(session_key, execution_id=execution_id)]
            logger.warning(
                "background commands for execution {} survived SIGKILL: {}",
                execution_id,
                ", ".join(survivors) or "none listed",
            )

    tails = []
    for task in tasks[:8]:
        try:
            with Path(task["log_path"]).open("rb") as log:
                size = log.seek(0, os.SEEK_END)
                log.seek(max(0, size - _NOTE_TAIL_BYTES))
                tail_text = log.read(_NOTE_TAIL_BYTES).decode("utf-8", errors="replace")
            if tail_text.strip():
                tails.append(f"{task['task_id']}:\n{tail_text}")
        except (KeyError, OSError):
            continue
    return "\n\n".join(tails)


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


def start_note(task: BackgroundTask) -> ToolOutput:
    """What a background launch tells the model: the handle, and how to read it.

    Or, when the task was already gone by the time this is built, what it said
    on the way out. ``start`` returns as soon as ``Popen`` does, which is before
    the command has run a line: a command that fails immediately -- port taken,
    binary missing -- is reported as running by a note that never looks, and the
    model then hunts for its own bug in a server that never bound (measured
    2026-09-22, session 692e5c: ``http.server 8765`` lost the port and the launch
    still read as success).

    The verdict travels with the text: a command that already exited nonzero is
    a failed call, the same answer the synchronous lane gives, and one that
    exited 0 in the window did what it was asked.
    """
    proc = _PROCS.get(task.task_id)
    if proc is None:
        return ToolOutput(_running_note(task))
    try:
        code = proc.wait(timeout=_CONFIRM_S)
    except subprocess.TimeoutExpired:
        # Still alive, which is all this window can establish.
        return ToolOutput(_running_note(task))
    return ToolOutput(_exited_note(task, code), ok=code == 0)


def _running_note(task: BackgroundTask) -> str:
    return (
        f"Started in the background as {task.task_id} (pid {task.pid}). It runs on THIS "
        f"computer, detached, logging to {task.log_path}. This turn is not held open for "
        f"it: read the log with exec (`tail {shlex.quote(task.log_path)}`) when you next "
        f"want to know -- later in this turn, or on a later one (a one-shot reminder "
        f"naming {task.task_id} brings you back if nothing else will). It is tracked and "
        f"will be reaped if the session ends."
    )


def _exited_note(task: BackgroundTask, code: int) -> str:
    """The launch note for a command that ended before it could be reported.

    The exit code and the log's tail are the whole diagnosis: nothing has
    happened since the command ran, so whatever went wrong is in there. Named as
    an ending rather than a "failure", because a command that exits 0 within the
    window did exactly what it was asked and simply had nothing to do in the
    background.
    """
    if code < 0:
        try:
            ended = f"killed by {signal.Signals(-code).name}"
        except ValueError:  # pragma: no cover - a signal this platform does not have
            ended = f"killed by signal {-code}"
    else:
        ended = f"exited with code {code}"
    output = tail(task.task_id, max_bytes=_NOTE_TAIL_BYTES).strip()
    ended_line = "It wrote nothing to its log."
    if output:
        ended_line = f"Its log ({task.log_path}) ends with:\n\n{output}"
    return (
        f"The background command ended immediately: `{task.command}` ({task.task_id}, "
        f"pid {task.pid}) {ended} before the launch could report it as running, so it is "
        f"not running now.\n\n{ended_line}\n\n"
        "Nothing is running and nothing will be reaped later. If the command was meant to "
        "keep running, fix what the output names and start it again."
    )
