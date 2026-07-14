"""Idempotent state tracker for cold-start import."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.atomic_io import atomic_replace

_DEFAULT_PATH = Path.home() / ".raven" / "import_state.json"


class ImportState:
    """Tracks which sources have been imported to enable resume.

    Storage layout (``~/.raven/import_state.json``, configurable)::

        {
          "meta": {"total": 42},
          "entries": {
            "claude_code:proj-memory": {"status": "submitted", ...},
            ...
          }
        }

    The state dict is cached in memory after the first read.  Mutations
    update the cache and flush to disk atomically via
    :func:`raven.utils.atomic_io.atomic_replace`.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        self._cache: dict[str, Any] | None = None

    def is_submitted(self, platform: str, source_key: str) -> bool:
        entry = self._entries().get(f"{platform}:{source_key}")
        return entry is not None and entry.get("status") == "submitted"

    def mark_submitted(self, platform: str, source_key: str) -> None:
        self._mark(platform, source_key, "submitted")

    def mark_failed(self, platform: str, source_key: str, error: str) -> None:
        self._mark(platform, source_key, "failed", error=error)

    def set_total(self, total: int) -> None:
        """Record the total number of importable units from a scan."""
        self._ensure_loaded().setdefault("meta", {})["total"] = total
        self._flush()

    def get_summary(self) -> dict[str, int]:
        """Return ``{"total", "submitted", "failed"}`` counts."""
        entries = self._entries()
        counts = Counter(v.get("status") for v in entries.values())
        meta = self._ensure_loaded().get("meta", {})
        return {
            "total": meta.get("total", len(entries)),
            "submitted": counts.get("submitted", 0),
            "failed": counts.get("failed", 0),
        }

    def get_progress(self) -> dict[str, Any]:
        return {
            "meta": dict(self._ensure_loaded().get("meta", {})),
            "entries": dict(self._entries()),
        }

    # -- internals ----------------------------------------------------------

    def _mark(
        self,
        platform: str,
        source_key: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        self._entries()[f"{platform}:{source_key}"] = {
            "status": status,
            "timestamp": time.time(),
            "error": error,
        }
        self._flush()

    def _entries(self) -> dict[str, dict[str, Any]]:
        return self._ensure_loaded().setdefault("entries", {})

    def _ensure_loaded(self) -> dict[str, Any]:
        if self._cache is None:
            self._cache = self._read_from_disk()
        return self._cache

    def _read_from_disk(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            backup = self._path.with_suffix(".json.corrupt")
            logger.warning(
                "Corrupt import state at %s -- backing up to %s",
                self._path,
                backup,
            )
            os.replace(self._path, backup)
            return {}
        if "entries" in raw:
            return raw
        # Migrate flat layout from earlier drafts.
        return {"entries": raw}

    def _flush(self) -> None:
        data = self._ensure_loaded()
        atomic_replace(self._path, json.dumps(data, indent=2))


__all__ = ["ImportState"]
