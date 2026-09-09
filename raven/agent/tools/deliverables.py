"""Persisted registry of files delivered to the user.

The token is the download capability: the gateway HTTP routes serve a file only
when a token resolves here, so a path never travels in a URL and there is
nothing for a caller to traverse.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.atomic_io import atomic_replace


@dataclass(frozen=True)
class DeliverableRecord:
    """One delivered file, addressable by its opaque token.

    ``title`` and ``description`` are what the agent said the file IS, and they
    are stored rather than left on the turn event: the event reaches a client
    once, and this registry is what answers "what has this conversation handed
    over" afterwards -- after a reconnect, after a compaction archived the turn
    that carried the manifest, or on a client that was not open at the time.
    Both default to empty so a registry written before they existed still loads.
    """

    token: str
    path: str
    name: str
    media_type: str
    size: int
    conversation: str
    created_at: str
    title: str = ""
    description: str = ""


class DeliverableStore:
    """Token -> delivered file, persisted as one JSON object."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._by_token: dict[str, DeliverableRecord] = {}
        self._load()
        self.prune_missing()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("deliverables: unreadable store at {}; starting empty", self._path)
            return
        if not isinstance(raw, dict):
            return
        for token, fields in raw.items():
            if not isinstance(fields, dict):
                continue
            try:
                self._by_token[token] = DeliverableRecord(token=token, **fields)
            except TypeError:
                logger.warning("deliverables: dropping malformed entry {}", token)

    def _save(self) -> None:
        payload = {
            token: {k: v for k, v in asdict(rec).items() if k != "token"} for token, rec in self._by_token.items()
        }
        atomic_replace(self._path, json.dumps(payload, ensure_ascii=False, indent=2))

    def prune_missing(self) -> int:
        """Drop entries whose file is gone. Returns how many were dropped."""
        gone = [token for token, rec in self._by_token.items() if not os.path.isfile(rec.path)]
        for token in gone:
            del self._by_token[token]
        if gone:
            self._save()
        return len(gone)

    def register(
        self,
        *,
        path: str,
        name: str,
        media_type: str,
        size: int,
        conversation: str,
        title: str = "",
        description: str = "",
    ) -> DeliverableRecord:
        """Register a delivered file, reusing the token if this conversation
        already delivered this path (the size is refreshed, so the UI never
        shows a stale figure for a file that changed)."""
        for token, rec in self._by_token.items():
            if rec.conversation == conversation and rec.path == path:
                refreshed = replace(
                    rec, name=name, media_type=media_type, size=size, title=title, description=description
                )
                self._by_token[token] = refreshed
                self._save()
                return refreshed
        rec = DeliverableRecord(
            token=secrets.token_urlsafe(32),
            path=path,
            name=name,
            media_type=media_type,
            size=size,
            conversation=conversation,
            created_at=datetime.now(UTC).isoformat(),
            title=title,
            description=description,
        )
        self._by_token[rec.token] = rec
        self._save()
        return rec

    def get(self, token: str) -> DeliverableRecord | None:
        return self._by_token.get(token)

    def for_conversation(self, conversation: str) -> list[DeliverableRecord]:
        """Everything this conversation handed over, oldest first.

        The answer to "what did this session deliver" that does not depend on a
        client having been connected when it happened, or on the turn that
        carried the manifest still being in the transcript.
        """
        if not conversation:
            return []
        rows = [rec for rec in self._by_token.values() if rec.conversation == conversation]
        rows.sort(key=lambda rec: rec.created_at)
        return rows

    def drop(self, token: str) -> None:
        if self._by_token.pop(token, None) is not None:
            self._save()
