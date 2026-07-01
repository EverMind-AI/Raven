"""Windows shim for the POSIX ``fcntl`` module.

Scoped to satisfy third-party dependencies (notably the bundled ``everos``
memory backend) that ``import fcntl`` at module top and call ``fcntl.flock``.
Without this, ``import everos.*`` raises ``ModuleNotFoundError: fcntl`` on
Windows and the whole memory backend silently degrades to a no-op.

``flock`` is backed by ``msvcrt`` byte-range locking; any msvcrt quirk
degrades to a no-op so a locking edge case can never break the dependency's
import or runtime. raven's own code uses :mod:`raven.utils.portable_lock`
(portalocker) instead and does not depend on this shim.
"""

from __future__ import annotations

import os
import sys
import types

# fcntl.flock operation flags (values are internal to this shim; callers use
# these attributes, so only self-consistency matters).
LOCK_SH = 0x1
LOCK_EX = 0x2
LOCK_NB = 0x4
LOCK_UN = 0x8


def _fileno(fd: object) -> int | None:
    if isinstance(fd, int):
        return fd
    getter = getattr(fd, "fileno", None)
    if not callable(getter):
        return None
    try:
        return int(getter())
    except Exception:
        return None


def flock(fd: object, operation: int) -> None:
    """Best-effort ``fcntl.flock`` on Windows via ``msvcrt.locking``.

    Locks a single byte at offset 0 of the anchor file. Any error degrades to
    a no-op — the goal is to let POSIX-only deps import and run, not to
    guarantee cross-process exclusion under every edge case.
    """
    try:
        import msvcrt
    except ImportError:
        return
    fileno = _fileno(fd)
    if fileno is None:
        return
    try:
        saved = os.lseek(fileno, 0, os.SEEK_CUR)
    except OSError:
        saved = None
    try:
        os.lseek(fileno, 0, os.SEEK_SET)
        if operation & LOCK_UN:
            msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
        else:
            mode = msvcrt.LK_NBLCK if (operation & LOCK_NB) else msvcrt.LK_LOCK
            msvcrt.locking(fileno, mode, 1)
    except Exception:
        # Never let a locking quirk break the caller's import/runtime.
        pass
    finally:
        if saved is not None:
            try:
                os.lseek(fileno, saved, os.SEEK_SET)
            except OSError:
                pass


def lockf(fd: object, operation: int, length: int = 0, start: int = 0, whence: int = 0) -> None:
    """``fcntl.lockf`` API parity — delegates to :func:`flock`."""
    flock(fd, operation)


def install() -> None:
    """Register this module as ``fcntl`` in ``sys.modules``.

    Windows-only, idempotent, and never clobbers a real ``fcntl`` (if one
    somehow exists it is preferred). Call before importing a POSIX-only
    dependency that hard-imports ``fcntl``.
    """
    if sys.platform != "win32":
        return
    if "fcntl" in sys.modules:
        return
    try:
        import fcntl  # noqa: F401 — a real fcntl exists; prefer it
        return
    except ImportError:
        pass
    module = types.ModuleType("fcntl")
    for name in ("LOCK_SH", "LOCK_EX", "LOCK_NB", "LOCK_UN", "flock", "lockf"):
        setattr(module, name, globals()[name])
    sys.modules["fcntl"] = module


__all__ = ["install", "flock", "lockf", "LOCK_SH", "LOCK_EX", "LOCK_NB", "LOCK_UN"]
