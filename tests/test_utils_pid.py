"""The liveness probe, driven against processes the OS really has.

A mocked probe proves nothing here. The defect this file guards is that the
POSIX call does not mean on Windows what it says on POSIX, so the only test
that can catch it is one that asks about a real process -- and, on Windows,
one that is not the caller's own, since that is the single pid the broken
probe answered correctly.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from raven.utils import pid as pid_module
from raven.utils.pid import pid_alive


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # noqa: S603


def test_a_running_child_is_alive_and_survives_the_question() -> None:
    """Asking must not answer by killing.

    ``os.kill(pid, 0)`` on Windows is ``TerminateProcess``, so against a child
    the caller may terminate the old probe returned "alive" and left a corpse.
    Termination is asynchronous, which is why survival is asserted by a wait
    that must time out rather than by a ``poll`` taken straight after: the
    immediate read still says "running" for a process already on its way down.
    """
    proc = _sleeper()
    try:
        assert pid_alive(proc.pid) is True
        assert pid_alive(proc.pid) is True
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=1.0)
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_an_exited_child_is_not_alive() -> None:
    proc = _sleeper()
    proc.kill()
    proc.wait(timeout=10)

    assert pid_alive(proc.pid) is False


def test_this_process_is_alive() -> None:
    assert pid_alive(os.getpid()) is True


def test_a_pid_that_cannot_exist_is_not_alive() -> None:
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


class _FakeKernel32:
    """The three calls the probe binds, answering from a script.

    The real subprocess tests above are the ones that catch the defect; these
    drive the branches those tests cannot reach on the machine running them.
    An access-denied process needs another account, and the coverage platform
    has no kernel32 at all, so the decision is asked here with the syscalls
    standing still.
    """

    def __init__(self, handle: int, wait: int = 0) -> None:
        self._handle = handle
        self._wait = wait
        self.closed: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:  # noqa: N802 - the win32 name
        self.opened = (access, inherit, pid)
        return self._handle

    def WaitForSingleObject(self, handle: int, timeout: int) -> int:  # noqa: N802 - the win32 name
        self.waited = (handle, timeout)
        return self._wait

    def CloseHandle(self, handle: int) -> bool:  # noqa: N802 - the win32 name
        self.closed.append(handle)
        return True


def _with_fake(monkeypatch: pytest.MonkeyPatch, kernel32: _FakeKernel32, error: int = 0) -> None:
    monkeypatch.setattr(pid_module, "_win32_api", lambda: (kernel32, lambda: error))


def test_an_unopenable_handle_is_alive_only_when_the_refusal_is_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asymmetry the probe exists for.

    A handle this account may not open means the process is there; every other
    refusal, including no such pid and a pid already reaped, means it is gone.
    """
    denied = _FakeKernel32(handle=0)
    _with_fake(monkeypatch, denied, error=5)
    assert pid_module._alive_windows(4321) is True

    missing = _FakeKernel32(handle=0)
    _with_fake(monkeypatch, missing, error=87)
    assert pid_module._alive_windows(4321) is False


@pytest.mark.parametrize(
    ("wait", "alive"),
    [(0x00000102, True), (0, False)],
    ids=["timed-out-so-running", "signalled-so-exited"],
)
def test_the_wait_result_is_the_liveness_answer(wait: int, alive: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32 = _FakeKernel32(handle=99, wait=wait)
    _with_fake(monkeypatch, kernel32)

    assert pid_module._alive_windows(4321) is alive
    assert kernel32.waited == (99, 0), "the wait has to be the zero-timeout one, or the probe blocks"
    assert kernel32.closed == [99], "an opened handle leaks unless every path closes it"


def test_the_handle_is_closed_even_when_the_wait_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32 = _FakeKernel32(handle=99)
    monkeypatch.setattr(kernel32, "WaitForSingleObject", _raise_oserror)
    _with_fake(monkeypatch, kernel32)

    with pytest.raises(OSError, match="wait failed"):
        pid_module._alive_windows(4321)
    assert kernel32.closed == [99]


def _raise_oserror(*_args: object) -> int:
    raise OSError("wait failed")


def test_a_process_owned_by_another_account_is_alive_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PermissionError` is the POSIX half of the same asymmetry: the signal was
    refused, which is only possible against a process that exists."""

    def _refuse(_pid: int, _sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", _refuse)
    assert pid_module._alive_posix(4321) is True


def test_any_other_oserror_reads_as_gone_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(_pid: int, _sig: int) -> None:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(os, "kill", _fail)
    assert pid_module._alive_posix(4321) is False


def test_the_platform_decides_which_probe_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatch itself, which no single-platform run can cover both ways."""
    monkeypatch.setattr(pid_module, "_alive_windows", lambda pid: "windows")
    monkeypatch.setattr(pid_module, "_alive_posix", lambda pid: "posix")

    monkeypatch.setattr(sys, "platform", "win32")
    assert pid_alive(4321) == "windows"

    monkeypatch.setattr(sys, "platform", "linux")
    assert pid_alive(4321) == "posix"
