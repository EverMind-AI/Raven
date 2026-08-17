"""``## Pending proposals`` — still-live PendingDecisions."""

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


class PendingProposalsProducer(AttentionProducer):
    """Projects the still-live entries of PendingDecisionStore — menus
    sent to the user that haven't been consumed or expired yet."""

    SECTION_HEADER = "## Pending proposals"

    def __init__(self, pending_store: "PendingDecisionStore") -> None:
        self._store = pending_store

    async def compute_body(self, now: datetime) -> str:
        now_ms = int(now.timestamp() * 1000)
        pending = self._store.all_active(now_ms=now_ms)
        if not pending:
            return ""
        lines: list[str] = []
        for d in pending:
            ts = datetime.fromtimestamp(d.created_at_ms / 1000)
            ts_short = ts.strftime("%Y-%m-%d %H:%M")
            age_min = max(0, int((now_ms - d.created_at_ms) / 60_000))
            stage = "awaiting_confirm" if d.awaiting_confirm else "open"
            intent = d.options[0].title if d.options else "(empty)"
            lines.append(
                f"- `{d.decision_id}` [{stage}] {intent} "
                f"— sent {ts_short} ({age_min}m ago) via {d.channel}:{d.to} "
                f"· {len(d.options)} options"
            )
        return "\n".join(lines)


__all__ = ["PendingProposalsProducer"]
