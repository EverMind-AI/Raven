"""Command runners for registered machines, so nothing above this has to know.

A machine the owner registered is not always a different computer. Someone who
writes their own solver usually runs it where they wrote it and installs raven
on that same box: there is no host to reach and no key to present, and a
command is simply run. Measured need, 2026-08-18 -- the CFD group's in-house
Fortran code lives on the machine raven would be installed on.

Everything above this seam names a connection and nothing else. Which runner
answers is a property of the row (``connections.transport_of``), so a tool
call reads the same whether the work runs across the room or in this process's
own filesystem.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from typing import Any

from raven.ops.connections import LOCAL, transport_of

CommandRunner = Callable[[str], tuple[int, str]]

# What ``timeout`` returns when it kills something, reused so a caller reads one
# code whichever way the cap was applied.
TIMED_OUT_RC = 124


class TransportError(RuntimeError):
    """A machine that cannot be reached from what its row says."""


def make_local_runner(cap_seconds: float = 600.0) -> CommandRunner:
    """Run the command here, answering in the shape an ssh runner answers in.

    ``shell=True`` because that is what the ssh runner effectively gives a
    caller: the remote sshd puts the string through a shell, so the commands
    written for it -- pipes, redirects, ``&&`` chains, heredocs -- keep working
    unchanged.

    The cap is enforced here rather than by wrapping the command in
    ``timeout``. That binary is GNU coreutils and macOS does not ship it,
    which is exactly the machine a local connection is most likely to be:
    measured 2026-08-19, the first local look came back
    ``exit 127, /bin/sh: timeout: command not found``.
    """

    def run(cmd: str) -> tuple[int, str]:
        # Its own session, so the cap can reach the whole tree. subprocess.run's
        # timeout kills the shell alone: a command that had forked (a python
        # that spawned a solver) kept running past the cap, past the return
        # code 124 that said it had been stopped, and outside anything this
        # channel tracks (reviewed 2026-09-04, reproduced with a sleep child).
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_own_group_kwargs(),
        )
        try:
            out, err = proc.communicate(timeout=cap_seconds)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                out, _err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # Something escaped the group and holds the pipe; the shell
                # itself is dead, and its output is whatever was collected.
                proc.kill()
                out = ""
            return TIMED_OUT_RC, out or ""
        tail = f"\n{err}" if err and proc.returncode != 0 else ""
        return proc.returncode, out + tail

    return run


def _own_group_kwargs() -> dict[str, Any]:
    """How this platform puts a child in a group of its own.

    POSIX: a new session, so the pid doubles as the group id ``killpg`` takes.
    Windows ignores ``start_new_session`` and has no ``killpg``; there the tree
    is killed by pid through ``taskkill``, which needs no group at all.
    """
    if hasattr(os, "killpg"):
        return {"start_new_session": True}
    return {}


def _kill_process_tree(proc: subprocess.Popen, grace_s: float = 2.0) -> None:
    """Stop the command and everything it started.

    POSIX: TERM the session, wait out a short grace, KILL what stayed.
    Windows: ``taskkill /T /F`` walks the child tree by pid -- the platform
    offers no group signal, and a bare ``Popen.kill`` would reach the shell
    alone, the very hole this exists to close (reviewed 2026-09-04: the first
    cut called ``os.killpg`` unconditionally and raised there instead of
    returning 124, leaving the command running).
    """
    if not hasattr(os, "killpg"):
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                subprocess.run(
                    [taskkill, "/T", "/F", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=grace_s + 5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def make_ssh_runner(
    host: str,
    port: int,
    key: str,
    *,
    user: str = "root",
    connect_timeout: int = 15,
) -> CommandRunner:
    def run(cmd: str) -> tuple[int, str]:
        argv = [
            "ssh",
            "-i",
            key,
            "-p",
            str(port),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={connect_timeout}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"{user}@{host}",
            cmd,
        ]
        proc = subprocess.run(argv, capture_output=True, text=True)
        tail = f"\n{proc.stderr}" if proc.stderr and proc.returncode != 0 else ""
        return proc.returncode, proc.stdout + tail

    return run


def runner_from(row: dict[str, Any], *, cap_seconds: float | None = None) -> CommandRunner:
    """The runner this connection row calls for.

    Raises rather than handing back something half-connected: a runner built on
    an empty address fails later with a message that reads as a network fault
    rather than as a row that was never told where to run. Measured 2026-08-17,
    and again 2026-08-18 when the failure reached the owner as "your machine
    refused the connection" while that machine was answering in under 200ms.
    """
    if transport_of(row) == LOCAL:
        # The caller's cap, not this module's default. Looking at a machine is
        # capped the same whichever way it is reached; without this the local
        # transport quietly allowed ten minutes, because the cap for a remote
        # look rides on the ``timeout`` wrapper and a local one has only the
        # runner.
        return make_local_runner(cap_seconds) if cap_seconds else make_local_runner()

    host = str(row.get("host") or "").strip()
    if not host:
        raise TransportError(
            f"connection {str(row.get('id') or '?')!r} has no address to run on: its row names no host"
        )
    return make_ssh_runner(
        host,
        int(row.get("port") or 22),
        os.path.expanduser(str(row.get("key") or "~/.ssh/id_rsa")),
        # A connection names the account to log in as. Dropping it would make
        # that field one more setting that is written, accepted and does nothing.
        user=str(row.get("user") or "root"),
    )
