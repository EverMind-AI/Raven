"""Cross-process persistence primitives for Sentinel components.

Each running Raven process (REPL + gateway) maintains its own in-memory
copy of NudgePolicy quotas, NudgeInjector queues, and DeferManager pending
heaps. Without coordination, two concurrent processes can double-nudge the
user and violate the hour/day quotas.

This module provides a single JSON file backing them, guarded by a
POSIX advisory lock (fcntl) so only one process at a time can
read-mutate-write. Writes are atomic via temp-file rename.

Not Windows-compatible. Raven's channel layer is POSIX-only anyway.
"""

from __future__ import annotations

import json
import os
import sys

try:
    import fcntl
except ImportError:
    fcntl = None
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from loguru import logger


class JsonStateStore:
    """File-backed JSON state with POSIX advisory locking.

    load() reads the file without a lock (atomic rename writes mean readers
    always see a complete old-or-new file). update() takes an exclusive
    lock across read-mutate-write.

    The lock is a separate sibling file (``<path>.lock``) so it survives
    atomic rename of the data file. Lock is advisory — all cooperating
    processes must use this class to coordinate.
    """

    def __init__(self, path: Path) -> None:
        if sys.platform == "win32":
            raise NotImplementedError("JsonStateStore requires POSIX fcntl")
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("sentinel state load failed at {}: {}", self.path, exc)
            return {}

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold an exclusive fcntl lock. Safe to nest via recursive callers."""
        with self.lock_path.open("a") as lock_fd:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)

    def update(
        self, fn: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        """Atomic read-modify-write. Callback receives current state, returns
        new state. Returns the newly-written state."""
        with self.locked():
            current = self.load()
            new_state = fn(current)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(new_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            return new_state

    def clear(self) -> None:
        """Remove the state file (primarily for tests)."""
        with self.locked():
            if self.path.exists():
                self.path.unlink()


__all__ = ["JsonStateStore"]
