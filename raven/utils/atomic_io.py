"""Crash-safe JSONL file primitives: locked append and atomic replace.

Both helpers serialize cross-process writers with an advisory
``fcntl.flock`` on a sidecar lock kept in a hidden ``.lock/`` subdir of the
target's own parent (auto-released on process death, so no stale-lock
cleanup is needed). On platforms without ``fcntl`` they degrade to unlocked
operation.
"""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:
    fcntl = None


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield
        return
    lock_dir = path.parent / ".lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / (path.name + ".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


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
