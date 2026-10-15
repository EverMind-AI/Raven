"""Keep each Windows ACP process tree in a job owned only by its Raven host.

The launcher reads one unbuffered byte before starting the configured command.
Raven sends it only after assigning the launcher to the job. If Raven dies
before assignment, stdin reaches EOF and the launcher exits without starting
an agent; afterwards, closing the host's job handle ends the entire tree.
After spawning, the launcher closes its own pipe descriptors so an agent that
closes stdout still delivers EOF while it remains running.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import subprocess
import sys
from typing import Any

_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100

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
