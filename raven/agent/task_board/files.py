"""Reading, writing and removing the task board's own files the way Windows allows.

Windows will not replace or remove a file another handle has open: ``os.replace``
raises ``PermissionError`` (WinError 5) and ``unlink`` WinError 32, and opening a
file that is being replaced is refused too. The Task Board page reads every
conversation's board on a worker thread, so a write can meet a read in flight.
Such a hold lasts one read, so each call is tried a few times, a short pause
apart, before the error reaches its caller -- a pause the calling thread, the
event loop included, waits out only while a reader holds the file. A write that
fails leaves no temporary file behind, and no two writes share one.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

ATTEMPTS = 5
PAUSE_S = 0.05


def _held_off(call: Callable[[], T]) -> T:
    for _ in range(ATTEMPTS - 1):
        try:
            return call()
        except PermissionError:
            time.sleep(PAUSE_S)
    return call()


def read_text(path: Path) -> str:
    return _held_off(lambda: path.read_text(encoding="utf-8"))


def write_text(path: Path, text: str) -> None:
    """Write the whole file beside ``path``, then move it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        _held_off(lambda: os.replace(tmp, path))
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def remove(path: Path) -> None:
    _held_off(lambda: path.unlink(missing_ok=True))
