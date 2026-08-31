"""Tests for the TUI startup update nudge (raven/updates/update_notice.py)."""

from __future__ import annotations

import json
import sys
import time

import pytest

from raven.updates import update_notice as un


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

    monkeypatch.setitem(sys.modules, "raven.updates.upgrade", _FakeUpgrade(_boom))
    un._refresh()

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["latest_version"] == "0.2.0"
    assert time.time() - saved["checked_at"] < 60


def test_successful_refresh_records_fetched_version(cache, monkeypatch):
    monkeypatch.setitem(sys.modules, "raven.updates.upgrade", _FakeUpgrade(lambda: "0.3.0"))
    un._refresh()

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["latest_version"] == "0.3.0"


def test_refresh_uses_the_quota_free_lookup(cache, monkeypatch):
    # The daily check runs on every install behind a shared egress; routing it through
    # the API is what drains the 60/hour unauthenticated bucket.
    import raven.updates.upgrade as upgrade

    monkeypatch.setattr(upgrade, "_fetch_latest_release", _forbidden_api_call)
    monkeypatch.setattr(upgrade, "fetch_latest_version", lambda: "0.4.0")
    un._refresh()

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["latest_version"] == "0.4.0"


def _forbidden_api_call(*args, **kwargs):
    raise AssertionError("the update notice must not touch the GitHub API")


class _FakeThread:
    def start(self):  # noqa: D102 - test double
        pass


class _FakeUpgrade:
    """Stands in for raven.updates.upgrade during _refresh()."""

    UpgradeError = RuntimeError

    def __init__(self, fetch) -> None:
        self._fetch = fetch

    def fetch_latest_version(self):  # noqa: D102 - test double
        return self._fetch()

    def _version_key(self, value):  # noqa: D102 - test double
        raise RuntimeError(value)


# ---------------------------------------------------------------------------
# _upgrade_command_works: the real implementation, not the fixture's stub
# ---------------------------------------------------------------------------


def test_upgrade_command_works_follows_the_install_kind(monkeypatch) -> None:
    """It delegates to the uv-tool probe, so editable installs are not nudged."""
    import raven.updates.upgrade as upgrade

    monkeypatch.setattr(upgrade, "_is_uv_tool_install", lambda: True)
    assert un._upgrade_command_works() is True

    monkeypatch.setattr(upgrade, "_is_uv_tool_install", lambda: False)
    assert un._upgrade_command_works() is False


def test_upgrade_command_works_treats_a_probe_failure_as_no(monkeypatch) -> None:
    """A malformed uv receipt raises; unknown must read as "cannot upgrade"."""
    import raven.updates.upgrade as upgrade

    def _boom() -> bool:
        raise RuntimeError("malformed uv receipt")

    monkeypatch.setattr(upgrade, "_is_uv_tool_install", _boom)
    assert un._upgrade_command_works() is False


# ---------------------------------------------------------------------------
# check_for_update: the resident gateway's live check


def _speaking_fake(version: str) -> _FakeUpgrade:
    """A fake whose ordering works, unlike the default one built to raise.

    check_for_update compares versions after its fetch, so a fake that raises
    on comparison silences the very answer under test.
    """
    fake = _FakeUpgrade(lambda: version)
    fake._version_key = lambda value: tuple(int(part) for part in value.lstrip("v").split("."))
    return fake


def test_check_for_update_names_the_newer_build(cache, monkeypatch):
    monkeypatch.setitem(sys.modules, "raven.updates.upgrade", _speaking_fake("0.3.0"))
    monkeypatch.setattr(un, "_upgrade_command_works", lambda: True)

    assert un.check_for_update("0.2.0") == "0.3.0"
    # And it wrote the cache a later launch reads.
    assert json.loads(cache.read_text(encoding="utf-8"))["latest_version"] == "0.3.0"


def test_check_for_update_is_silent_when_current_is_the_latest(cache, monkeypatch):
    monkeypatch.setitem(sys.modules, "raven.updates.upgrade", _speaking_fake("0.3.0"))
    monkeypatch.setattr(un, "_upgrade_command_works", lambda: True)

    assert un.check_for_update("0.3.0") is None


def test_check_for_update_respects_the_opt_out(cache, monkeypatch):
    monkeypatch.setenv("RAVEN_NO_UPDATE_CHECK", "1")
    fetched = {"n": 0}

    def _fetch():
        fetched["n"] += 1
        return "0.9.9"

    monkeypatch.setitem(sys.modules, "raven.updates.upgrade", _FakeUpgrade(_fetch))

    assert un.check_for_update("0.1.0") is None
    assert fetched["n"] == 0, "opting out must skip the fetch, not just the answer"


# ---------------------------------------------------------------------------
# The beta channel's validator: what a 304 is allowed to change
# ---------------------------------------------------------------------------


@pytest.fixture
def beta(cache, monkeypatch):
    """A refresh that reads the beta pointer, with the registry stood in for.

    The channel is faked at `channel()` rather than by writing a beta.json, so
    the test never depends on whether the machine running it has joined.
    """
    from raven.updates import beta_channel

    chan = beta_channel.BetaChannel(project="85454048", username="raven-beta", token="gldt-secret")
    monkeypatch.setattr(beta_channel, "channel", lambda: chan)
    return beta_channel


def _write_read(path, latest, *, etag, checked_at=0.0):
    payload = {"latest_version": latest, "checked_at": checked_at, "etag": etag}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _answering(module, monkeypatch, read):
    """Point read_pointer at one canned answer; returns the validators offered."""
    offered = []

    def _read_pointer(chan, *, etag=None):
        offered.append(etag)
        return read

    monkeypatch.setattr(module, "read_pointer", _read_pointer)
    return offered


def test_an_unchanged_pointer_keeps_the_version_and_the_validator(cache, beta, monkeypatch):
    _write_read(cache, "0.1.12b6", etag='"c9b42d2d"')
    offered = _answering(beta, monkeypatch, beta.PointerRead(release=None, etag='"c9b42d2d"', unchanged=True))

    assert un._refresh() is True, "a 304 is a completed check, not a failed one"

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert offered == ['"c9b42d2d"'], "the cached validator has to go back out, or the 304 never happens"
    assert saved["latest_version"] == "0.1.12b6"
    assert saved["etag"] == '"c9b42d2d"'


def test_an_unchanged_pointer_still_stamps_the_check(cache, beta, monkeypatch):
    """checked_at is what tells the next launch its cache is fresh. A pointer
    confirmed unchanged is as fresh as an answer gets, so it counts as one --
    otherwise every launch spawns a refresh thread for a channel that has not
    moved."""
    _write_read(cache, "0.1.12b6", etag='"c9b42d2d"', checked_at=0.0)
    _answering(beta, monkeypatch, beta.PointerRead(release=None, etag='"c9b42d2d"', unchanged=True))

    un._refresh()

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert time.time() - saved["checked_at"] < 60


def test_a_moved_pointer_updates_both_the_version_and_the_validator(cache, beta, monkeypatch):
    from raven.updates.upgrade import ReleaseInfo

    _write_read(cache, "0.1.12b6", etag='"old"')
    release = ReleaseInfo(version="0.1.12b7", wheel_url="https://gitlab.com/wheel")
    offered = _answering(beta, monkeypatch, beta.PointerRead(release=release, etag='"new"', unchanged=False))

    assert un._refresh() is True

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert offered == ['"old"']
    assert saved["latest_version"] == "0.1.12b7"
    assert saved["etag"] == '"new"', "a stale validator would 304 against a pointer that has moved on"


def test_a_failed_read_reports_it_and_keeps_both(cache, beta, monkeypatch):
    _write_read(cache, "0.1.12b6", etag='"c9b42d2d"')

    def _boom(chan, *, etag=None):
        raise RuntimeError("offline")

    monkeypatch.setattr(beta, "read_pointer", _boom)

    assert un._refresh() is False, "the announcer's backoff reads this"

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["latest_version"] == "0.1.12b6"
    assert saved["etag"] == '"c9b42d2d"'


def test_a_validator_without_a_version_is_not_offered(cache, beta, monkeypatch):
    """A 304 means "unchanged from what you have". With no version cached there
    is nothing for that to mean, and offering the validator anyway would pin the
    notice off until the pointer happened to move."""
    cache.write_text(json.dumps({"checked_at": 0.0, "etag": '"orphan"'}), encoding="utf-8")
    from raven.updates.upgrade import ReleaseInfo

    release = ReleaseInfo(version="0.1.12b6", wheel_url="https://gitlab.com/wheel")
    offered = _answering(beta, monkeypatch, beta.PointerRead(release=release, etag='"fresh"', unchanged=False))

    un._refresh()

    assert offered == [None]
    assert json.loads(cache.read_text(encoding="utf-8"))["latest_version"] == "0.1.12b6"


def test_the_stable_path_keeps_no_validator(cache, stable, monkeypatch):
    """Stable reads GitHub's release-page redirect, which this cache has no
    validator for; a leftover one from a machine that left the channel must not
    survive as a key nothing sets."""
    _write_read(cache, "0.2.0", etag='"leftover"')
    monkeypatch.setitem(sys.modules, "raven.updates.upgrade", _FakeUpgrade(lambda: "0.3.0"))

    un._refresh()

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["latest_version"] == "0.3.0"
    assert "etag" not in saved


# ---------------------------------------------------------------------------
# check_now: the same answer, plus why a None is one
# ---------------------------------------------------------------------------


@pytest.fixture
def stable(monkeypatch):
    """An install that is not on the beta channel.

    Pinned rather than inherited: the fakes below stand in for the GitHub lookup,
    which `_refresh` only reaches on the stable path. Without this the test reads
    whether the developer running it happens to have joined the channel, and on a
    machine that has, it fetches the real registry instead of the fake.
    """
    from raven.updates import beta_channel

    monkeypatch.setattr(beta_channel, "channel", lambda: None)


def test_check_now_reports_a_reached_source_separately_from_a_newer_build(cache, stable, monkeypatch):
    monkeypatch.setitem(sys.modules, "raven.updates.upgrade", _speaking_fake("0.3.0"))

    assert un.check_now("0.2.0") == ("0.3.0", True)
    assert un.check_now("0.3.0") == (None, True), "nothing newer is still a reached source"


def test_check_now_reports_a_source_that_never_answered(cache, stable, monkeypatch):
    def _boom():
        raise RuntimeError("offline")

    monkeypatch.setitem(sys.modules, "raven.updates.upgrade", _FakeUpgrade(_boom))

    assert un.check_now("0.2.0") == (None, False)


def test_check_now_treats_a_free_silence_as_reached(cache, monkeypatch):
    """Opted out or cannot upgrade: no request was made, so there is no failing
    source to back away from and the caller should keep its cadence."""
    monkeypatch.setenv(un._OPT_OUT_ENV, "1")

    assert un.check_now("0.1.0") == (None, True)
