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
