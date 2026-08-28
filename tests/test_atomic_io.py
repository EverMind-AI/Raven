"""Tests for raven.utils.atomic_io."""

import multiprocessing
import os
from pathlib import Path

from raven.utils.atomic_io import atomic_replace, atomic_update, locked_append, write_transaction

WRITERS = 2
CALLS_PER_WRITER = 50
LINES_PER_CALL = 5


def _append_worker(path_str: str, writer_id: int) -> None:
    for call_idx in range(CALLS_PER_WRITER):
        block = [f"{writer_id}:{call_idx}:{line_idx}" for line_idx in range(LINES_PER_CALL)]
        locked_append(Path(path_str), block)


def test_locked_append_appends_lines(tmp_path: Path):
    """Sequential calls accumulate lines in order."""
    path = tmp_path / "s.jsonl"
    locked_append(path, ["a", "b"])
    locked_append(path, ["c"])
    assert path.read_text(encoding="utf-8") == "a\nb\nc\n"


def test_lock_lives_in_hidden_lock_subdir(tmp_path: Path):
    """The advisory lock sidecar lives in a hidden ``.lock/`` dir derived from
    the target's own parent — never beside the target file."""
    path = tmp_path / "s.jsonl"
    locked_append(path, ["a"])
    beside = [p.name for p in tmp_path.iterdir() if p.is_file() and p.name.endswith(".lock")]
    assert beside == []
    assert (tmp_path / ".lock" / "s.jsonl.lock").exists()


def test_locked_append_concurrent_writers_lose_nothing(tmp_path: Path):
    """Two processes appending concurrently: every line lands, and the
    lines of one locked_append call stay contiguous (turn-block invariant)."""
    path = tmp_path / "s.jsonl"
    # spawn, not the Linux default fork: pytest leaves the parent multi-threaded,
    # and forking from there segfaults the interpreter at exit.
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_append_worker, args=(str(path), w)) for w in range(WRITERS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == WRITERS * CALLS_PER_WRITER * LINES_PER_CALL
    assert len(set(lines)) == len(lines)

    block_positions: dict[tuple[str, str], list[int]] = {}
    for pos, line in enumerate(lines):
        writer_id, call_idx, _ = line.split(":")
        block_positions.setdefault((writer_id, call_idx), []).append(pos)
    for positions in block_positions.values():
        assert positions == list(range(positions[0], positions[0] + LINES_PER_CALL))


def test_locked_append_repairs_missing_trailing_newline(tmp_path: Path):
    """Appending after a crashed partial line starts on a fresh line, so
    the new record is not merged into the partial one."""
    path = tmp_path / "s.jsonl"
    path.write_text('{"partial": "tru', encoding="utf-8")
    locked_append(path, ["next"])
    assert path.read_text(encoding="utf-8") == '{"partial": "tru\nnext\n'


def test_atomic_replace_swaps_content(tmp_path: Path):
    """atomic_replace replaces the whole file and leaves no temp residue."""
    path = tmp_path / "s.jsonl"
    path.write_text("old\n", encoding="utf-8")
    atomic_replace(path, "new1\nnew2\n")
    assert path.read_text(encoding="utf-8") == "new1\nnew2\n"
    residue = [p.name for p in tmp_path.iterdir() if p.name not in ("s.jsonl", ".lock")]
    assert residue == []


def test_atomic_replace_creates_missing_file(tmp_path: Path):
    path = tmp_path / "fresh.jsonl"
    atomic_replace(path, "data\n")
    assert path.read_text(encoding="utf-8") == "data\n"


def test_helpers_work_cross_platform(tmp_path: Path):
    """Both helpers work on every platform. Locking is cross-platform via
    portalocker (POSIX fcntl + Windows LockFileEx) — there is no longer an
    fcntl-absent 'degrade to unlocked' path."""
    path = tmp_path / "s.jsonl"
    locked_append(path, ["x"])
    atomic_replace(path, "y\n")
    assert path.read_text(encoding="utf-8") == "y\n"


def test_atomic_replace_preserves_the_targets_mode(tmp_path: Path):
    """os.replace swaps the inode: a config chmod 600 (api keys) must not come
    back world-readable after a save — the leak _persist_migrations documented
    and five sibling _write_atomic copies never fixed."""
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o600)

    atomic_replace(path, '{"k": 1}')

    assert (path.stat().st_mode & 0o777) == 0o600


def test_atomic_update_concurrent_increments_lose_nothing(tmp_path: Path):
    """The locked RMW transaction is what the unlocked tmp+replace sites lack:
    two writers read-modify-write concurrently and neither update is swallowed."""
    import threading

    path = tmp_path / "counter.txt"

    def bump(_: int) -> None:
        def update(current: str | None) -> tuple[str, None]:
            value = int(current or "0")
            return str(value + 1), None

        atomic_update(path, update)

    threads = [threading.Thread(target=bump, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert path.read_text(encoding="utf-8") == "16"


def test_atomic_replace_with_explicit_mode_holds_from_creation(tmp_path: Path):
    """Secret-bearing files (serve.json's token) need their mode from the very
    first byte; an explicit mode wins over both umask and a leftover tmp's
    stale wide mode."""
    path = tmp_path / "serve.json"
    stale_tmp = tmp_path / "serve.json.tmp"
    stale_tmp.write_text("crashed writer residue", encoding="utf-8")
    os.chmod(stale_tmp, 0o644)

    atomic_replace(path, '{"token": "s3cret"}', mode=0o600)

    assert (path.stat().st_mode & 0o777) == 0o600
    assert path.read_text(encoding="utf-8") == '{"token": "s3cret"}'


def test_write_transaction_is_reentrant_for_the_helpers(tmp_path: Path):
    """atomic_replace inside write_transaction on the same path must not
    deadlock on the sidecar (a second flock in one process blocks)."""
    path = tmp_path / "reg.json"
    path.write_text("old", encoding="utf-8")

    with write_transaction(path):
        current = path.read_text(encoding="utf-8")
        atomic_replace(path, current + "+new")

    assert path.read_text(encoding="utf-8") == "old+new"
    # The lock is genuinely released afterwards: a plain helper call proceeds.
    atomic_replace(path, "after")
    assert path.read_text(encoding="utf-8") == "after"


def _probe_lock(lock_path_str: str, q) -> None:
    from raven.utils.portable_lock import LockTimeoutError, file_lock

    try:
        with file_lock(Path(lock_path_str), blocking=False):
            q.put("acquired")
    except LockTimeoutError:
        q.put("blocked")


def test_write_transaction_excludes_other_processes(tmp_path: Path):
    """Deterministic mutual exclusion: while a transaction is held, another
    process's non-blocking acquire is refused — the lost-update window between
    a registry's read and its flush is closed, not narrowed."""
    path = tmp_path / "reg.json"
    lock_path = path.parent / ".lock" / (path.name + ".lock")

    with write_transaction(path):
        q = multiprocessing.Queue()
        proc = multiprocessing.Process(target=_probe_lock, args=(str(lock_path), q))
        proc.start()
        verdict = q.get(timeout=10)
        proc.join(10)
    assert verdict == "blocked"

    q2 = multiprocessing.Queue()
    proc2 = multiprocessing.Process(target=_probe_lock, args=(str(lock_path), q2))
    proc2.start()
    assert q2.get(timeout=10) == "acquired"
    proc2.join(10)
