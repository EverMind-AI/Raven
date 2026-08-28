"""Crash-safe file primitives: locked append, replace, and update.

The helpers serialize cross-process writers with an advisory lock on a
sidecar lock kept in a hidden ``.lock/`` subdir of the target's own parent
(auto-released on process death, so no stale-lock cleanup is needed). The
lock is cross-platform (``portalocker``: POSIX ``fcntl`` + Windows
``LockFileEx``), so concurrent writers are serialized on Windows too.
"""

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from raven.utils.portable_lock import file_lock

T = TypeVar("T")

_held = threading.local()


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    # Reentrant within a thread: a helper called from inside write_transaction
    # on the same path must not flock the sidecar a second time — a second
    # LOCK_EX on another fd of the same file blocks even within one process.
    # Tracked per-thread, so holders must not await while inside (an
    # interleaving task on this thread would be treated as the holder).
    held: set[str] = getattr(_held, "paths", None) or set()
    _held.paths = held
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ".lock" / (path.name + ".lock")
    key = str(lock_path)
    if key in held:
        yield
        return
    with file_lock(lock_path):
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)


@contextmanager
def write_transaction(path: Path) -> Iterator[None]:
    """Hold ``path``'s write lock across a wider read-modify-write.

    A read made inside the block is still current when the write lands —
    without this, two processes doing read-modify-write around the locked
    helpers can still swallow each other's update between their read and
    their write. Helpers re-entered on the same path from the same thread
    (``atomic_replace`` / ``locked_append``) skip re-acquiring. Do not await
    while holding.
    """
    with _locked(path):
        yield


def locked_append(path: Path, lines: list[str]) -> None:
    """Append ``lines`` (sans newline) to ``path`` as one contiguous block."""
    if not lines:
        return
    with _locked(path):
        with open(path, "a+b") as f:
            payload = "".join(line + "\n" for line in lines).encode("utf-8")
            # A crashed writer can leave a partial line without a trailing
            # newline; start on a fresh line so records never merge.
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    payload = b"\n" + payload
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())


def _replace_unlocked(path: Path, data: str, create_mode: int | None = None) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    preserve: int | None = None
    if create_mode is None:
        try:
            preserve = path.stat().st_mode & 0o7777
        except OSError:
            preserve = None
        f = open(tmp_path, "w", encoding="utf-8")
    else:
        # Secret-bearing files: the mode must hold from the first byte, so it
        # is applied by os.open at creation — narrowing with a chmod after the
        # write leaves the payload readable for the span in between. A crashed
        # writer's leftover tmp would keep its old inode mode through O_CREAT,
        # so it is removed first to restore creation semantics.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        f = os.fdopen(os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, create_mode), "w", encoding="utf-8")
    with f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    # os.replace swaps the inode, so a target that was chmod 600 (config files
    # carrying api keys) would silently come back with the tmp's umask default
    # after every save. Carry the target's own mode onto the replacement.
    if preserve is not None:
        os.chmod(tmp_path, preserve)
    os.replace(tmp_path, path)


def atomic_replace(path: Path, data: str, *, mode: int | None = None) -> None:
    """Replace ``path``'s content with ``data`` via temp file + os.replace.

    ``mode``: hold this exact mode from the temp file's creation onward and
    carry it onto ``path`` (for secret-bearing files like token state). The
    default preserves the target's existing mode, umask for a new file.
    """
    with _locked(path):
        _replace_unlocked(path, data, create_mode=mode)


def atomic_update(path: Path, update: Callable[[str | None], tuple[str | None, T]]) -> T:
    """Lock ``path`` across a read-modify-write transaction."""
    with _locked(path):
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        replacement, result = update(current)
        if replacement is not None:
            _replace_unlocked(path, replacement)
        return result
