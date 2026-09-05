"""Unit tests for the gateway single-instance lock helper.

Covers acquire / already-running / read_status liveness probe / corrupt payload
/ Windows lock-less degrade. The lock is an advisory ``fcntl.flock`` keyed to
``get_data_dir()/gateway.lock``; two separate ``open()`` calls on the same file
contend even within one process, so a second acquire raises while the first
handle is held.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.cli import _gateway_lock
from raven.cli._gateway_lock import (
    GatewayAlreadyRunningError,
    acquire,
    read_status,
)
from raven.config.loader import set_config_path


@pytest.fixture
def tmp_instance(tmp_path: Path):
    """Point get_data_dir() at a tmp instance and restore global config state."""
    set_config_path(tmp_path / "config.json")
    try:
        yield tmp_path
    finally:
        set_config_path(None)  # type: ignore[arg-type]


def test_acquire_writes_payload(tmp_instance: Path) -> None:
    old_umask = os.umask(0o022)
    fd = acquire(now=123.0)
    try:
        lock_file = tmp_instance / "gateway.lock"
        assert lock_file.exists()
        info = _gateway_lock._read_payload(lock_file)
        assert info.pid == os.getpid()
        assert info.started_at == 123.0
        # The same file later receives the web token, so it must be born
        # owner-only instead of inheriting the umask.
        assert (lock_file.stat().st_mode & 0o777) == 0o600
    finally:
        os.umask(old_umask)
        fd.close()


def test_second_acquire_raises_already_running(tmp_instance: Path) -> None:
    held = acquire(now=456.0)  # keep handle alive → lock stays held
    try:
        with pytest.raises(GatewayAlreadyRunningError) as exc:
            acquire(now=789.0)
        assert exc.value.info.pid == os.getpid()
        assert exc.value.info.started_at == 456.0
    finally:
        held.close()


def test_acquire_succeeds_after_previous_released(tmp_instance: Path) -> None:
    first = acquire(now=1.0)
    first.close()  # release
    second = acquire(now=2.0)
    second.close()


def test_read_status_none_when_no_lock_file(tmp_instance: Path) -> None:
    assert read_status(now=0.0) is None


def test_read_status_reports_owner_while_held(tmp_instance: Path) -> None:
    held = acquire(now=111.0)
    try:
        info = read_status(now=0.0)
        assert info is not None
        assert info.pid == os.getpid()
        assert info.started_at == 111.0
    finally:
        held.close()


def test_read_status_none_after_release(tmp_instance: Path) -> None:
    held = acquire(now=1.0)
    held.close()
    assert read_status(now=0.0) is None


def test_read_payload_corrupt_returns_placeholder(tmp_instance: Path) -> None:
    lock_file = tmp_instance / "gateway.lock"
    lock_file.write_text("not-json{{{")
    info = _gateway_lock._read_payload(lock_file)
    assert info.pid == -1
    assert info.config_path == ""


def test_payload_readable_while_lock_held(tmp_instance: Path) -> None:
    """The lock anchor is a separate file from the payload, so a concurrent
    reader (doctor) can read the owner payload even while the lock is held —
    including on Windows, where the lock is mandatory and would otherwise
    block a read of the locked file."""
    held = acquire(now=222.0)
    try:
        info = _gateway_lock._read_payload(tmp_instance / "gateway.lock")
        assert info.pid == os.getpid()
        assert info.started_at == 222.0
    finally:
        held.close()


def _spy_payload_write_modes(monkeypatch, records: list[tuple[int, int]]) -> None:
    """Record (inode, mode) at the moment of every ``os.write``.

    A final ``stat`` cannot distinguish narrow-then-write from write-then-
    narrow, and the exposure is decided at the write. Filtering by inode
    afterwards keeps writes to unrelated descriptors out of the record.
    """
    real_write = os.write

    def spying_write(fd: int, data) -> int:
        st = os.fstat(fd)
        records.append((st.st_ino, st.st_mode & 0o777))
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", spying_write)


def test_the_published_endpoint_file_is_never_briefly_world_readable(tmp_path, monkeypatch) -> None:
    """The payload carries the web token: when publish itself creates the
    file, the token must already land in an owner-only file."""
    from raven.cli import _gateway_lock as lock

    target = tmp_path / "gateway.lock"
    monkeypatch.setattr(lock, "_lock_path", lambda: target)
    records: list[tuple[int, int]] = []
    _spy_payload_write_modes(monkeypatch, records)

    old_umask = os.umask(0o022)
    try:
        lock.publish_web_endpoint("127.0.0.1", 8765, "s3cret-token")
    finally:
        os.umask(old_umask)

    ino = target.stat().st_ino
    modes_at_write = [mode for i, mode in records if i == ino]
    assert modes_at_write and all(mode == 0o600 for mode in modes_at_write)
    assert (target.stat().st_mode & 0o777) == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))["web_token"] == "s3cret-token"


def test_an_existing_wide_open_payload_is_narrowed_before_the_token_lands(tmp_path, monkeypatch) -> None:
    """O_CREAT applies its mode only to a file it creates, so a payload that
    already exists as 0644 must be narrowed BEFORE the token is written --
    narrowing after leaves the token world-readable for the span in between."""
    from raven.cli import _gateway_lock as lock

    target = tmp_path / "gateway.lock"
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, 0o644)
    monkeypatch.setattr(lock, "_lock_path", lambda: target)
    records: list[tuple[int, int]] = []
    _spy_payload_write_modes(monkeypatch, records)

    old_umask = os.umask(0o022)
    try:
        lock.publish_web_endpoint("127.0.0.1", 8765, "s3cret-token")
    finally:
        os.umask(old_umask)

    ino = target.stat().st_ino
    modes_at_write = [mode for i, mode in records if i == ino]
    assert modes_at_write and all(mode == 0o600 for mode in modes_at_write)
    assert (target.stat().st_mode & 0o777) == 0o600


def test_first_start_never_exposes_the_token(tmp_instance: Path, monkeypatch) -> None:
    """The normal first boot: acquire() creates the payload, then the bound
    server publishes the token into it. Every write to that file, the token's
    included, must hit an owner-only inode."""
    records: list[tuple[int, int]] = []
    _spy_payload_write_modes(monkeypatch, records)

    old_umask = os.umask(0o022)
    held = acquire(now=1.0)
    try:
        _gateway_lock.publish_web_endpoint("127.0.0.1", 8765, "s3cret-token")
    finally:
        os.umask(old_umask)
        held.close()

    lock_file = tmp_instance / "gateway.lock"
    ino = lock_file.stat().st_ino
    modes_at_write = [mode for i, mode in records if i == ino]
    assert len(modes_at_write) >= 2  # acquire's payload write + the token write
    assert all(mode == 0o600 for mode in modes_at_write)
    assert (lock_file.stat().st_mode & 0o777) == 0o600
    assert json.loads(lock_file.read_text(encoding="utf-8"))["web_token"] == "s3cret-token"
