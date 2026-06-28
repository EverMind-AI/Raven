"""``## Project rhythm (last 7 days)`` — per-project cadence summary."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from raven.memory_engine.consolidate.consolidator import _parse_episode_line
from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer,
)

if TYPE_CHECKING:
    from raven.memory_engine.consolidate.consolidator import MemoryStore


_WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class ProjectRhythmProducer(AttentionProducer):
    """Buckets episodes.md entries by ``#project-X`` tag within the window,
    then emits per-project: total count, dominant weekday, peak 3-hour
    band. Cadence summary is heuristic — surfaced as a hint, not a
    strict claim."""

    SECTION_HEADER = "## Project rhythm (last 7 days)"

    def __init__(
        self,
        memory_store: "MemoryStore",
        *,
        since_days: int = 7,
        top_limit: int = 8,
    ) -> None:
        self._store = memory_store
        self._since_days = since_days
        self._top_limit = top_limit

    async def compute_body(self, now: datetime) -> str:
        history_file = self._store.history_file
        if not history_file.exists():
            return ""
        cutoff = now - timedelta(days=self._since_days)
        buckets: dict[str, list[tuple[datetime, int, int]]] = {}
        for line in history_file.read_text(encoding="utf-8").splitlines():
            parsed = _parse_episode_line(line)
            if not parsed:
                continue
            ts, _, tags = parsed
            try:
                dt = datetime.strptime(
                    ts.replace("T", " "), "%Y-%m-%d %H:%M",
                )
            except ValueError:
                continue
            if dt < cutoff:
                continue
            for tag in tags:
                if tag.startswith("project-"):
                    buckets.setdefault(tag, []).append(
                        (dt, dt.weekday(), dt.hour),
                    )
        if not buckets:
            return ""
        ranked = sorted(
            buckets.items(), key=lambda kv: -len(kv[1]),
        )[:self._top_limit]
        lines: list[str] = []
        for tag, entries in ranked:
            count = len(entries)
            weekday_counts: dict[int, int] = {}
            hour_counts: dict[int, int] = {}
            for _, wd, hr in entries:
                weekday_counts[wd] = weekday_counts.get(wd, 0) + 1
                hour_counts[hr] = hour_counts.get(hr, 0) + 1
            dom_wd = max(weekday_counts.items(), key=lambda kv: kv[1])[0]
            best_band_start = max(
                range(0, 22),
                key=lambda h: sum(
                    hour_counts.get(h + i, 0) for i in range(3)
                ),
            )
            band_count = sum(
                hour_counts.get(best_band_start + i, 0) for i in range(3)
            )
            band_str = (
                f"{best_band_start:02d}:00-{best_band_start + 3:02d}:00 "
                f"({band_count})"
            )
            project_name = tag[len("project-"):]
            lines.append(
                f"- **{project_name}** ({count} ep): peak "
                f"{_WEEKDAY[dom_wd]}, hours {band_str}"
            )
        return "\n".join(lines)


__all__ = ["ProjectRhythmProducer"]
