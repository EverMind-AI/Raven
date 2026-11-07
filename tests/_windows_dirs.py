"""Removing a directory the way Windows does, reproduced on any host.

Windows will not remove a directory that a live process has as its working
directory: the removal fails with ``WinError 32`` even when the directory is
empty. POSIX lets the process keep it, so on this host the order in which a
check ends its agent and removes its directory cannot go wrong by itself. This
stands the refusal in: while ``holding`` says a process still works in a
directory, removing it raises what Windows raises.
"""

from __future__ import annotations

import errno
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from raven.utils import scratch

SHARING_VIOLATION = "The process cannot access the file because it is being used by another process"


def scratch_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where every temporary directory is made from here on, so a test sees which are left.

    A refused removal is retried without the pause between tries, which is
    there for a process tree that is still exiting and has none to wait for here.
    """
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    monkeypatch.setattr(scratch, "_REMOVE_PAUSE_S", 0)
    return root


def windows_removal(monkeypatch: pytest.MonkeyPatch, holding: Callable[[Path], bool]) -> list[Path]:
    """Refuse to remove a directory while ``holding`` says it is in use; return each refused path.

    Only ``shutil.rmtree`` is replaced, which is what ``tempfile`` removes a
    temporary directory with, so every other removal still runs.
    """
    real_rmtree = shutil.rmtree
    refused: list[Path] = []

    def rmtree(path, *args, **kwargs):
        if holding(Path(path)):
            refused.append(Path(path))
            raise PermissionError(errno.EACCES, SHARING_VIOLATION, str(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    return refused
