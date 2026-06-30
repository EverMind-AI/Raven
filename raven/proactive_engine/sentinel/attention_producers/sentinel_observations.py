"""``## Sentinel Observations (auto)`` — diagnostic snapshot of NudgePolicy.

Plugged into the unified ``AttentionProducer`` orchestration so all
attention.md sections share one tick path / lock acquisition. 24h
cooldown + MIN_FEEDBACK_FOR_UPDATE feedback floor gate the work.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer,
)

if TYPE_CHECKING:
    from raven.config.raven import SentinelObservationsConfig
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.proactive_engine.sentinel.feedback.tracker import (
        NudgeFeedbackTracker,
    )
    from raven.proactive_engine.sentinel.trigger_policy.policy import (
        NudgePolicy,
    )

# Single HTML cookie at top of body; matched out-of-section by the
# orchestrator's cooldown gate (this producer reads the existing
# attention.md to find its own cookie).
_LAST_UPDATED_RE = re.compile(r"<!--\s*last_updated=([0-9T:\-+\.]+)\s*-->")


class SentinelObservationsProducer(AttentionProducer):
    """Renders signal counts / topic stats / dismiss hours / adaptive
    tuning state for the user-facing diagnostic section.

    Cooldown gate: skip the work when (a) MIN_FEEDBACK_FOR_UPDATE not met
    or (b) an on-disk ``<!-- last_updated=ISO -->`` cookie is younger
    than COOLDOWN_HOURS. The cookie is the section's first body line.
    """

    SECTION_HEADER = "## Sentinel Observations (auto)"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        feedback: "NudgeFeedbackTracker",
        policy: "NudgePolicy",
        config: "SentinelObservationsConfig | None" = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        # Lazy import — the config import edge would otherwise drag the
        # full pydantic schema into the producer's module-load path.
        if config is None:
            from raven.config.raven import SentinelObservationsConfig

            config = SentinelObservationsConfig()
        self._memory_store = memory_store
        self._feedback = feedback
        self._policy = policy
        self._config = config
        self._now_fn = now_fn or datetime.now

    def should_run(self, now: datetime) -> bool:
        if not self._has_enough_feedback():
            return False
        if self._recently_updated(now):
            return False
        return True

    async def compute_body(self, now: datetime) -> str:
        # ``should_run`` already gated the cooldown + feedback floor; if
        # caller skipped that gate (e.g. forced rebuild), still produce
        # the body. Empty-string return is the no-op signal.
        lines: list[str] = [
            f"<!-- last_updated={now.isoformat(timespec='minutes')} -->",
            "",
        ]

        # Signal counts (7d)
        counts = self._feedback.counts(since_days=7)
        dispatched = counts.get("dispatched", 0)
        accepted = counts.get("accepted", 0)
        dismissed = counts.get("dismissed", 0)
        ignored = counts.get("ignored", 0)
        accept_rate = (accepted / dispatched * 100) if dispatched else 0.0
        lines.append("### Signal counts (last 7 days)")
        lines.append(
            f"- dispatched: {dispatched}, accepted: {accepted} "
            f"({accept_rate:.0f}%), dismissed: {dismissed}, "
            f"ignored: {ignored}"
        )
        lines.append("")

        # Topic stats (7d)
        topic_window_cutoff = now - timedelta(days=7)
        id_to_topic: dict[str, str] = {}
        id_signals: dict[str, set[str]] = {}
        for r in self._feedback.recent(n=2000):
            ts = r.get("ts")
            if not isinstance(ts, str):
                continue
            try:
                t = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if (t.replace(tzinfo=None) if t.tzinfo else t) < topic_window_cutoff:
                continue
            nid = r.get("id")
            sig = r.get("signal")
            if not nid or not sig:
                continue
            if sig == "dispatched":
                tag = (r.get("details") or {}).get("topic_tag")
                if tag:
                    id_to_topic[nid] = tag
            id_signals.setdefault(nid, set()).add(sig)

        topic_stats: dict[str, dict[str, int]] = {}
        for nid, tag in id_to_topic.items():
            signals = id_signals.get(nid, set())
            s = topic_stats.setdefault(
                tag,
                {"dispatched": 0, "accepted": 0, "dismissed": 0},
            )
            s["dispatched"] += 1
            if "accepted" in signals:
                s["accepted"] += 1
            if "dismissed" in signals:
                s["dismissed"] += 1

        lines.append("### Topics fired (last 7 days)")
        if topic_stats:
            ordered = sorted(
                topic_stats.items(),
                key=lambda kv: (-kv[1]["dispatched"], kv[0]),
            )[:10]
            for tag, s in ordered:
                d = s["dispatched"]
                a = s["accepted"]
                x = s["dismissed"]
                rate = (a / d * 100) if d else 0
                hint = ""
                if d >= 3 and x / d >= 0.6:
                    hint = "  ⚠ high-dismiss → de-prioritize"
                elif d >= 3 and a / d >= 0.7:
                    hint = "  ✓ well-received"
                lines.append(f"- `{tag}` × {d} (accept {a}, dismiss {x}, accept_rate {rate:.0f}%){hint}")
        else:
            topic_counts: Counter[str] = Counter()
            for tag, dq in (self._policy._topic_fired_at or {}).items():
                n = sum(1 for t in dq if (now - t) <= timedelta(days=7))
                if n > 0:
                    topic_counts[tag] = n
            if topic_counts:
                for tag, n in topic_counts.most_common(8):
                    lines.append(f"- `{tag}` × {n}  (no feedback joined)")
            else:
                lines.append("- (none in window)")
        lines.append("")

        # Dismiss hour-of-day (7d)
        lines.append("### Dismiss timing pattern (last 7 days)")
        dismiss_hours: Counter[int] = Counter()
        cutoff = now - timedelta(days=7)
        for r in self._feedback.recent(n=500):
            if r.get("signal") != "dismissed":
                continue
            ts = r.get("ts")
            if not isinstance(ts, str):
                continue
            try:
                t = datetime.fromisoformat(ts)
            except ValueError:
                continue
            t_naive = t.replace(tzinfo=None) if t.tzinfo else t
            if t_naive < cutoff:
                continue
            dismiss_hours[t_naive.hour] += 1
        if dismiss_hours:
            for h, n in sorted(dismiss_hours.items()):
                if n >= 1:
                    lines.append(f"- {h:02d}:00-{(h + 1) % 24:02d}:00 dismissed {n} times")
        else:
            lines.append("- (no dismissals in window)")
        lines.append("")

        # Adaptive tuning state
        lines.append("### Adaptive tuning")
        mult = getattr(self._policy, "_hour_quota_multiplier", 1.0)
        if mult < 0.99:
            lines.append(f"- hour_quota_multiplier: {mult:.2f} (tightened due to dismiss rate)")
        else:
            lines.append("- hour_quota_multiplier: 1.00 (no tightening)")
        return "\n".join(lines)

    # ── Internals ───────────────────────────────────────────────────

    def _has_enough_feedback(self) -> bool:
        recent = self._feedback.recent(n=200)
        dispatched_count = sum(1 for r in recent if r.get("signal") == "dispatched")
        return dispatched_count >= self._config.min_feedback

    def _recently_updated(self, now: datetime) -> bool:
        path = self._memory_store.attention_file
        if not path.exists():
            return False
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return False
        # Find OUR cookie — must appear after our section header to avoid
        # picking up a cookie from a future producer that adopts the
        # same pattern.
        idx = content.find(self.SECTION_HEADER)
        if idx == -1:
            return False
        m = _LAST_UPDATED_RE.search(content, idx)
        if not m:
            return False
        try:
            last = datetime.fromisoformat(m.group(1))
        except ValueError:
            return False
        if last.tzinfo is not None:
            last = last.replace(tzinfo=None)
        n = now.replace(tzinfo=None) if now.tzinfo else now
        return (n - last) < timedelta(hours=self._config.cooldown_hours)


__all__ = ["SentinelObservationsProducer"]
