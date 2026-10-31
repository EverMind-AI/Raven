"""Keep each Windows ACP process tree in a job owned only by its Raven host.

The launcher reads one unbuffered byte before starting the configured command.
Raven sends it only after assigning the launcher to the job. If Raven dies
before assignment, stdin reaches EOF and the launcher exits without starting
an agent; afterwards, closing the host's job handle ends the entire tree.

A job the agent hands off on purpose has to outlive the agent, and a job object
cannot let one child leave without letting every child leave: Git for Windows'
MSYS2 runtime adds CREATE_BREAKAWAY_FROM_JOB to every native program it starts
when the job allows breakaway (winsup/cygwin/spawn.cc), so an ordinary Bash
tool command would leak past the host. The job therefore stays closed to
breakaway, and the host offers a hand-off channel instead: the agent asks the
host, which is not in the job, to start the relay through ``_HandoffServer``.
The channel's address and key arrive in the agent's environment; a request
runs the relay with the environment the agent sent.
After spawning, the launcher closes its own pipe descriptors so an agent that
closes stdout still delivers EOF while it remains running.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import secrets
import shutil
import subprocess
import sys
import threading
from typing import Any

_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100

HANDOFF_PIPE = "RAVEN_ACP_HANDOFF_PIPE"
"""The environment variable naming the host's hand-off pipe, set on every agent."""
HANDOFF_KEY = "RAVEN_ACP_HANDOFF_KEY"
"""The environment variable holding the pipe's authentication key, as hex."""

_LAUNCHER = """\
import ctypes, os, subprocess, sys
if os.read(0, 1) != b'1':
    sys.exit(125)
try:
    child = subprocess.Popen(
        sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
except OSError as exc:
    print(f'raven: cannot start {sys.argv[1]}: {exc}', file=sys.stderr, flush=True)
    sys.exit(127)
for fd in (0, 1, 2):
    os.close(fd)
os._exit(ctypes.c_int(child.wait()).value)
"""


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32() -> Any:  # pragma: no cover - native Windows bindings
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    api.CreateJobObjectW.restype = ctypes.c_void_p
    api.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    api.SetInformationJobObject.restype = ctypes.c_int
    api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.OpenProcess.restype = ctypes.c_void_p
    api.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    api.AssignProcessToJobObject.restype = ctypes.c_int
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    return api


def _win_error() -> OSError:  # pragma: no cover - native Windows error translation
    return ctypes.WinError(ctypes.get_last_error())


class JobObject:
    """An unnamed, non-inheritable kill-on-close job for one ACP connection."""

    def __init__(self) -> None:
        self._api = _kernel32()
        self._handle = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise _win_error()
        limits = _ExtendedLimits()
        # No breakaway: a job object's permission is all-or-nothing for the
        # tree, and Git for Windows spends it on every native child it starts
        # (see the module docstring). Work that must outlive the host is
        # started by the host itself, outside this job.
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not self._api.SetInformationJobObject(
            self._handle, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = _win_error()
            self.close()
            raise error

    def assign(self, pid: int) -> None:
        process = self._api.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            raise _win_error()
        try:
            if not self._api.AssignProcessToJobObject(self._handle, process):
                raise _win_error()
        finally:
            self._api.CloseHandle(process)

    def close(self) -> None:
        if self._handle is not None:
            if not self._api.CloseHandle(self._handle):
                raise _win_error()
            self._handle = None


class _HandoffServer:
    """Starts handed-off work outside this host's ACP job, on an agent's request.

    One per host process, created on the first launch. It is a thread of the
    host, not a child of the agent, so what it starts is outside every job
    object the host owns. The agent reaches it over a named pipe; ``launch``
    puts the pipe's name and key in the agent's environment.

    A request is ``{"argv": [...], "cwd": str, "env": {name: value}}``. The
    answer carries the relay's ``returncode``, ``stdout`` and ``stderr``, or
    ``error`` when it could not be started. The pipe's default security
    descriptor is the launching user's, and the key keeps another same-session
    process from using a name it guessed.
    """

    def __init__(self) -> None:
        from multiprocessing.connection import Listener

        self.address = f"\\\\.\\pipe\\raven-acp-handoff-{os.getpid()}-{secrets.token_hex(8)}"
        self._authkey = secrets.token_bytes(32)
        self._listener = Listener(self.address, family="AF_PIPE", authkey=self._authkey)
        threading.Thread(target=self._accept, name="raven-acp-handoff", daemon=True).start()

    @property
    def env(self) -> dict[str, str]:
        return {HANDOFF_PIPE: self.address, HANDOFF_KEY: self._authkey.hex()}

    def _accept(self) -> None:
        while True:
            try:
                connection = self._listener.accept()
            except (OSError, EOFError):
                return
            except Exception:
                # A failed handshake is one bad client, not a broken pipe.
                continue
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: Any) -> None:
        try:
            request = connection.recv()
            argv = [str(argument) for argument in request["argv"]]
            completed = subprocess.run(
                argv,
                cwd=request.get("cwd"),
                env=request.get("env") or None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # Windows-only, and this method is unit-tested on POSIX too.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=60,
                close_fds=True,
            )
            connection.send(
                {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
            )
        except Exception as exc:
            try:
                connection.send({"error": f"{type(exc).__name__}: {exc}"})
            except Exception:
                pass
        finally:
            try:
                connection.close()
            except Exception:
                pass


_IS_WINDOWS = sys.platform == "win32"
_HANDOFF: _HandoffServer | None = None
_HANDOFF_TRIED = False


def _handoff() -> _HandoffServer | None:
    """The host's hand-off server, started on first use, or None if it cannot be.

    A channel that cannot be opened must not keep an agent from starting: the
    on-call backend falls back to starting its job inside the job object and
    says so in the job's log.
    """
    global _HANDOFF, _HANDOFF_TRIED
    if not _IS_WINDOWS:
        return None
    if not _HANDOFF_TRIED:
        _HANDOFF_TRIED = True
        try:
            _HANDOFF = _HandoffServer()
        except Exception:
            _HANDOFF = None
    return _HANDOFF


def _program(argv: list[str], cwd: str | None) -> list[str]:
    program = argv[0]
    if os.path.dirname(program):
        program = os.path.abspath(os.path.join(cwd or os.getcwd(), program))
        if not os.path.splitext(program)[1]:
            program += ".exe"
        if not os.path.isfile(program):
            raise FileNotFoundError(f"program does not exist: {argv[0]}")
    else:
        program = shutil.which(program) or ""
        if not program:
            raise FileNotFoundError(f"program does not exist: {argv[0]}")
    return [program, *argv[1:]]


async def launch(argv: list[str], **kwargs: Any) -> tuple[asyncio.subprocess.Process, JobObject]:
    """Start a gated launcher, assign its job, then let it start the agent."""
    argv = _program(argv, kwargs.get("cwd"))
    # The agent's own environment is kept (it was handed in by the caller); the
    # hand-off channel is the host's, so a relay it starts runs outside this job.
    # A platform without AF_PIPE gets None from ``_handoff`` and no channel.
    child_env = dict(kwargs["env"]) if kwargs.get("env") is not None else dict(os.environ)
    handoff = _handoff()
    if handoff is not None:
        child_env.update(handoff.env)
    kwargs["env"] = child_env
    job = JobObject()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            getattr(sys, "_base_executable", None) or sys.executable,
            "-I",
            "-S",
            "-c",
            _LAUNCHER,
            *argv,
            creationflags=subprocess.CREATE_NO_WINDOW,
            **kwargs,
        )
        job.assign(proc.pid)
        if proc.stdin is None:
            raise ValueError("the ACP launcher needs a stdin pipe")
        proc.stdin.write(b"1")
        await proc.stdin.drain()
        return proc, job
    except BaseException:
        try:
            job.close()
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except OSError:
                    pass
                proc._transport.close()
        raise
