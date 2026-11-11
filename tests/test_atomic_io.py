"""Tests for raven.utils.atomic_io."""

import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from raven.utils.atomic_io import atomic_replace, atomic_update, locked_append, write_transaction

WRITERS = 2
CALLS_PER_WRITER = 50
LINES_PER_CALL = 5


class _WindowsOS:
    """atomic_io's view of ``os`` on Windows: patching the real ``os.name``
    would also make pathlib build WindowsPath objects that cannot exist here."""

    name = "nt"

    def __getattr__(self, attribute: str):
        return getattr(os, attribute)


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


def test_explicit_mode_on_windows_copies_the_named_sources_dacl(tmp_path: Path, monkeypatch):
    """A Windows mode cannot express a DACL: when the mode came from another
    file, the temp must take that file's descriptor at creation, or its private
    text lands in a record only as protected as the folder."""
    from raven.utils import atomic_io

    source = tmp_path / "soul.local.md"
    source.write_text("operator text", encoding="utf-8")
    target = tmp_path / ".soul.md.addendum"
    copied: list[tuple[Path, Path]] = []

    def create_with_dacl(tmp: Path, like: Path) -> int:
        copied.append((tmp, like))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        return os.open(tmp, flags, 0o600)

    with monkeypatch.context() as patched:
        patched.setattr(atomic_io, "os", _WindowsOS())
        patched.setattr(atomic_io, "_create_with_dacl_of", create_with_dacl)
        atomic_io.atomic_replace(target, "private rule\n", mode=0o600, newline="", like=source)

    assert copied == [(tmp_path / ".soul.md.addendum.tmp", source)]
    assert target.read_text(encoding="utf-8") == "private rule\n"


def test_a_default_replace_on_windows_still_copies_the_targets_dacl(tmp_path: Path, monkeypatch):
    """The default path's Windows DACL copy stays: a writer that names no source
    keeps protecting the target with its own descriptor."""
    from raven.utils import atomic_io

    target = tmp_path / "agent.md"
    target.write_text("old", encoding="utf-8")
    copied: list[Path] = []

    def create_with_dacl(tmp: Path, like: Path) -> int:
        copied.append(like)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        return os.open(tmp, flags, 0o600)

    with monkeypatch.context() as patched:
        patched.setattr(atomic_io, "os", _WindowsOS())
        patched.setattr(atomic_io, "_create_with_dacl_of", create_with_dacl)
        atomic_io.atomic_replace(target, "new")

    assert copied == [target]
    assert target.read_text(encoding="utf-8") == "new"


def test_explicit_mode_without_a_named_source_keeps_the_plain_creation(tmp_path: Path, monkeypatch):
    """The other secret writers pass only a mode: they have no source file to
    copy a DACL from, and a target that does not exist yet has none either."""
    from raven.utils import atomic_io

    target = tmp_path / "serve.json"
    copied: list[Path] = []

    def create_with_dacl(tmp: Path, like: Path) -> int:
        copied.append(like)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        return os.open(tmp, flags, 0o600)

    with monkeypatch.context() as patched:
        patched.setattr(atomic_io, "os", _WindowsOS())
        patched.setattr(atomic_io, "_create_with_dacl_of", create_with_dacl)
        atomic_io.atomic_replace(target, '{"token": "s3cret"}', mode=0o600)

    assert copied == []
    assert target.read_text(encoding="utf-8") == '{"token": "s3cret"}'


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not available on Windows")
def test_the_tmp_is_never_more_open_than_the_target_while_it_holds_the_new_bytes(tmp_path: Path, monkeypatch):
    """A chmod after the write left a private target's next text in a
    umask-default tmp for the span in between."""
    path = tmp_path / "agent.md"
    path.write_text("private", encoding="utf-8")
    os.chmod(path, 0o600)
    seen: list[int] = []
    real_fsync = os.fsync

    def watched(fd: int) -> None:
        seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", watched)
    previous = os.umask(0o022)
    try:
        atomic_replace(path, "still private")
    finally:
        os.umask(previous)
    assert seen == [0o600]
    assert path.read_text(encoding="utf-8") == "still private"


def test_the_windows_bindings_are_declared_once_not_per_save(tmp_path: Path, monkeypatch):
    """ctypes keeps every pointer type it is asked for: a structure declared per
    call pinned about 6 KB more on every save, for the process's life."""
    import ctypes
    import sys
    import types

    from raven.utils import atomic_io

    class _Fn:
        def __init__(self, impl):
            self.impl = impl

        def __call__(self, *args):
            return self.impl(*args)

    def get_security(_path, _info, buffer, _size, needed):
        needed._obj.value = 16
        return buffer is not None

    def create(path, *_rest):
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    loaded: list[str] = []

    def win_dll(name, **_kwargs):
        loaded.append(name)
        return types.SimpleNamespace(
            GetFileSecurityW=_Fn(get_security), CreateFileW=_Fn(create), CloseHandle=_Fn(lambda _h: True)
        )

    monkeypatch.setattr(ctypes, "WinDLL", win_dll, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(open_osfhandle=lambda handle, _flags: handle))
    atomic_io._win32_security.cache_clear()
    try:
        target = tmp_path / "agent.md"
        target.write_text("old", encoding="utf-8")
        for attempt in range(2):
            tmp = tmp_path / f"agent.md.{attempt}.tmp"
            os.close(atomic_io._create_with_dacl_of(tmp, target))
            assert tmp.is_file()
        assert loaded == ["advapi32", "kernel32"]
    finally:
        atomic_io._win32_security.cache_clear()


def test_a_failed_replace_takes_its_tmp_with_it(tmp_path: Path, monkeypatch):
    """Windows refuses to replace a read-only target, by which point the tmp
    has taken the target's read-only mode; left behind, it refused the next
    attempt's open even after the target was made writable again."""
    path = tmp_path / "agent.md"
    path.write_text("old", encoding="utf-8")

    def refused(_src, dst):
        raise PermissionError(13, "Access is denied", str(dst))

    monkeypatch.setattr(os, "replace", refused)
    with pytest.raises(PermissionError):
        atomic_replace(path, "new")
    assert not (tmp_path / "agent.md.tmp").exists()

    monkeypatch.undo()
    atomic_replace(path, "new")
    assert path.read_text(encoding="utf-8") == "new"


def test_a_read_only_tmp_a_dead_writer_left_does_not_block_the_replace(tmp_path: Path):
    path = tmp_path / "agent.md"
    path.write_text("old", encoding="utf-8")
    stale_tmp = tmp_path / "agent.md.tmp"
    stale_tmp.write_text("dead writer residue", encoding="utf-8")
    os.chmod(stale_tmp, stat.S_IREAD)

    atomic_replace(path, "new")

    assert path.read_text(encoding="utf-8") == "new"
    assert not stale_tmp.exists()


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


class TestReplaceRetry:
    def test_retries_a_transient_sharing_violation(self, tmp_path: Path, monkeypatch):
        import raven.utils.atomic_io as atomic_io

        path = tmp_path / "state.json"
        path.write_text("old", encoding="utf-8")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("sharing violation")
            return real_replace(src, dst)

        monkeypatch.setattr(atomic_io.os, "replace", flaky)
        monkeypatch.setattr(atomic_io, "_REPLACE_BACKOFF", 0.001)

        atomic_replace(path, "new")
        assert path.read_text(encoding="utf-8") == "new"
        assert calls["n"] == 3

    def test_raises_after_the_retry_budget(self, tmp_path: Path, monkeypatch):
        import raven.utils.atomic_io as atomic_io

        path = tmp_path / "state.json"
        path.write_text("old", encoding="utf-8")
        calls = {"n": 0}

        def always_denied(src, dst):
            calls["n"] += 1
            raise PermissionError("sharing violation")

        monkeypatch.setattr(atomic_io.os, "replace", always_denied)
        monkeypatch.setattr(atomic_io, "_REPLACE_BACKOFF", 0.001)

        with pytest.raises(PermissionError):
            atomic_replace(path, "new")
        assert calls["n"] == atomic_io._REPLACE_ATTEMPTS
        assert path.read_text(encoding="utf-8") == "old"
        assert not (tmp_path / "state.json.tmp").exists()

    def test_does_not_retry_a_missing_source(self, tmp_path: Path, monkeypatch):
        import raven.utils.atomic_io as atomic_io

        path = tmp_path / "state.json"
        path.write_text("old", encoding="utf-8")
        calls = {"n": 0}

        def missing(src, dst):
            calls["n"] += 1
            raise FileNotFoundError(src)

        monkeypatch.setattr(atomic_io.os, "replace", missing)
        monkeypatch.setattr(atomic_io, "_REPLACE_BACKOFF", 0.001)

        with pytest.raises(FileNotFoundError):
            atomic_replace(path, "new")
        assert calls["n"] == 1
