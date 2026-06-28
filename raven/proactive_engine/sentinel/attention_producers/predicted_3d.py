"""``## Predicted next 3 days`` — view over DailyAnalysisService output."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer, WEEKDAY,
)

if TYPE_CHECKING:
    from raven.proactive_engine.sentinel.predictor.daily_analysis import (
        DailyAnalysisService,
    )


class Predicted3DProducer(AttentionProducer):
    """Renders the predictions slice of the shared daily-analysis output.

    Groups by ``date`` (H3 per day) with bullets per prediction. Days
    earlier than ``now.date()`` are dropped — the LLM occasionally
    emits past-dated predictions and they don't belong in a forecast.
    """

    SECTION_HEADER = "## Predicted next 3 days"

    def __init__(self, *, analysis: "DailyAnalysisService") -> None:
        self._analysis = analysis

    async def compute_body(self, now: datetime) -> str:
        result = await self._analysis.get(now)
        if result is None or not result.predictions:
            return ""
        today = now.date()
        by_day: dict[str, list] = {}
        for pred in result.predictions:
            try:
                pred_date = date.fromisoformat(pred.date)
            except ValueError:
                continue
            if pred_date < today:
                continue
            by_day.setdefault(pred.date, []).append(pred)
        if not by_day:
            return ""
        parts: list[str] = []
        for day in sorted(by_day):
            try:
                d = date.fromisoformat(day)
                weekday = WEEKDAY[d.weekday()]
                header = f"### {day} ({weekday})"
            except ValueError:
                header = f"### {day}"
            parts.append(header)
            for pred in by_day[day]:
                parts.append(
                    f"- {pred.text} "
                    f"[{pred.confidence} — {pred.basis}]"
                    if pred.basis else
                    f"- {pred.text} [{pred.confidence}]"
                )
            parts.append("")
        return "\n".join(parts).rstrip()


__all__ = ["Predicted3DProducer"]
