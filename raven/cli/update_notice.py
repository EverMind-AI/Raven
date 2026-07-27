"""Startup update nudge for the TUI status bar.

The status bar's right slot shows an "update available" hint in place of the
cwd/branch label when the session init bundle carries ``update_behind`` /
``update_command`` (see ``ui-tui/src/components/appChrome.tsx``); this module
is what fills those in.

The live check hits the GitHub releases API, which is too slow to run on the
session-create hot path, so we keep a small cache in ``~/.raven`` and refresh
it in a daemon thread at most once a day. A launch therefore shows the notice
based on the *cached* latest version; the first launch after a release lands
refreshes the cache and the notice appears on the next launch. Any network or
parse failure is swallowed -- an update nudge must never break startup.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

_CACHE_PATH = Path.home() / ".raven" / "update_check.json"
_REFRESH_TTL_SECONDS = 24 * 60 * 60
_UPGRADE_COMMAND = "raven upgrade"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _version_key(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(value.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _read_cache() -> dict | None:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(latest_version: str, *, now: float) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps({"checked_at": now, "latest_version": latest_version}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _refresh() -> None:
    # Imported lazily: the GitHub client pulls in httpx, which we keep off the
    # session-create hot path (this runs in a daemon thread).
    try:
        from raven.cli.upgrade_commands import _fetch_latest_release

        release = _fetch_latest_release()
        _write_cache(release.version, now=time.time())
    except Exception:
        # Network error, rate limit, parse failure -- try again next TTL.
        pass


def maybe_refresh_async() -> None:
    """Refresh the cached latest version in the background if it is stale.

    Fire-and-forget: spawns a daemon thread only when the cache is missing or
    older than the TTL, so a normal launch touches the network at most once a
    day and never blocks.
    """
    cache = _read_cache()
    if cache is not None:
        checked_at = cache.get("checked_at")
        if isinstance(checked_at, (int, float)) and (time.time() - checked_at) < _REFRESH_TTL_SECONDS:
            return
    threading.Thread(target=_refresh, daemon=True).start()


def update_notice(current_version: str) -> tuple[int, str] | None:
    """Return ``(behind, command)`` when the cached latest release is newer.

    ``behind`` is a positive flag (the status bar shows a version-agnostic
    "Update available", not a count). Returns ``None`` when up to date, when
    the cache is absent, or when either version is unparseable.
    """
    cache = _read_cache()
    if not cache:
        return None
    latest = cache.get("latest_version")
    if not isinstance(latest, str):
        return None
    latest_key = _version_key(latest)
    current_key = _version_key(current_version)
    if latest_key is None or current_key is None:
        return None
    if latest_key > current_key:
        return 1, _UPGRADE_COMMAND
    return None
