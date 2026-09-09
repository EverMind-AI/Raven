"""``## Active threads`` — routines user has confirmed."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from raven.proactive_engine.sentinel.attention_producers._base import (
    WEEKDAY,
    AttentionProducer,
)

if TYPE_CHECKING:
    from raven.proactive_engine.sentinel.predictor.routine_store import (
        RoutineStore,
    )
    from raven.proactive_engine.sentinel.types import Routine


def _routine_bullet(r: "Routine") -> str:
    """Shared formatting for routine bullets — used by Active /
    Recently abandoned / Archived producers so the section style stays
    consistent."""
    bits: list[str] = [f"**{r.id}**: {r.pattern}"]
    if r.day_of_week is not None:
        bits.append(WEEKDAY[r.day_of_week % 7])
    if r.time_slot is not None:
        sh, eh = r.time_slot
        bits.append(f"{sh:02d}:00-{eh:02d}:00")
    stats = (
        f"occ {r.occurrence_count}"
        + (f" · weight {r.weight:.2f}" if r.weight else "")
        + (f" · last {r.last_triggered[:10]}" if r.last_triggered else "")
    )
    return f"- {' '.join(bits)} ({stats})"


class ActiveThreadsProducer(AttentionProducer):
    """Confirmed, currently-active routines — what sentinel believes the
    user is presently maintaining as a habit."""

    SECTION_HEADER = "## Active threads"

    def __init__(self, routine_store: "RoutineStore") -> None:
        self._store = routine_store

    async def compute_body(self, now: datetime) -> str:
        routines = [r for r in self._store.all_routines() if r.status == "active" and r.user_confirmed]
        if not routines:
            return ""
        routines.sort(
            key=lambda r: (
                -(r.weight or 0),
                -(r.occurrence_count or 0),
                r.id,
            ),
        )
        return "\n".join(_routine_bullet(r) for r in routines)


__all__ = ["ActiveThreadsProducer", "_routine_bullet"]
