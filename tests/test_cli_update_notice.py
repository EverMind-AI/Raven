"""Tests for the TUI startup update nudge (raven/cli/update_notice.py)."""

from __future__ import annotations

import json
import time

import pytest

from raven.cli import update_notice as un


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / "update_check.json"
    monkeypatch.setattr(un, "_CACHE_PATH", path)
    return path


def _write(path, latest, *, checked_at=None):
    payload = {"latest_version": latest, "checked_at": time.time() if checked_at is None else checked_at}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_no_cache_yields_no_notice(cache):
    assert un.update_notice("0.1.9") is None


def test_newer_cached_release_yields_notice(cache):
    _write(cache, "0.2.0")
    assert un.update_notice("0.1.9") == (1, "raven upgrade")


def test_same_version_yields_no_notice(cache):
    _write(cache, "0.1.9")
    assert un.update_notice("0.1.9") is None


def test_older_cached_release_yields_no_notice(cache):
    # A local dev build ahead of the latest release must not nag.
    _write(cache, "0.1.9")
    assert un.update_notice("0.2.0") is None


def test_unparseable_versions_are_ignored(cache):
    _write(cache, "not-a-version")
    assert un.update_notice("0.1.9") is None
    _write(cache, "0.2.0")
    assert un.update_notice("garbage") is None


def test_v_prefix_is_tolerated(cache):
    _write(cache, "v0.2.0")
    assert un.update_notice("v0.1.9") == (1, "raven upgrade")


def test_corrupt_cache_is_ignored(cache):
    cache.write_text("{not json", encoding="utf-8")
    assert un.update_notice("0.1.9") is None


def test_refresh_skipped_when_cache_is_fresh(cache, monkeypatch):
    _write(cache, "0.1.9", checked_at=time.time())
    spawned = []
    monkeypatch.setattr(un.threading, "Thread", lambda *a, **k: spawned.append((a, k)) or _FakeThread())
    un.maybe_refresh_async()
    assert spawned == []


def test_refresh_spawned_when_cache_is_stale(cache, monkeypatch):
    _write(cache, "0.1.9", checked_at=time.time() - un._REFRESH_TTL_SECONDS - 1)
    spawned = []
    monkeypatch.setattr(un.threading, "Thread", lambda *a, **k: spawned.append(k) or _FakeThread())
    un.maybe_refresh_async()
    assert len(spawned) == 1
    assert spawned[0]["daemon"] is True


def test_refresh_spawned_when_cache_absent(cache, monkeypatch):
    spawned = []
    monkeypatch.setattr(un.threading, "Thread", lambda *a, **k: spawned.append(k) or _FakeThread())
    un.maybe_refresh_async()
    assert len(spawned) == 1


class _FakeThread:
    def start(self):  # noqa: D102 - test double
        pass
