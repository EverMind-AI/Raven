"""Crash-durable per-turn diagnostic sidecar.

The canonical session is persisted once, at the end of a successful turn
(``_save_turn`` + ``SessionManager.save``) — a turn killed mid-flight
(batch timeout, OOM, SIGKILL) leaves no trace on disk. The journal fills
that observability gap: it appends each turn product to
``<session>.partial.jsonl`` as it happens and deletes the file once the
canonical save succeeds, so a surviving partial file is always the
wreckage of an interrupted turn, never a duplicate of saved data.

Diagnostic only: the journal is never read back by the agent, carries no
train-serve weight (the canonical trajectory stays the single source of
truth), and must never break a turn — the first write failure disables it
for the rest of the turn with a single warning.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

_KEEP_ENV = "RAVEN_KEEP_TURN_JOURNAL"
_KEEP_MIN_FREE_BYTES = 5 * 1024**3


class TurnJournal:
    """Append-only event log for one agent turn.

    ``base`` is the index of the first message that belongs to the current
    turn; earlier entries (system prompt, restored history) are already on
    disk in the canonical session and are never journaled.
    """

    def __init__(self, path: Path, base: int = 0):
        self.path = path
        self._watermark = base
        self._disabled = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._disabled = True

    def event(self, name: str, **fields: Any) -> None:
        """Write a marker line (e.g. ``llm_call_start``) with a wall-clock stamp."""
        self._write({"event": name, **fields})

    def checkpoint(self, messages: list[dict[str, Any]]) -> None:
        """Journal every message appended since the last checkpoint.

        A message list shorter than the watermark means history was cut
        without ``rewind`` being told (emergency context shrink) — record
        the cut and realign rather than crash or double-write.
        """
        if len(messages) < self._watermark:
            self._write({"event": "history_shrunk", "to": len(messages)})
            self._watermark = len(messages)
        for m in messages[self._watermark :]:
            self._write(dict(m))
        self._watermark = len(messages)

    def rewind(self, to: int, **fields: Any) -> None:
        """Record a hook rollback and re-arm the watermark so the messages
        injected in place of the popped ones get journaled next checkpoint."""
        self._write({"event": "rollback", **fields})
        self._watermark = min(self._watermark, to)

    def discard(self) -> None:
        """Delete the sidecar after the canonical save succeeded.

        With ``RAVEN_KEEP_TURN_JOURNAL=1`` the file is renamed instead. The
        journal records each tool body at the moment it was appended, before any
        emergency shrink rewrote it, so it is the only on-disk copy of evidence
        the canonical trajectory has replaced with an elision placeholder — that
        makes every offline grounding measurement read from the journal a real
        figure rather than a lower bound. Off by default: this is hundreds of KB
        per turn on a shared volume that has been filled by other tenants
        before, so the switch also refuses to keep anything once free space
        drops below the headroom a training run needs.
        """
        if os.environ.get(_KEEP_ENV, "").strip().lower() in ("1", "true", "yes"):
            try:
                free = shutil.disk_usage(self.path.parent).free
            except OSError:
                free = 0
            if free >= _KEEP_MIN_FREE_BYTES:
                kept = self.path.with_suffix(".kept.jsonl")
                try:
                    self.path.replace(kept)
                    return
                except OSError:
                    logger.warning("turn journal: could not keep {}", kept)
            else:
                logger.warning(
                    "turn journal: {} free below {} GiB, discarding instead of keeping",
                    self.path.parent,
                    _KEEP_MIN_FREE_BYTES // 1024**3,
                )
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("turn journal: could not remove {}", self.path)

    def _write(self, record: dict[str, Any]) -> None:
        if self._disabled:
            return
        record.setdefault("ts", datetime.now().isoformat())
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            self._disabled = True
            logger.warning("turn journal write failed, disabled for this turn: {}", self.path)
