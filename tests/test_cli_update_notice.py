"""Tests for the TUI startup update nudge (raven/cli/update_notice.py)."""

from __future__ import annotations

import json
import sys
import time

import pytest

from raven.cli import update_notice as un


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Point the module at a temp cache and let the notice run.

    The suite-wide autouse fixture opts every test out of the update check;
    this is the one place that exercises it, so it clears the flag.
    """
    path = tmp_path / "update_check.json"
    monkeypatch.delenv(un._OPT_OUT_ENV, raising=False)
    monkeypatch.setattr(un, "_cache_path", lambda: path)
    monkeypatch.setattr(un, "_upgrade_command_works", lambda: True)
    return path


def _write(path, latest, *, checked_at=None):
    payload = {"latest_version": latest, "checked_at": time.time() if checked_at is None else checked_at}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_no_cache_yields_no_notice(cache):
    assert un.update_notice("0.1.9") is None


def test_newer_cached_release_yields_notice(cache):
    _write(cache, "0.2.0")
    assert un.update_notice("0.1.9") == (True, "raven upgrade")


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
    assert un.update_notice("v0.1.9") == (True, "raven upgrade")


@pytest.mark.parametrize("installed", ["0.1.9rc1", "0.1.9.dev1", "0.1.9+local"])
def test_prerelease_installs_still_get_the_notice(cache, installed):
    # The strict grammar matches the whole string, so without normalising the
    # suffix these users would silently never see the hint.
    _write(cache, "0.2.0")
    assert un.update_notice(installed) == (True, "raven upgrade")


def test_prerelease_of_the_latest_release_is_not_nagged(cache):
    # 0.2.0rc1 compares equal to 0.2.0: the suffix is dropped, not ordered.
    _write(cache, "0.2.0")
    assert un.update_notice("0.2.0rc1") is None


def test_refresh_skipped_when_upgrade_command_would_fail(cache, monkeypatch):
    # An install that cannot run `raven upgrade` never sees the result, so it
    # should not pay for the fetch either.
    monkeypatch.setattr(un, "_upgrade_command_works", lambda: False)
    spawned = []
    monkeypatch.setattr("threading.Thread", lambda *a, **k: spawned.append(k) or _FakeThread())
    un.maybe_refresh_async()
    assert spawned == []


def test_corrupt_cache_is_ignored(cache):
    cache.write_text("{not json", encoding="utf-8")
    assert un.update_notice("0.1.9") is None


@pytest.mark.parametrize("payload", ['"0.2.0"', "[1]", "42", "null"])
def test_non_object_cache_is_ignored(cache, monkeypatch, payload):
    # Valid JSON that is not an object used to raise AttributeError out of
    # update_notice(), which took `raven tui` and session.create down with it.
    #
    # A non-object reads back as "no cache", so the refresh call below would
    # spawn a real thread whose write lands after monkeypatch has restored
    # _cache_path -- i.e. on the real home. The assertion only needs "does not
    # raise", so the thread is stubbed.
    monkeypatch.setattr("threading.Thread", lambda *a, **k: _FakeThread())
    cache.write_text(payload, encoding="utf-8")
    assert un.update_notice("0.1.9") is None
    un.maybe_refresh_async()


def test_no_notice_when_upgrade_command_would_fail(cache, monkeypatch):
    _write(cache, "0.2.0")
    monkeypatch.setattr(un, "_upgrade_command_works", lambda: False)
    assert un.update_notice("0.1.9") is None


def test_opt_out_env_silences_notice_and_fetch(cache, monkeypatch):
    _write(cache, "0.2.0")
    monkeypatch.setenv(un._OPT_OUT_ENV, "1")
    assert un.update_notice("0.1.9") is None

    spawned = []
    monkeypatch.setattr("threading.Thread", lambda *a, **k: spawned.append(k) or _FakeThread())
    un.maybe_refresh_async()
    assert spawned == []


def test_refresh_skipped_when_cache_is_fresh(cache, monkeypatch):
    _write(cache, "0.1.9", checked_at=time.time())
    spawned = []
    monkeypatch.setattr("threading.Thread", lambda *a, **k: spawned.append((a, k)) or _FakeThread())
    un.maybe_refresh_async()
    assert spawned == []


def test_refresh_spawned_when_cache_is_stale(cache, monkeypatch):
    _write(cache, "0.1.9", checked_at=time.time() - un._REFRESH_TTL_SECONDS - 1)
    spawned = []
    monkeypatch.setattr("threading.Thread", lambda *a, **k: spawned.append(k) or _FakeThread())
    un.maybe_refresh_async()
    assert len(spawned) == 1
    assert spawned[0]["daemon"] is True


def test_refresh_spawned_when_cache_absent(cache, monkeypatch):
    spawned = []
    monkeypatch.setattr("threading.Thread", lambda *a, **k: spawned.append(k) or _FakeThread())
    un.maybe_refresh_async()
    assert len(spawned) == 1


def test_failed_refresh_still_backs_off_and_keeps_version(cache, monkeypatch):
    # Offline / rate-limited / draft-release: stamping checked_at is what stops
    # every launch from refetching, and the known version must survive.
    _write(cache, "0.2.0", checked_at=0.0)

    def _boom():
        raise RuntimeError("offline")

    monkeypatch.setitem(sys.modules, "raven.cli.upgrade_commands", _FakeUpgrade(_boom))
    un._refresh()

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["latest_version"] == "0.2.0"
    assert time.time() - saved["checked_at"] < 60


def test_successful_refresh_records_fetched_version(cache, monkeypatch):
    monkeypatch.setitem(sys.modules, "raven.cli.upgrade_commands", _FakeUpgrade(lambda: _Release("0.3.0")))
    un._refresh()

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["latest_version"] == "0.3.0"


class _FakeThread:
    def start(self):  # noqa: D102 - test double
        pass


class _Release:
    def __init__(self, version: str) -> None:
        self.version = version


class _FakeUpgrade:
    """Stands in for raven.cli.upgrade_commands during _refresh()."""

    UpgradeError = RuntimeError

    def __init__(self, fetch) -> None:
        self._fetch = fetch

    def _fetch_latest_release(self):  # noqa: D102 - test double
        return self._fetch()

    def _version_key(self, value):  # noqa: D102 - test double
        raise RuntimeError(value)
