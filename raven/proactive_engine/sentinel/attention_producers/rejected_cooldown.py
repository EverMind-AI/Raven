"""``## Rejected proposals (cooldown)`` — consumed-without-pick within window."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer,
)

if TYPE_CHECKING:
    from raven.proactive_engine.sentinel.executor.pending_decision import (
        PendingDecisionStore,
    )


class RejectedCooldownProducer(AttentionProducer):
    """PendingDecisions the user consumed without picking (rejected) within
    the ``cooldown_hours`` window. Per-decision granularity — no
    topic-level aggregation."""

    SECTION_HEADER = "## Rejected proposals (cooldown)"

    def __init__(
        self,
        pending_store: "PendingDecisionStore",
        *,
        cooldown_hours: float = 24.0,
    ) -> None:
        self._store = pending_store
        self._cooldown_hours = cooldown_hours

    async def compute_body(self, now: datetime) -> str:
        now_ms = int(now.timestamp() * 1000)
        cutoff_ms = now_ms - int(self._cooldown_hours * 3_600_000)
        decisions = self._store.all_decisions(include_consumed=True)
        rejected = [
            d
            for d in decisions
            if d.consumed
            and d.picked_option_id is None
            and d.consumed_at_ms is not None
            and d.consumed_at_ms >= cutoff_ms
        ]
        if not rejected:
            return ""
        rejected.sort(key=lambda d: d.consumed_at_ms or 0, reverse=True)
        cooldown_ms = int(self._cooldown_hours * 3_600_000)
        lines: list[str] = []
        for d in rejected:
            ts = datetime.fromtimestamp((d.consumed_at_ms or 0) / 1000)
            ts_short = ts.strftime("%Y-%m-%d %H:%M")
            expires = datetime.fromtimestamp(
                ((d.consumed_at_ms or 0) + cooldown_ms) / 1000,
            ).strftime("%Y-%m-%d %H:%M")
            intent = d.options[0].title if d.options else "(empty)"
            lines.append(f"- `{d.decision_id}` {intent} — rejected {ts_short}, cooldown until {expires}")
        return "\n".join(lines)


__all__ = ["RejectedCooldownProducer"]
