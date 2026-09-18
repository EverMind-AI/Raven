"""Whether a process id still names a running process.

``os.kill(pid, 0)`` is the POSIX existence probe, and on Windows it is not a
probe at all. CPython implements ``os.kill`` there as ``TerminateProcess``, so
signal 0 never reaches a liveness check: every pid but the caller's own comes
back ``OSError`` with ``ERROR_INVALID_PARAMETER``. Read as "gone" -- which is
what the hand-rolled probes in this tree did -- it makes a running process
invisible, and a caller that cannot see a process cannot stop one.

Windows answers the question with ``OpenProcess`` and a zero-timeout wait, the
pair ``raven/updates/upgrade.py`` already uses to watch a parent exit.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

_ERROR_ACCESS_DENIED = 5
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a process that has not exited.

    A process owned by another account counts as alive. The caller cannot
    signal it, but reporting it gone is what starts a second copy beside it.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _alive_windows(pid)
    return _alive_posix(pid)


def _alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _win32_api() -> tuple[Any, Callable[[], int]]:  # pragma: no cover - WinDLL does not load off Windows
    """kernel32 with the three calls bound, and the reader for its error code.

    Split from the decision below so the decision can be exercised anywhere.
    This half is ctypes declarations and nothing else, and it cannot run on the
    platform that measures coverage.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32, ctypes.get_last_error


def _alive_windows(pid: int) -> bool:
    kernel32, last_error = _win32_api()

    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        # Only one refusal means the process is there: a handle this account
        # may not open. No such pid, and a pid already reaped, both land here
        # too, and both are dead.
        return last_error() == _ERROR_ACCESS_DENIED
    try:
        # A process handle signals when the process exits, so a wait that times
        # out at once is the liveness answer. GetExitCodeProcess would serve
        # except in one case: a process whose real exit code is 259 cannot be
        # told apart from STILL_ACTIVE.
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)
