"""What each provider last said it serves, kept so the picker can offer it.

The picker's list is built from sources that need no network -- what the user
added, the curated shortlist, LiteLLM's bundled catalogue -- because it is read
on every open and a live round trip to each vendor is what made it slow. That
list lags the vendor: OpenRouter answers with several hundred models where the
bundled snapshot knows about a hundred. Whenever raven does ask the vendor (a
connect's test, a "check again", the add-model sheet), the answer is written
here with each id's kind, and the picker unions it in without asking again.

One small JSON file under the cache directory, replaced atomically. A lost
write costs nothing but a shorter list until the next ask.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from raven.config.paths import get_cache_dir

CACHE_VERSION = 1
CACHE_FILENAME = "served-models.json"


def cache_path() -> Path:
    return get_cache_dir() / CACHE_FILENAME


def _read() -> dict[str, Any]:
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION or not isinstance(raw.get("providers"), dict):
        return {}
    return raw["providers"]


def _write(providers: dict[str, Any]) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "providers": providers}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def remember(slug: str, models: dict[str, str]) -> None:
    """Record what ``slug`` serves right now: model id to kind."""
    providers = _read()
    providers[slug] = {"at": time.time(), "models": {str(k): str(v) for k, v in models.items() if k}}
    _write(providers)


def recall(slug: str) -> dict[str, str]:
    """What ``slug`` last said it serves, or nothing if it was never asked."""
    entry = _read().get(slug)
    models = entry.get("models") if isinstance(entry, dict) else None
    return {str(k): str(v) for k, v in models.items()} if isinstance(models, dict) else {}


def forget(slug: str) -> None:
    """Drop a provider's answer, as a disconnect does."""
    providers = _read()
    if providers.pop(slug, None) is not None:
        _write(providers)
