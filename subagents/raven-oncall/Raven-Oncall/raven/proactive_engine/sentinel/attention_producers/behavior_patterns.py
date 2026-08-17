"""``## Cross-project behavior patterns (14d)`` — view over
DailyAnalysisService output."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer,
)

if TYPE_CHECKING:
    from raven.proactive_engine.sentinel.predictor.daily_analysis import (
        DailyAnalysisService,
    )


class BehaviorPatternsProducer(AttentionProducer):
    """Renders the patterns slice of the shared daily-analysis output.

    Groups by ``kind`` (temporal / workflow / topical) with one H3 per
    kind; bullets carry text + supporting projects + confidence.
    """

    SECTION_HEADER = "## Cross-project behavior patterns (14d)"

    def __init__(self, *, analysis: "DailyAnalysisService") -> None:
        self._analysis = analysis

    async def compute_body(self, now: datetime) -> str:
        result = await self._analysis.get(now)
        if result is None or not result.patterns:
            return ""
        by_kind: dict[str, list] = {}
        for p in result.patterns:
            by_kind.setdefault(p.kind, []).append(p)
        kind_order = ("temporal", "workflow", "topical")
        kind_labels = {
            "temporal": "Temporal patterns",
            "workflow": "Workflow patterns",
            "topical": "Topical patterns",
        }
        parts: list[str] = []
        for kind in kind_order:
            entries = by_kind.get(kind, [])
            if not entries:
                continue
            parts.append(f"### {kind_labels[kind]}")
            for p in entries:
                proj_str = ""
                if p.supporting_projects:
                    proj_str = " · projects: " + ", ".join(f"`{pn}`" for pn in p.supporting_projects)
                parts.append(f"- {p.text} [{p.confidence}{proj_str}]")
            parts.append("")
        return "\n".join(parts).rstrip()


__all__ = ["BehaviorPatternsProducer"]
