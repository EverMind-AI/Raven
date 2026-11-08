"""Unit tests for the evolver trial-identity markers (runmark).

A trial is only considered finished when the runner that observed it records a
completion marker, and the marker carries a digest of the harness tree that
produced it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

import raven.utils.atomic_io as atomic_io
from evolver.runmark import MARK, VERSION, fingerprint, is_complete, mark_complete, read_mark


def _tree(tmp_path: Path, *, mixed_case: bool = False) -> Path:
    d = tmp_path / "harness"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.py").write_text("x = 1\n")
    if mixed_case:
        (d / "README.md").write_text("r\n")
        (d / "agent.py").write_text("a\n")
    return d


def test_fingerprint_none_is_base():
    assert fingerprint(None) == "base"


def test_fingerprint_absent_dir(tmp_path: Path):
    assert fingerprint(tmp_path / "absent") == "absent"


def test_fingerprint_stable_and_ignores_byproducts(tmp_path: Path):
    d = _tree(tmp_path)
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"junk")
    (d / ".DS_Store").write_text("noise")
    (d / f"{MARK}.tmp").write_text("leftover\n")
    h1 = fingerprint(d)
    # byproducts must not affect the digest
    (d / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"changed")
    (d / ".DS_Store").write_text("changed")
    (d / f"{MARK}.tmp").write_text("changed\n")
    assert fingerprint(d) == h1
    # a real source change does change it
    (d / "sub" / "a.py").write_text("x = 2\n")
    assert fingerprint(d) != h1


def test_fingerprint_ignores_git_metadata(tmp_path: Path):
    d = _tree(tmp_path)
    h1 = fingerprint(d)
    # a worktree's .git is a file pointing at a per-worktree path
    (d / ".git").write_text("gitdir: /some/worktree/path\n")
    assert fingerprint(d) == h1
    # a normal checkout's .git is a directory holding mutable metadata
    (d / ".git").unlink()
    (d / ".git").mkdir()
    (d / ".git" / "index").write_text("noise\n")
    assert fingerprint(d) == h1


def test_fingerprint_orders_names_platform_independently(tmp_path: Path):
    # Pinned digest for README.md, agent.py, sub/x.py hashed in POSIX-name
    # order. A case-folding sort (Windows Path ordering) puts agent.py first
    # and produces a different digest. Write bytes explicitly: Path.write_text
    # translates "\n" to "\r\n" on Windows, and fingerprint hashes raw bytes.
    d = tmp_path / "harness"
    d.mkdir()
    (d / "README.md").write_bytes(b"r\n")
    (d / "agent.py").write_bytes(b"a\n")
    (d / "sub").mkdir()
    (d / "sub" / "x.py").write_bytes(b"x\n")
    assert fingerprint(d) == "1d0448a1475ad04c"


def test_mark_complete_writes_marker(tmp_path: Path):
    where = tmp_path / "trial"
    path = mark_complete(where, run_id="run-1", extra={"foo": "bar"})
    assert path == where / MARK
    data = read_mark(where)
    assert data is not None
    assert data["run_id"] == "run-1"
    assert data["version"] == VERSION
    assert data["foo"] == "bar"
    assert data["harness"] == "base"


def test_is_complete_requires_matching_run_id(tmp_path: Path):
    where = tmp_path / "trial"
    mark_complete(where, run_id="run-1")
    assert is_complete(where, run_id="run-1") is True
    assert is_complete(where, run_id="run-2") is False
    assert is_complete(where) is True


def test_is_complete_requires_expected_metadata(tmp_path: Path):
    where = tmp_path / "trial"
    mark_complete(where, run_id="run-1", extra={"split": "train", "k": 3, "n_tasks": 2})
    assert is_complete(where, run_id="run-1", expected={"k": 3, "n_tasks": 2}) is True
    assert is_complete(where, run_id="run-1", expected={"k": 1}) is False
    assert is_complete(where, run_id="run-1", expected={"split": "test"}) is False
    assert is_complete(where, run_id="run-1", expected={"n_tasks": 3}) is False


def test_is_complete_none_harness_is_base_not_wildcard(tmp_path: Path):
    where = tmp_path / "trial"
    harness = _tree(tmp_path)
    mark_complete(where, run_id="run-1", harness_dir=harness)
    # a cwd-less check is for the inherited harness, not any harness
    assert is_complete(where, run_id="run-1") is False
    assert is_complete(where, run_id="run-1", harness_dir=harness) is True


def test_is_complete_rejects_stale_harness(tmp_path: Path):
    where = tmp_path / "trial"
    harness = _tree(tmp_path)
    mark_complete(where, run_id="run-1", harness_dir=harness)
    (harness / "sub" / "a.py").write_text("x = 2\n")
    assert is_complete(where, run_id="run-1", harness_dir=harness) is False


def test_read_mark_invalid(tmp_path: Path):
    where = tmp_path / "trial"
    where.mkdir()
    (where / MARK).write_text("not json")
    assert read_mark(where) is None
    assert is_complete(where) is False


def test_read_mark_invalid_utf8(tmp_path: Path):
    where = tmp_path / "trial"
    where.mkdir()
    (where / MARK).write_bytes(b"\xff\xfe")
    assert read_mark(where) is None
    assert is_complete(where) is False


def test_read_mark_does_not_collide_with_a_concurrent_write(tmp_path: Path):
    """read_mark takes the write sidecar lock, so a reader cannot hold the
    marker open while mark_complete replaces it (a Windows sharing violation)."""
    import threading

    where = tmp_path / "trial"
    mark_complete(where, run_id="run-1")
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for _ in range(50):
                mark_complete(where, run_id="run-1")
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(50):
                assert is_complete(where, run_id="run-1") is True
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_extra_cannot_override_reserved_fields(tmp_path: Path):
    where = tmp_path / "trial"
    mark_complete(
        where,
        run_id="expected",
        extra={"run_id": "other", "version": 999, "at": 0, "harness": "fake"},
    )
    data = read_mark(where)
    assert data is not None
    assert data["run_id"] == "expected"
    assert data["version"] == VERSION
    assert data["harness"] == "base"
    assert data["at"] != 0
    assert is_complete(where, run_id="expected") is True
    assert is_complete(where, run_id="other") is False


@pytest.mark.parametrize("ancestor", ["__pycache__", ".git", ".lock"])
def test_fingerprint_reads_a_harness_below_a_byproduct_named_ancestor(tmp_path: Path, ancestor: str):
    """rglob yields absolute paths; judging their parts would let an ancestor
    named like ignored content hide every file and collapse the digest."""
    root = tmp_path / ancestor / "harness"
    root.mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n")
    other = tmp_path / ancestor / "other"
    other.mkdir(parents=True)
    (other / "a.py").write_text("x = 2\n")

    assert fingerprint(root) != "e3b0c44298fc1c14"
    assert fingerprint(root) != fingerprint(other)


def test_absent_harness_paths_do_not_share_an_identity(tmp_path: Path):
    absent_a = tmp_path / "gone-a"
    absent_b = tmp_path / "gone-b"
    where = tmp_path / "trial"
    mark_complete(where, run_id="run-1", harness_dir=absent_a)

    assert is_complete(where, run_id="run-1", harness_dir=absent_a) is False
    assert is_complete(where, run_id="run-1", harness_dir=absent_b) is False

    real = _tree(tmp_path)
    where_real = tmp_path / "trial-real"
    mark_complete(where_real, run_id="run-1", harness_dir=real)
    assert is_complete(where_real, run_id="run-1", harness_dir=absent_a) is False


def test_expected_missing_key_does_not_match_a_none_expectation(tmp_path: Path):
    where = tmp_path / "trial"
    mark_complete(where, run_id="run-1", extra={"k": 3})

    assert is_complete(where, run_id="run-1", expected={"absent_key": None}) is False


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_is_complete_rejects_a_non_integer_version(tmp_path: Path, version: object):
    where = tmp_path / "trial"
    where.mkdir()
    (where / MARK).write_text(json.dumps({"version": version, "run_id": "run-1", "harness": "base"}))

    assert is_complete(where, run_id="run-1") is False


def test_a_reader_holds_the_marker_lock_until_it_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    where = tmp_path / "trial"
    mark_complete(where, run_id="run-1")
    reading = threading.Event()
    release = threading.Event()
    replace_attempted = threading.Event()
    writer_done = threading.Event()
    real_read_text = Path.read_text
    real_replace = os.replace

    def slow_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == MARK:
            reading.set()
            assert release.wait(5)
        return real_read_text(self, *args, **kwargs)

    def tracking_replace(src: object, dst: object) -> None:
        replace_attempted.set()
        real_replace(src, dst)

    def writer() -> None:
        mark_complete(where, run_id="run-1")
        writer_done.set()

    monkeypatch.setattr(Path, "read_text", slow_read_text)
    monkeypatch.setattr(os, "replace", tracking_replace)

    reader_thread = threading.Thread(target=read_mark, args=(where,), daemon=True)
    reader_thread.start()
    assert reading.wait(5)
    writer_thread = threading.Thread(target=writer, daemon=True)
    writer_thread.start()
    try:
        blocked = not replace_attempted.wait(0.5)
    finally:
        release.set()
        reader_thread.join(5)
        writer_thread.join(5)

    assert blocked, "the write replaced the marker while the reader held the lock"
    assert writer_done.is_set()


def test_a_locked_reader_keeps_a_simulated_windows_replace_from_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """On Windows os.replace fails while another handle has the marker open.

    Simulated here on every platform: the writer's replace raises
    PermissionError whenever the reader still holds the file open, so only the
    read-side lock keeps the two from overlapping.
    """
    where = tmp_path / "trial"
    mark_complete(where, run_id="run-1")
    handle_open = threading.Event()
    finish_read = threading.Event()
    reader_started = threading.Event()
    writer_started = threading.Event()
    refused = threading.Event()
    errors: list[BaseException] = []
    real_read_text = Path.read_text
    real_replace = os.replace

    def held_open_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == MARK:
            with open(self, "rb"):
                handle_open.set()
                assert finish_read.wait(5)
            handle_open.clear()
        return real_read_text(self, *args, **kwargs)

    def windows_like_replace(src: object, dst: object) -> None:
        if Path(dst).name == MARK and handle_open.is_set():
            refused.set()
            raise PermissionError(32, "The process cannot access the file")
        real_replace(src, dst)

    def reader() -> None:
        reader_started.set()
        read_mark(where)

    def writer() -> None:
        writer_started.set()
        try:
            mark_complete(where, run_id="run-1")
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    monkeypatch.setattr(Path, "read_text", held_open_read_text)
    monkeypatch.setattr(os, "replace", windows_like_replace)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    assert reader_started.wait(5)
    assert handle_open.wait(5)
    writer_thread = threading.Thread(target=writer, daemon=True)
    writer_thread.start()
    assert writer_started.wait(5)
    time.sleep(0.4)
    refused_while_held = refused.is_set()
    finish_read.set()
    reader_thread.join(5)
    writer_thread.join(5)

    assert refused_while_held is False, "a replace landed while the reader held the file open"
    assert errors == []
    assert is_complete(where, run_id="run-1") is True


def test_a_marker_is_still_readable_when_the_lock_cannot_be_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    where = tmp_path / "trial"
    mark_complete(where, run_id="run-1")

    def unavailable(lock_path: Path, **kwargs: object) -> None:
        raise PermissionError(13, "read-only file system", str(lock_path))

    monkeypatch.setattr("raven.utils.atomic_io.file_lock", unavailable)

    data = read_mark(where)
    assert data is not None
    assert data["run_id"] == "run-1"
    assert is_complete(where, run_id="run-1") is True


def test_mark_complete_retries_a_transient_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    where = tmp_path / "trial"
    calls: list[object] = []
    real_replace = os.replace

    def flaky_replace(src: object, dst: object) -> None:
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError(32, "sharing violation")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    mark_complete(where, run_id="run-1")

    assert len(calls) == 2
    assert is_complete(where, run_id="run-1") is True


def test_mark_complete_retries_a_permission_error_from_the_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    where = tmp_path / "trial"
    real_create_like = atomic_io._create_like
    attempts: list[object] = []

    def refusing_create_like(tmp_path: Path, path: Path, preserve: int | None) -> int:
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError(13, "denied at open")
        return real_create_like(tmp_path, path, preserve)

    monkeypatch.setattr(atomic_io, "_create_like", refusing_create_like)
    mark_complete(where, run_id="run-1")

    assert len(attempts) == 2
    assert is_complete(where, run_id="run-1") is True


def test_mark_complete_removes_the_tmp_after_the_retries_run_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    where = tmp_path / "trial"

    def always_refused(src: object, dst: object) -> None:
        raise PermissionError(32, "sharing violation")

    monkeypatch.setattr(os, "replace", always_refused)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    with pytest.raises(PermissionError):
        mark_complete(where, run_id="run-1")

    assert not (where / (MARK + ".tmp")).exists()
