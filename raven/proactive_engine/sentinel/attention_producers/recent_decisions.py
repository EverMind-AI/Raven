"""``## Recent proactive decisions (14d)`` — dispatched events + outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer,
)
from raven.proactive_engine.sentinel.feedback.tracker import FeedbackSignal

if TYPE_CHECKING:
    from raven.proactive_engine.sentinel.feedback.tracker import (
        NudgeFeedbackTracker,
    )


class RecentProactiveDecisionsProducer(AttentionProducer):
    """One row per ``signal=dispatched`` event in the window. Joined
    accept/dismiss outcome on the same nudge_id is rendered as a trailing
    ``→ accepted`` / ``→ dismissed`` annotation so each decision and its
    eventual stance fit in one line."""

    SECTION_HEADER = "## Recent proactive decisions (14d)"

    def __init__(
        self,
        tracker: "NudgeFeedbackTracker",
        *,
        since_days: int = 14,
        max_rows: int = 50,
    ) -> None:
        self._tracker = tracker
        self._since_days = since_days
        self._max_rows = max_rows

    async def compute_body(self, now: datetime) -> str:
        cutoff = now - timedelta(days=self._since_days)
        outcomes: dict[str, str] = {}
        dispatched: list[dict[str, Any]] = []
        for rec in self._tracker.recent(n=2000):
            ts_str = rec.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            if ts < cutoff:
                continue
            sig = rec.get("signal")
            nid = rec.get("id")
            if not nid:
                continue
            if sig == FeedbackSignal.DISPATCHED.value:
                dispatched.append(rec)
            elif sig in {
                FeedbackSignal.ACCEPTED.value,
                FeedbackSignal.DISMISSED.value,
                FeedbackSignal.IGNORED.value,
            }:
                outcomes[nid] = sig
        if not dispatched:
            return ""
        lines: list[str] = []
        for rec in dispatched[-self._max_rows:]:
            ts_short = str(rec.get("ts", ""))[:16].replace("T", " ")
            details = rec.get("details") or {}
            if not isinstance(details, dict):
                details = {}
            action = rec.get("action") or ""
            outcome = outcomes.get(rec.get("id", ""), "")
            outcome_str = f"  → {outcome}" if outcome else ""
            session = rec.get("session_key") or ""
            if action == "discovery_menu":
                # Discovery menus span 3-4 different topics; no single
                # topic_tag applies. Show option count + target channel
                # instead — that's the useful signal for "did the user
                # pick one of these?" lookback.
                opt_n = details.get("option_count", "?")
                target = session.split(":", 1)[0] if ":" in session else session
                lines.append(
                    f"- [{ts_short}] discovery_menu "
                    f"({opt_n} options → {target}){outcome_str}"
                )
                continue
            topic = details.get("topic_tag") or ""
            prio = rec.get("priority", "?")
            score = rec.get("proactivity_score", 0.0)
            try:
                score_str = f"{float(score):.2f}"
            except (TypeError, ValueError):
                score_str = "?"
            topic_str = f"`{topic}`" if topic else "(untagged)"
            lines.append(
                f"- [{ts_short}] {topic_str} prio={prio} "
                f"score={score_str}{outcome_str}"
            )
        return "\n".join(lines)


__all__ = ["RecentProactiveDecisionsProducer"]
