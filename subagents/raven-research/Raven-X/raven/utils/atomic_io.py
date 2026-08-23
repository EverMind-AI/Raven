"""Crash-safe JSONL file primitives: locked append and atomic replace.

Both helpers serialize cross-process writers with an advisory lock on a
sidecar lock kept in a hidden ``.lock/`` subdir of the target's own parent
(auto-released on process death, so no stale-lock cleanup is needed). The
lock is cross-platform (``portalocker``: POSIX ``fcntl`` + Windows
``LockFileEx``), so concurrent writers are serialized on Windows too.
"""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from raven.utils.portable_lock import file_lock


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ".lock" / (path.name + ".lock")
    with file_lock(lock_path):
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


def atomic_replace(path: Path, data: str) -> None:
    """Replace ``path``'s content with ``data`` via temp file + os.replace."""
    with _locked(path):
        tmp_path = path.with_name(path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
