"""Crash-safe file primitives: locked append, replace, and update.

The helpers serialize cross-process writers with an advisory lock on a
sidecar lock kept in a hidden ``.lock/`` subdir of the target's own parent
(auto-released on process death, so no stale-lock cleanup is needed). The
lock is cross-platform (``portalocker``: POSIX ``fcntl`` + Windows
``LockFileEx``), so concurrent writers are serialized on Windows too.
"""

import os
import stat
import threading
from contextlib import contextmanager, suppress
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


def _discard(tmp_path: Path) -> None:
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        return
    except OSError:
        # Windows will not unlink a read-only file, and a tmp is read-only
        # once a read-only target's mode has been carried onto it.
        with suppress(OSError):
            os.chmod(tmp_path, stat.S_IREAD | stat.S_IWRITE)
            os.unlink(tmp_path)


_DACL_SECURITY_INFORMATION = 0x4
_GENERIC_WRITE = 0x40000000
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x80


def _create_with_dacl_of(tmp_path: Path, like: Path) -> int:  # pragma: no cover - WinDLL does not load off Windows
    """A write fd on a new ``tmp_path`` holding ``like``'s DACL from creation.

    Raises ``OSError`` when that DACL cannot be read: the tmp is then never
    made with a wider one.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetFileSecurityW.restype = wintypes.BOOL
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    source = str(like)
    needed = wintypes.DWORD(0)
    if (
        not advapi32.GetFileSecurityW(source, _DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(needed))
        and not needed.value
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetFileSecurityW(
        source, _DACL_SECURITY_INFORMATION, descriptor, needed.value, ctypes.byref(needed)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), ctypes.addressof(descriptor), False)
    handle = kernel32.CreateFileW(
        str(tmp_path), _GENERIC_WRITE, 0, ctypes.byref(attributes), _CREATE_NEW, _FILE_ATTRIBUTE_NORMAL, None
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _create_like(tmp_path: Path, path: Path, preserve: int | None) -> int:
    """A write fd on a new ``tmp_path`` no more open than ``path``, from its first byte.

    The new bytes may be as private as the old: a chmod after the write, or
    on Windows the folder's inherited ACL, which os.replace carries onto the
    target, would leave them readable by more accounts in between or after.
    """
    if os.name == "nt" and preserve is not None:
        return _create_with_dacl_of(tmp_path, path)  # pragma: no cover - Windows only
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    return os.open(tmp_path, flags, 0o666 if preserve is None else preserve & 0o777)


def _replace_unlocked(path: Path, data: str, create_mode: int | None = None, newline: str | None = None) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    # Callers hold path's lock, so a tmp already here is a dead writer's: it
    # would lend its old mode through O_CREAT, or refuse the open outright
    # when it was left read-only.
    _discard(tmp_path)
    preserve: int | None = None
    try:
        if create_mode is None:
            try:
                preserve = path.stat().st_mode & 0o7777
            except OSError:
                preserve = None
            f = os.fdopen(_create_like(tmp_path, path, preserve), "w", encoding="utf-8", newline=newline)
        else:
            # Secret-bearing files: the mode must hold from the first byte, so it
            # is applied by os.open at creation — narrowing with a chmod after the
            # write leaves the payload readable for the span in between.
            f = os.fdopen(
                os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, create_mode),
                "w",
                encoding="utf-8",
                newline=newline,
            )
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
    except BaseException:
        # Windows refuses to replace a read-only target after the tmp has
        # taken its mode; the tmp goes too, so making the target writable is
        # all a retry needs.
        _discard(tmp_path)
        raise


def remove_with_lock(path: Path) -> None:
    """Remove ``path`` and its lock sidecar; both absent is fine.

    The sidecar's location is this module's own fact -- a caller deleting a
    locked file must not re-derive it by hand, or the two drift and deleted
    files leave lock litter behind.
    """
    path.unlink(missing_ok=True)
    (path.parent / ".lock" / (path.name + ".lock")).unlink(missing_ok=True)


def atomic_replace(path: Path, data: str, *, mode: int | None = None, newline: str | None = None) -> None:
    """Replace ``path``'s content with ``data`` via temp file + os.replace.

    ``mode``: hold this exact mode from the temp file's creation onward and
    carry it onto ``path`` (for secret-bearing files like token state). The
    default preserves the target's existing mode -- and on Windows its DACL --
    from the temp file's creation, umask for a new file.
    ``newline`` is :func:`open`'s: ``""`` writes ``data`` byte for byte, with
    no translation of line endings to the platform's.
    """
    with _locked(path):
        _replace_unlocked(path, data, create_mode=mode, newline=newline)


def atomic_update(
    path: Path,
    update: Callable[[str | None], tuple[str | None, T]],
    *,
    mode: int | None = None,
) -> T:
    """Lock ``path`` across a read-modify-write transaction.

    ``mode`` carries the same meaning as in ``atomic_replace``: hold that exact
    mode from the temp file's creation onward, for a transaction that writes
    secret material. Without it a brand-new file lands at the process umask,
    which a chmod afterwards can only narrow once the bytes are already there.
    """
    with _locked(path):
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        replacement, result = update(current)
        if replacement is not None:
            _replace_unlocked(path, replacement, create_mode=mode)
        return result
