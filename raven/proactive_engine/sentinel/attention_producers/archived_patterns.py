"""``## Archived patterns`` — routines the user explicitly dismissed."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer,
)

if TYPE_CHECKING:
    from raven.proactive_engine.sentinel.predictor.routine_store import (
        RoutineStore,
    )


class ArchivedPatternsProducer(AttentionProducer):
    """Routines the user explicitly retired (status='retired'). Shown so
    the user can see which patterns sentinel learned but they declined —
    useful to re-confirm one later via the CLI."""

    SECTION_HEADER = "## Archived patterns"

    def __init__(self, routine_store: "RoutineStore") -> None:
        self._store = routine_store

    async def compute_body(self, now: datetime) -> str:
        retired = [
            r for r in self._store.all_routines() if r.status == "retired"
        ]
        if not retired:
            return ""
        retired.sort(key=lambda r: r.dismissed_at_ms or 0, reverse=True)
        lines: list[str] = []
        for r in retired:
            dismissed_str = ""
            if r.dismissed_at_ms:
                dismissed_str = (
                    f" · dismissed "
                    + datetime.fromtimestamp(
                        r.dismissed_at_ms / 1000,
                    ).strftime("%Y-%m-%d")
                )
            lines.append(f"- **{r.id}**: {r.pattern}{dismissed_str}")
        return "\n".join(lines)


__all__ = ["ArchivedPatternsProducer"]
