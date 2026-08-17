"""On-disk persistence for the model catalog (per-model pricing + context window).

A single versioned JSON file under ``~/.raven/cache/``, written atomically
(temp-file + ``os.replace``) so concurrent multi-process readers never observe a
torn file. The catalog is a disposable, refetchable whole-blob cache with an
authoritative network source, so a lost write race just costs one extra refetch
— no lock is needed.

Named after what it persists (the model catalog), not its source: the storage
layer is source-agnostic, so a future catalog source reuses it unchanged. This
is the storage layer only; freshness (TTL), the in-process tier, and the actual
fetch are the caller's concern (see ``pricing._fetch_openrouter_models``).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from loguru import logger

from raven.config.paths import get_cache_dir

# Bump to force-invalidate every on-disk file after a schema change.
CACHE_VERSION = 1
CACHE_FILENAME = "model-catalog.json"

# Test seam: when set, overrides the on-disk location so tests never touch the
# real ~/.raven/cache/. None → derive from get_cache_dir() lazily.
_CACHE_PATH: Path | None = None


def cache_path() -> Path:
    """Resolve the on-disk catalog path (honoring the test override)."""
    if _CACHE_PATH is not None:
        return _CACHE_PATH
    return get_cache_dir() / CACHE_FILENAME


def load() -> tuple[dict[str, dict], float] | None:
    """Return ``(models, fetched_at)`` from disk, else None.

    A missing, unparseable, malformed, or wrong-version file is treated as a
    miss (None) — reading the cache must never raise into the cost path.
    """
    try:
        path = cache_path()
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if raw.get("version") != CACHE_VERSION:
        return None
    models = raw.get("models")
    if not isinstance(models, dict):
        return None
    try:
        fetched_at = float(raw.get("fetched_at", 0.0))
    except (TypeError, ValueError):
        return None
    return models, fetched_at


def save(models: dict[str, dict]) -> None:
    """Atomically persist the catalog: temp file + ``os.replace`` (best-effort).

    The temp file lives in the same directory (so the rename is POSIX-atomic on
    one filesystem) and is pid-suffixed so racing writers don't clobber each
    other's temp. A lost rename race just costs one extra fetch — never data.
    """
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "fetched_at": time.time(),
            "models": models,
        }
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        logger.debug("model_catalog_cache: failed to persist disk cache ({}), skipping", exc)
