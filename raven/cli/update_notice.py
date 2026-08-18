"""Startup update nudge for the TUI status bar.

The status bar's right slot shows an "update available" hint in place of the
cwd/branch label when the session init bundle carries ``update_available`` /
``update_command`` (see ``ui-tui/src/components/appChrome.tsx``); this module
is what fills those in.

The live check reads the release page redirect (not the releases API, whose
unauthenticated quota is 60 requests per hour per IP -- a daily check from every
install behind one egress is enough to drain it, and a nudge must not cost the
budget that ``raven upgrade`` needs). An install that joined the beta channel
asks that channel's own registry instead, which never used the API to begin with.
Either way it is too slow for the session-create hot path, so we keep a small
cache in the runtime cache dir and refresh it in a daemon thread at most once a
day. A launch therefore shows the notice based on the *cached* latest version;
the first launch after a release lands refreshes the cache and the notice appears
on the next launch. Any network or parse failure is swallowed -- an update nudge
must never break startup.

Set ``RAVEN_NO_UPDATE_CHECK=1`` to opt out of both the fetch and the hint.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_CACHE_NAME = "update_check.json"
_REFRESH_TTL_SECONDS = 24 * 60 * 60
_UPGRADE_COMMAND = "raven upgrade"
_OPT_OUT_ENV = "RAVEN_NO_UPDATE_CHECK"
_TRUTHY = {"1", "true", "yes", "on"}


def _cache_path() -> Path:
    # Resolved per call, not at import: get_cache_dir() follows the active
    # config path, which set_config_path() can move after this module loads.
    from raven.config import paths

    return paths.get_cache_dir() / _CACHE_NAME


def _disabled() -> bool:
    return os.environ.get(_OPT_OUT_ENV, "").strip().lower() in _TRUTHY


def _release_prefix(value: str) -> str:
    """Reduce ``0.2.0rc1`` / ``0.1.9.dev1`` to the ``X.Y.Z`` it builds on.

    The strict parser matches the whole string, so a prerelease or dev suffix
    would read as unparseable and silence the hint for anyone running one. The
    suffix is dropped rather than ordered: an rc of a release compares equal to
    it, so an rc user is not nagged to "upgrade" to the version they are
    already testing.
    """
    raw = value.strip().lstrip("vV")
    parts = []
    for part in raw.split(".")[:3]:
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            return raw
        parts.append(digits)
    return ".".join(parts) if len(parts) == 3 else raw


def _version_key(value: str) -> tuple[int, ...] | None:
    """Parse ``1.2.3`` / ``v1.2.3`` / ``1.2.3rc1``, ``None`` when unparseable.

    ``upgrade_commands._version_key`` is the single source of truth for the
    grammar; it raises for anything it cannot read, which here just means
    "show no notice".

    On the beta channel the suffix is the whole point -- ``0.1.12b1`` and
    ``0.1.12b2`` are two different builds -- so that channel's ordering reads
    the version whole instead of reducing it to the release it builds on.
    """
    from raven.cli import beta_channel
    from raven.cli.upgrade_commands import UpgradeError

    if beta_channel.is_active():
        try:
            return beta_channel.release_key(value)
        except UpgradeError:
            return None

    from raven.cli.upgrade_commands import _version_key as strict_key

    try:
        return strict_key(_release_prefix(value))
    except (UpgradeError, AttributeError):
        return None


def _read_cache() -> dict | None:
    try:
        parsed = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    # A hand-edited cache can be valid JSON and still not an object; without
    # this guard the .get() below raises and takes `raven tui` down with it.
    return parsed if isinstance(parsed, dict) else None


def _write_cache(latest_version: str | None, *, now: float) -> None:
    payload: dict[str, object] = {"checked_at": now}
    if latest_version is not None:
        payload["latest_version"] = latest_version
    try:
        path = _cache_path()
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _refresh() -> None:
    # Imported lazily: the GitHub client pulls in httpx, which we keep off the
    # session-create hot path (this runs in a daemon thread).
    cache = _read_cache() or {}
    previous = cache.get("latest_version")
    keep = previous if isinstance(previous, str) else None

    try:
        from raven.cli import beta_channel
        from raven.cli.upgrade_commands import fetch_latest_version

        chan = beta_channel.channel()
        if chan is None:
            _write_cache(fetch_latest_version(), now=time.time())
        else:
            _write_cache(beta_channel.fetch_latest(chan).version, now=time.time())
    except Exception:
        # Offline, rate-limited, or the latest release is a draft/prerelease.
        # Stamp checked_at anyway so we back off for a full TTL instead of
        # refetching on every launch, and keep whatever version we had.
        _write_cache(keep, now=time.time())


def maybe_refresh_async() -> None:
    """Refresh the cached latest version in the background if it is stale.

    Fire-and-forget: spawns a daemon thread only when the cache is missing or
    older than the TTL, so a normal launch touches the network at most once a
    day and never blocks. Installs that cannot run ``raven upgrade`` skip the
    fetch entirely -- they would never be shown the result.
    """
    if _disabled() or not _upgrade_command_works():
        return

    cache = _read_cache()
    if cache is not None:
        checked_at = cache.get("checked_at")
        if isinstance(checked_at, (int, float)) and (time.time() - checked_at) < _REFRESH_TTL_SECONDS:
            return

    import threading

    threading.Thread(target=_refresh, daemon=True).start()


def _upgrade_command_works() -> bool:
    """Whether ``raven upgrade`` can actually do anything on this install.

    It refuses to run for editable and non-uv-tool installs, so nudging those
    users points them at a command that always exits 1.
    """
    try:
        from raven.cli.upgrade_commands import _is_uv_tool_install

        return _is_uv_tool_install()
    except Exception:
        # A malformed uv receipt raises UpgradeError; treat unknown as "no".
        return False


def check_for_update(current_version: str) -> str | None:
    """Fetch the latest release right now and name it if it is newer. Blocking.

    The cached path above trades freshness for startup speed: the notice shows
    one launch late. A resident gateway has no next launch to lean on, so its
    periodic announcer calls this instead -- one live fetch, the same cache
    written (a tab opened later still benefits), and the same silences: opted
    out, an install that cannot upgrade, or nothing newer all answer ``None``.
    """
    if _disabled() or not _upgrade_command_works():
        return None
    _refresh()
    if update_notice(current_version) is None:
        return None
    cache = _read_cache() or {}
    latest = cache.get("latest_version")
    return latest if isinstance(latest, str) else None


def update_notice(current_version: str) -> tuple[bool, str] | None:
    """Return ``(available, command)`` when the cached latest release is newer.

    Returns ``None`` when up to date, when the cache is absent or unreadable,
    when either version is unparseable, or when ``raven upgrade`` would fail on
    this install anyway.
    """
    if _disabled():
        return None

    cache = _read_cache()
    if not cache:
        return None

    latest = cache.get("latest_version")
    if not isinstance(latest, str):
        return None

    latest_key = _version_key(latest)
    current_key = _version_key(current_version)
    if latest_key is None or current_key is None or latest_key <= current_key:
        return None

    if not _upgrade_command_works():
        return None

    return True, _UPGRADE_COMMAND
