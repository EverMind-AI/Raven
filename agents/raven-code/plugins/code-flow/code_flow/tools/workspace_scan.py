"""Deadline-bounded listings for Raven-Code's post-command diagnostics only."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from raven.agent.tools.snapshot import MAX_ENTRIES, SKIP_DIRS, Snapshot


def take(root: Path, *, deadline: float) -> Snapshot | None:
    """Discard incomplete walks; a deadline cannot interrupt an in-flight OS read."""
    try:
        start = root.resolve()
    except OSError:
        return None
    if not start.is_dir():
        return None
    entries: Snapshot = {}
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        if time.monotonic() >= deadline:
            return None
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            if time.monotonic() >= deadline:
                return None
            path = os.path.join(dirpath, name)
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            entries[path] = (info.st_size, info.st_mtime_ns)
            if len(entries) > MAX_ENTRIES:
                return None
    return entries
