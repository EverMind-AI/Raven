"""The kill path a Windows host takes, on Windows or reproduced on this host.

Windows has no ``os.killpg`` and starts no process group, so code that ends a
child's whole tree there asks ``taskkill /T`` instead. On Windows nothing is
replaced and the real ``taskkill`` runs. Elsewhere ``killpg`` is removed to send
the code down that branch, and ``taskkill`` is stood in for by ending the
process group it was asked about: the code under test starts its launcher in a
session of its own, so on this host that group is the launcher's tree. Either
way every call to ``taskkill`` is recorded, so a test asserts the same calls
and the same deaths on both.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from contextlib import suppress

import pytest

_STAND_IN = "taskkill"

#: The interpreter the processes these tests stop run on. A Windows venv's
#: ``python.exe`` is a redirector that starts this one as its child, so a spawn
#: of it hands back a pid the script inside never sees as its own.
PYTHON = sys._base_executable


def windows_kill_path(monkeypatch: pytest.MonkeyPatch, *, fails: BaseException | None = None) -> list[list[str]]:
    """Send a stop down the ``taskkill`` branch and return the arguments of each call to it.

    Every other ``subprocess.run`` still runs: the login-shell capture a spawn
    reads its environment from goes through it too. With ``fails``, each call
    to ``taskkill`` is recorded and then raises it, ending nothing.
    """
    real_run = subprocess.run
    calls: list[list[str]] = []
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        taskkill = shutil.which("taskkill")
        if taskkill is None:
            pytest.skip("taskkill is not on PATH")
    else:
        monkeypatch.delattr(os, "killpg")
        real_which = shutil.which
        taskkill = _STAND_IN

        def which(name, *args, **kwargs):
            return _STAND_IN if name == "taskkill" else real_which(name, *args, **kwargs)

        monkeypatch.setattr(shutil, "which", which)

    def run(argv, *args, **kwargs):
        if not argv or argv[0] != taskkill:
            return real_run(argv, *args, **kwargs)
        calls.append(list(argv[1:]))
        if fails is not None:
            raise fails
        if killpg is None:
            return real_run(argv, *args, **kwargs)
        with suppress(ProcessLookupError):
            killpg(int(argv[argv.index("/PID") + 1]), signal.SIGKILL)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    return calls
