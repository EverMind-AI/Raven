"""On-disk cache of vendor search responses and reader pages, shared by every run on a host.

Off by default. A rollout that samples each question several times re-reads the same pages:
measured over 2,740 runs (four samples per question), a later sample could have taken 20.6%
of its reader fetches from a sibling's, while byte-identical searches across runs were 0.4%.

Only answers worth replaying are stored: a search that returned organic rows, a reader page
of 200 that kept its encoding, is not a stub, and carries no target error or CAPTCHA header.
Everything the tools do after the transport (benchmark guard, dedup, digest, ledger) still
runs on a hit, so a cached answer is shaped exactly like a fresh one.

Entries are one compressed file each, written to a temporary name and renamed into place, so
concurrent processes never see a torn entry and a race costs one duplicate request. Keys never
contain credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import zlib
from pathlib import Path

__all__ = ["WebCache"]

_EVENTS = "events.jsonl"


class WebCache:
    def __init__(self, root: str | Path, ttl_seconds: float, *, search: bool = True, fetch: bool = True) -> None:
        self.root = Path(root).expanduser()
        self.ttl_seconds = float(ttl_seconds)
        self.search = search
        self.fetch = fetch

    def _path(self, kind: str, key: str) -> Path:
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / kind / h[:2] / f"{h}.z"

    def get(self, kind: str, key: str) -> str | None:
        """The stored body, or ``None`` when absent, expired or unreadable. Never raises."""
        hit: str | None = None
        try:
            entry = json.loads(zlib.decompress(self._path(kind, key).read_bytes()))
            if time.time() - float(entry["t"]) < self.ttl_seconds and isinstance(entry.get("body"), str):
                hit = entry["body"]
        except (OSError, ValueError, KeyError, TypeError, zlib.error):
            pass
        self._event(kind, hit is not None)
        return hit

    def put(self, kind: str, key: str, body: str) -> None:
        """Store ``body`` under ``key``. Never raises: a full disk must not fail the tool call."""
        try:
            path = self._path(kind, key)
            path.parent.mkdir(parents=True, exist_ok=True)
            blob = zlib.compress(json.dumps({"t": time.time(), "body": body}, ensure_ascii=False).encode("utf-8"))
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(blob)
                os.replace(tmp, path)
            except BaseException:
                os.unlink(tmp)
                raise
        except (OSError, ValueError):  # ValueError: a lone surrogate cannot be encoded
            pass

    async def aget(self, kind: str, key: str) -> str | None:
        return await asyncio.to_thread(self.get, kind, key)

    async def aput(self, kind: str, key: str, body: str) -> None:
        await asyncio.to_thread(self.put, kind, key, body)

    def _event(self, kind: str, hit: bool) -> None:
        # One short O_APPEND write per lookup, so the hit rate is readable without the run dirs.
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.root / _EVENTS, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": round(time.time(), 3), "kind": kind, "hit": hit}) + "\n")
        except OSError:
            pass
