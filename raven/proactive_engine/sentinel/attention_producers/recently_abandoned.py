"""``## Recently abandoned, worth resuming`` — silent 7-30d active routines."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer,
)
from raven.proactive_engine.sentinel.attention_producers.active_threads import (
    _routine_bullet,
)

if TYPE_CHECKING:
    from raven.proactive_engine.sentinel.predictor.routine_store import (
        RoutineStore,
    )


class RecentlyAbandonedProducer(AttentionProducer):
    """Active routines whose ``last_triggered`` is between ``silence_days``
    and ``abandon_days`` old — recently silent but not yet stale. Past
    ``abandon_days`` they drop out (the Archived producer doesn't cover
    them, but Sentinel would emit a resume nudge before then anyway)."""

    SECTION_HEADER = "## Recently abandoned, worth resuming"

    def __init__(
        self,
        routine_store: "RoutineStore",
        *,
        silence_days: int = 7,
        abandon_days: int = 30,
    ) -> None:
        if abandon_days <= silence_days:
            raise ValueError(
                f"abandon_days ({abandon_days}) must be > silence_days "
                f"({silence_days})",
            )
        self._store = routine_store
        self._silence_ms = silence_days * 86_400_000
        self._abandon_ms = abandon_days * 86_400_000

    async def compute_body(self, now: datetime) -> str:
        now_ms = int(now.timestamp() * 1000)
        candidates: list = []
        for r in self._store.all_routines():
            if r.status not in {"active", "paused"}:
                continue
            if not r.last_triggered:
                continue
            try:
                last_ms = int(datetime.fromisoformat(
                    r.last_triggered.replace("Z", "+00:00"),
                ).timestamp() * 1000)
            except (ValueError, AttributeError):
                continue
            age = now_ms - last_ms
            if age < self._silence_ms or age >= self._abandon_ms:
                continue
            candidates.append(r)
        if not candidates:
            return ""
        candidates.sort(key=lambda r: r.last_triggered or "", reverse=True)
        return "\n".join(_routine_bullet(r) for r in candidates)


__all__ = ["RecentlyAbandonedProducer"]
