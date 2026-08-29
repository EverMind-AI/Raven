"""Unit tests for MemoryStore.locked() + _safe_write_long_term -- the
cross-process user.md lock, and the one compare-and-set write built on it,
shared by Personalizer + MemoryConsolidator + SentinelMemoryWriter.
"""

from __future__ import annotations

import multiprocessing
import sys
import time
from multiprocessing import Value
from pathlib import Path

import pytest

from raven.memory_engine.consolidate.consolidator import MemoryStore


def test_locked_yields_without_throwing(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.write_long_term("hello\n")
    with store.locked():
        assert store.read_long_term() == "hello\n"


def test_lock_path_is_sibling(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    # Lock file is always a sibling of memory_file with a ``.lock`` suffix —
    # derives from whatever path memory_file points at (L4: user.md).
    assert store.memory_lock_path.name == store.memory_file.name + ".lock"
    assert store.memory_lock_path.parent == store.memory_file.parent


def test_safe_write_returns_true_when_unchanged(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.write_long_term("v1\n")
    ok = store._safe_write_long_term("v1\n", lambda _current: "v2\n")
    assert ok is True
    assert store.read_long_term() == "v2\n"


def test_safe_write_builds_from_what_is_on_disk(tmp_path: Path) -> None:
    """The build runs under the lock, over the file's own text -- so a caller
    that appends gets the current content, not the copy it read earlier."""
    store = MemoryStore(tmp_path)
    store.write_long_term("v1\n")
    seen: list[str] = []

    def _build(current: str) -> str:
        seen.append(current)
        return current + "appended\n"

    assert store._safe_write_long_term("v1\n", _build) is True
    assert seen == ["v1\n"]
    assert store.read_long_term() == "v1\nappended\n"


def test_safe_write_leaves_the_file_alone_when_the_build_changes_nothing(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.write_long_term("v1\n")
    before = store.memory_file.stat().st_mtime_ns
    assert store._safe_write_long_term("v1\n", lambda current: current) is True
    assert store.memory_file.stat().st_mtime_ns == before


def test_safe_write_skips_when_concurrent_modification(tmp_path: Path) -> None:
    """If another writer changed user.md between our read and write, the
    cas-style _safe_write_long_term refuses to clobber and returns False."""
    store = MemoryStore(tmp_path)
    store.write_long_term("original\n")
    # Simulate another writer: directly mutate the file before we attempt write.
    store.memory_file.write_text("changed-by-other\n", encoding="utf-8")
    ok = store._safe_write_long_term("original\n", lambda _current: "our-update\n")
    assert ok is False
    # The other writer's content is preserved; ours is dropped.
    assert store.read_long_term() == "changed-by-other\n"


# ---------------------------------------------------------------------------
# Multi-process serialization (POSIX-only)


def _worker_acquire_and_hold(lock_dir: str, hold_secs: float, ready: Value, done: Value) -> None:
    """Helper run in subprocess: grab the same file lock and hold it."""
    store = MemoryStore(Path(lock_dir))
    with store.locked():
        ready.value = 1  # signal main: we have the lock
        time.sleep(hold_secs)
        done.value = 1


@pytest.mark.skipif(sys.platform == "win32", reason="spawn harness is run on POSIX CI only")
def test_lock_serializes_across_processes(tmp_path: Path) -> None:
    """Two processes locking the same MemoryStore must serialize:
    process B can only acquire after process A releases.
    """
    store = MemoryStore(tmp_path)
    store.write_long_term("init\n")

    # spawn, not the Linux default fork: pytest leaves the parent multi-threaded,
    # and forking from there segfaults the interpreter at exit.
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Value("i", 0)
    done = ctx.Value("i", 0)
    p = ctx.Process(
        target=_worker_acquire_and_hold,
        args=(str(tmp_path), 0.3, ready, done),
    )
    p.start()

    # Wait for the worker to hold the lock
    deadline = time.monotonic() + 5
    while ready.value == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.value == 1, "worker never acquired the lock"

    # While the worker holds the lock, our locked() acquire should block.
    t0 = time.monotonic()
    with store.locked():
        elapsed = time.monotonic() - t0
        # Worker held for 0.3s; we should have waited at least most of that.
        assert elapsed >= 0.15, (
            f"main process didn't wait for worker to release "
            f"(elapsed={elapsed:.3f}s); file lock not enforced cross-process"
        )

    p.join(timeout=5)
    assert done.value == 1
