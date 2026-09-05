"""Cross-platform advisory file lock.

Serializes cross-process (and cross-thread) writers to a shared file by
taking an exclusive lock on a sibling ``.lock`` anchor via ``portalocker``
(POSIX ``fcntl.flock`` + Windows ``LockFileEx`` under the hood).

Replaces the previous ``fcntl``-only lock paths that silently degraded to
*unlocked* on Windows (``import fcntl`` → ``ImportError`` / ``sys.platform ==
"win32"`` no-op branches), which lost concurrent writes on Windows.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import portalocker


class LockTimeoutError(RuntimeError):
    """Raised for a non-blocking acquire when another holder has the lock."""


@contextmanager
def file_lock(lock_path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``lock_path`` for the block's body.

    The anchor file is created on first use. With ``blocking=False`` a
    :class:`LockTimeoutError` is raised immediately if another process holds it.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = portalocker.LOCK_EX
    if not blocking:
        flags |= portalocker.LOCK_NB

    fh = open(lock_path, "a+")
    try:
        try:
            portalocker.lock(fh, flags)
        except portalocker.exceptions.LockException as exc:
            raise LockTimeoutError(f"lock already held: {lock_path}") from exc
        try:
            yield
        finally:
            try:
                portalocker.unlock(fh)
            except Exception:
                pass
    finally:
        fh.close()


__all__ = ["file_lock", "LockTimeoutError"]
