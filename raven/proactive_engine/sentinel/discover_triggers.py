"""DiscoverTriggerStore — file-based IPC for ad-hoc TaskDiscoverer fires.

Mirrors cron's jobs.json mechanics: atomic write (tmp + os.replace) +
fcntl advisory lock + caller-driven polling. CLI ``discover-now`` adds
a trigger; gateway tick / startup drains it.

Triggers are consume-and-delete (no retry on crash). For a separate
PendingDecisionStore: that one holds **dispatched** menus the user can
pick from; a trigger is **pre-dispatch intent** with no options yet.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from loguru import logger

_STORE_VERSION = 1


@dataclass
class DiscoverTrigger:
    """One ad-hoc discovery request queued by a CLI process."""

    id: str
    channel: str
    to: str
    queued_at_ms: int


class DiscoverTriggerStore:
    """File-backed FIFO of pending TaskDiscoverer fires.

    Default path ``<sentinel_dir>/discover_triggers.json``. Schema::

        {"version": 1,
         "triggers": [
            {"id": "trg_abc12345",
             "channel": "feishu",
             "to": "ou_...",
             "queuedAtMs": 1716988000000}
         ]}
    """

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.lock_path = store_path.with_suffix(store_path.suffix + ".lock")
        self._can_lock = sys.platform != "win32"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Exclusive advisory lock. No-op on Windows."""
        if not self._can_lock:
            yield
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock_fd:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)

    def _read(self) -> list[DiscoverTrigger]:
        """Read triggers from disk. Returns [] on missing or malformed file —
        a corrupt file must not break the gateway tick loop."""
        if not self.store_path.exists():
            return []
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "DiscoverTriggerStore: failed to parse {}: {}: {} — treating as empty",
                self.store_path,
                type(exc).__name__,
                exc,
            )
            return []
        triggers: list[DiscoverTrigger] = []
        for raw in data.get("triggers", []):
            try:
                triggers.append(
                    DiscoverTrigger(
                        id=raw["id"],
                        channel=raw["channel"],
                        to=raw["to"],
                        queued_at_ms=int(raw.get("queuedAtMs", 0)),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "DiscoverTriggerStore: skipping malformed entry {}: {}: {}",
                    raw,
                    type(exc).__name__,
                    exc,
                )
        return triggers

    def _write(self, triggers: list[DiscoverTrigger]) -> None:
        """Atomic write (tmp + replace). Caller holds ``_locked()``."""
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": _STORE_VERSION,
            "triggers": [
                {
                    "id": t.id,
                    "channel": t.channel,
                    "to": t.to,
                    "queuedAtMs": t.queued_at_ms,
                }
                for t in triggers
            ],
        }
        tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, self.store_path)

    def add(self, channel: str, to: str) -> DiscoverTrigger:
        """Enqueue a trigger. Returns the persisted record so callers
        can echo the assigned id back to the user."""
        trigger = DiscoverTrigger(
            id=f"trg_{secrets.token_hex(4)}",
            channel=channel,
            to=to,
            queued_at_ms=int(time.time() * 1000),
        )
        with self._locked():
            triggers = self._read()
            triggers.append(trigger)
            self._write(triggers)
        return trigger

    def consume_all(self) -> list[DiscoverTrigger]:
        """Atomically drain all queued triggers. fcntl serializes
        concurrent consumers so each trigger fires exactly once."""
        with self._locked():
            triggers = self._read()
            if not triggers:
                return []
            self._write([])
        return triggers


__all__ = ["DiscoverTrigger", "DiscoverTriggerStore"]
