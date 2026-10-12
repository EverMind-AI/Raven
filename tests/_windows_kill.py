"""The kill path a Windows host takes, reproduced on this one.

Windows has no ``os.killpg`` and starts no process group, so code that ends a
child's whole tree there has to ask ``taskkill /T`` instead. Removing ``killpg``
sends a test down that branch on any host; ``taskkill`` itself is recorded
rather than run, since there is none here to run.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

TASKKILL = r"C:\Windows\System32\taskkill.exe"


def windows_kill_path(monkeypatch: pytest.MonkeyPatch, *, fails: BaseException | None = None) -> list[list[str]]:
    """Remove ``os.killpg``, put ``taskkill`` on PATH, and return the calls made to it.

    Every other ``subprocess.run`` still runs: the login-shell capture a spawn
    reads its environment from goes through it too. With ``fails``, each call to
    ``taskkill`` is recorded and then raises it.
    """
    monkeypatch.delattr(os, "killpg")
    real_which, real_run = shutil.which, subprocess.run
    calls: list[list[str]] = []

    def which(name, *args, **kwargs):
        return TASKKILL if name == "taskkill" else real_which(name, *args, **kwargs)

    def run(argv, *args, **kwargs):
        if argv and argv[0] == TASKKILL:
            calls.append(list(argv))
            if fails is not None:
                raise fails
            return subprocess.CompletedProcess(argv, 0)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", run)
    return calls
