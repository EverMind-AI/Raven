"""Unit tests for the gateway single-instance lock helper.

Covers acquire / already-running / read_status liveness probe / corrupt payload
/ Windows lock-less degrade. The lock is an advisory ``fcntl.flock`` keyed to
``get_data_dir()/gateway.lock``; two separate ``open()`` calls on the same file
contend even within one process, so a second acquire raises while the first
handle is held.
"""

from __future__ import annotations

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
    fd = acquire(now=123.0)
    try:
        lock_file = tmp_instance / "gateway.lock"
        assert lock_file.exists()
        info = _gateway_lock._read_payload(lock_file)
        assert info.pid == os.getpid()
        assert info.started_at == 123.0
    finally:
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


def test_acquire_degrades_without_fcntl(tmp_instance: Path, monkeypatch) -> None:
    """Windows path: fcntl is None → acquire never locks, never raises."""
    monkeypatch.setattr(_gateway_lock, "fcntl", None)
    first = acquire(now=1.0)
    second = acquire(now=2.0)  # no contention without a real lock
    first.close()
    second.close()
