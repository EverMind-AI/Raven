"""Per-session watch-cursor store for the Mochat channel.

Tracks the highest seen cursor per session and persists the map to a JSON file
with a debounced background save, so a restart resumes watching where it left
off instead of replaying or skipping history. Self-contained: owns its save
task and never calls back into the channel.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_SAVE_DEBOUNCE_S = 0.5


class CursorStore:
    """Monotonic per-session cursors + debounced JSON persistence.

    ``mark`` keeps the maximum cursor per session (negative or stale values
    are ignored) and schedules a debounced save; ``close`` cancels a pending
    save and persists immediately (the stop path).
    """

    def __init__(self, state_dir: Path, *, debounce_s: float = _SAVE_DEBOUNCE_S):
        self._dir = state_dir
        self._path = state_dir / "session_cursors.json"
        self._cursors: dict[str, int] = {}
        self._save_task: asyncio.Task | None = None
        self._debounce_s = debounce_s

    def get(self, session_id: str) -> int:
        return self._cursors.get(session_id, 0)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._cursors

    def snapshot(self) -> dict[str, int]:
        return dict(self._cursors)

    def mark(self, session_id: str, cursor: int) -> None:
        if cursor < 0 or cursor < self._cursors.get(session_id, 0):
            return
        self._cursors[session_id] = cursor
        if not self._save_task or self._save_task.done():
            self._save_task = asyncio.create_task(self._save_debounced())

    async def _save_debounced(self) -> None:
        await asyncio.sleep(self._debounce_s)
        await self.save()

    async def load(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except Exception as e:
            logger.warning("Failed to read Mochat cursor file: {}", e)
            return
        cursors = data.get("cursors") if isinstance(data, dict) else None
        if not isinstance(cursors, dict):
            return
        for sid, cur in cursors.items():
            if isinstance(sid, str) and isinstance(cur, int) and cur >= 0:
                self._cursors[sid] = cur

    async def save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({
                "schemaVersion": 1, "updatedAt": datetime.now(timezone.utc).isoformat(),
                "cursors": self._cursors,
            }, ensure_ascii=False, indent=2) + "\n", "utf-8")
        except Exception as e:
            logger.warning("Failed to save Mochat cursor file: {}", e)

    async def close(self) -> None:
        if self._save_task:
            self._save_task.cancel()
            self._save_task = None
        await self.save()
