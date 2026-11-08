"""Which Codex CLI release a generation request says it comes from.

The official CLI sends its own release on every request to the Codex backend, as
a ``version`` header its built-in OpenAI provider carries
(``create_openai_provider`` in ``codex-rs/model-provider-info``). The backend
gates models by client version, so a request that names an old release, or
none, can be refused a model the account is offered. Raven has no release of its
own to send; it sends the newest one OpenAI has published.

The newest release is asked of npm, where the CLI is published as
``@openai/codex``, and only its ``latest`` tag is read: ``alpha`` carries
prereleases (``0.164.0-alpha.3`` beside ``0.163.0``), which are not a client
anyone runs. Asked rarely, kept on disk for when npm cannot be reached, and
backed by a release this code was written against, so the answer is always a
real release and never the ``0.0.0`` that hid the newest models.

The catalogue does not use this: it asks as ``DISCOVERY_CLIENT_VERSION`` in
``codex_catalog``, to see every model rather than the ones today's release can
reach.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from raven.home import raven_home
from raven.utils import mirrors

DIST_TAGS_URL = "https://registry.npmjs.org/-/package/@openai/codex/dist-tags"
#: The same tags from npm's mirror, asked only when npm cannot be reached.
DIST_TAGS_MIRROR_URL = f"{mirrors.NPM_REGISTRY}/-/package/@openai/codex/dist-tags"

#: The answer when npm has never been reached. A real release -- the backend may
#: one day check -- and no older than the newest known generation-side gate:
#: ``gpt-6.1-sol`` needs ``0.159.0``. Bump it with Codex CLI releases.
FALLBACK_VERSION = "0.163.0"

CACHE_FILENAME = "codex-client-version.json"

#: Releases come every few days; a turn should not wait on npm more often than this.
_CACHE_TTL_SECONDS = 15 * 60

_RELEASE = re.compile(r"\d+\.\d+\.\d+")

#: (monotonic stamp, version)
_cache: tuple[float, str] | None = None


def resolve(*, timeout: float = 3.0) -> str:
    """The release to send: npm's ``latest``, else the last one seen, else ours.

    Never raises -- a turn is not worth failing over which release it names -- and
    never answers older than ``FALLBACK_VERSION``, so ``0.0.0`` cannot come back.
    Whatever is answered is kept for the TTL, a fallback included: npm being
    unreachable is not fixed by asking again on the next turn, and paying the
    timeout on every turn while offline would be.
    """
    global _cache

    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_SECONDS:
        return _cache[1]

    try:
        version = _usable(_fetch_latest(timeout=timeout))
    except Exception:
        version = None

    if version is not None:
        _remember(version)
    else:
        version = _usable(_recall()) or FALLBACK_VERSION

    _cache = (now, version)

    return version


def reset_cache() -> None:
    """Forget the in-process answer, for a caller that wants this instant's."""
    global _cache

    _cache = None


def cache_path() -> Path:
    """Where the last good answer is kept, under the same ``cache`` directory.

    Not ``get_cache_dir()``: that helper creates the directory on the way out,
    and a cache that cannot be prepared must cost the next process the last-good
    value rather than this turn its answer. Resolution still touches the
    environment, so both callers guard it.
    """
    return raven_home() / "cache" / CACHE_FILENAME


def _fetch_latest(*, timeout: float) -> object:
    import httpx

    try:
        response = httpx.get(DIST_TAGS_URL, timeout=timeout)
    except httpx.HTTPError as exc:
        if not (mirrors.enabled() and mirrors.unreachable(exc)):
            raise
        response = httpx.get(DIST_TAGS_MIRROR_URL, timeout=timeout)
    response.raise_for_status()
    tags = response.json()

    return tags.get("latest") if isinstance(tags, dict) else None


def _usable(value: object) -> str | None:
    """``value`` if it is a plain release no older than ours, else None.

    Plain means ``X.Y.Z`` and nothing else: a prerelease, an empty tag or a
    stray ``v`` is refused rather than repaired. Older than ours means npm, or a
    file written before this code shipped, names a release this code already
    knows is behind.
    """
    if not isinstance(value, str) or not _RELEASE.fullmatch(value):
        return None
    if _parts(value) < _parts(FALLBACK_VERSION):
        return None

    return value


def _parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _recall() -> object:
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except Exception:
        return None

    return raw.get("version") if isinstance(raw, dict) else None


def _remember(version: str) -> None:
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"version": version, "at": time.time()}), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass
