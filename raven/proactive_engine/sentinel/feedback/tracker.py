"""NudgeFeedbackTracker — record dispatched / accepted / dismissed signals.

Every Sentinel executor calls ``tracker.record_dispatched(...)`` after a
successful dispatch. When a downstream signal arrives (user replied to a
nudge, user explicitly dismissed, session eventually produced the action
the nudge suggested), the tracker records it against the original dispatch
event.

Persistence: append-only JSONL at ``{sentinel_dir}/feedback.jsonl``
(default ``~/.raven/sentinel/feedback.jsonl``). The ledger feeds
NudgePolicy's user-level acceptance-rate model, so it lives next to
``state.json`` rather than under the workspace.

Older installs that wrote to ``{workspace}/sentinel_feedback.jsonl`` are
migrated on next sentinel start by
``raven.cli._proactive_stack._migrate_legacy_feedback_log``.

Not a ring buffer: keeps every event. The file grows roughly
(#nudges/day) × (bytes/event) ≈ a few KB/day — fine for months.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from loguru import logger


class FeedbackSignal(str, Enum):
    DISPATCHED = "dispatched"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    IGNORED = "ignored"        # time-based: user didn't engage within window
    NEUTRAL = "neutral"        # user replied but reply carried no clear
                                # accept/dismiss intent (logged only;
                                # excluded from acceptance_rate denominator)


# Dispatch actions that expect user engagement, so prolonged silence on them
# is a soft negative worth recording as IGNORED. spawn / defer are excluded —
# a silent user isn't "ignoring" a backgrounded task or an undelivered defer.
_IGNORE_SWEEP_ACTIONS: frozenset[str] = frozenset({"nudge", "nudge_inject"})

# Weight of a time-based IGNORED relative to an explicit DISMISSED when
# accumulating L5's per-topic hard-cooldown count. Silence is softer evidence
# than an explicit "no", so it needs ~2x the volume to reach the threshold.
_IGNORED_REJECT_WEIGHT: float = 0.5


class NudgeFeedbackTracker:
    """Append-only feedback log with simple acceptance-rate rollups.

    Stateless-ish: ``_recent`` in-memory cache mirrors the tail of the log
    for fast rollups. On startup, ``load()`` rehydrates it from disk.
    """

    def __init__(
        self,
        log_path: Path | str,
        *,
        in_memory_window_days: int = 30,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._in_memory_window = timedelta(days=in_memory_window_days)
        self._now_fn = now_fn or datetime.now
        # Recent events for rollup queries — bounded, NOT authoritative.
        self._recent: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Write

    def record_dispatched(
        self,
        nudge_id: str,
        *,
        action: str,
        session_key: str,
        priority: str = "low",
        proactivity_score: float = 0.0,
        source: str = "planner_tick",
        details: dict | None = None,
    ) -> None:
        """Call after any executor successfully dispatches a nudge."""
        self._emit({
            "id": nudge_id,
            "signal": FeedbackSignal.DISPATCHED.value,
            "action": action,
            "session_key": session_key,
            "priority": priority,
            "proactivity_score": proactivity_score,
            "source": source,
            "details": details or {},
        })

    def record_accepted(self, nudge_id: str, *, context: str | None = None) -> None:
        """User engaged — replied, took the suggested action, clicked, etc."""
        self._emit({
            "id": nudge_id,
            "signal": FeedbackSignal.ACCEPTED.value,
            "context": context,
        })

    def record_dismissed(self, nudge_id: str, *, reason: str | None = None) -> None:
        """User explicitly dismissed. Also tell NudgePolicy to cool down
        that session — caller is responsible for the NudgePolicy call."""
        self._emit({
            "id": nudge_id,
            "signal": FeedbackSignal.DISMISSED.value,
            "reason": reason,
        })

    def record_ignored(self, nudge_id: str, *, window_seconds: int) -> None:
        """No engagement within window — soft negative signal."""
        self._emit({
            "id": nudge_id,
            "signal": FeedbackSignal.IGNORED.value,
            "window_seconds": window_seconds,
        })

    def record_neutral(self, nudge_id: str, *, reason: str | None = None) -> None:
        """User replied but reply carried no clear accept/dismiss intent.

        Logged only — by design NEUTRAL is NOT counted in
        ``acceptance_rate()``'s numerator OR denominator so a string of
        ambiguous replies neither inflates nor deflates the configured
        adaptive-quota signal.
        """
        self._emit({
            "id": nudge_id,
            "signal": FeedbackSignal.NEUTRAL.value,
            "reason": reason,
        })

    def sweep_ignored(
        self, *, window_seconds: int, now: datetime | None = None,
    ) -> int:
        """Record IGNORED for engagement-expecting dispatches gone silent.

        Scans the in-memory log for ``_IGNORE_SWEEP_ACTIONS`` DISPATCHED
        events older than ``window_seconds`` that never reached a terminal
        signal (ACCEPTED / DISMISSED / NEUTRAL / IGNORED) and records IGNORED
        for each. This is the only producer of IGNORED — without it, silence
        reaches the acceptance-rate layers (via the dispatched denominator)
        but stays invisible to L2's per-hour DND and L5's per-topic cooldown,
        which key off explicit reject signals.

        Idempotent: an IGNORED id has a terminal signal and is not re-swept.
        Returns the count of newly-IGNORED ids. Caller drives cadence
        (SentinelRunner tick).
        """
        now = now or self._now_fn()
        cutoff = now - timedelta(seconds=window_seconds)
        dispatched_at: dict[str, datetime] = {}
        terminal: set[str] = set()
        for rec in self._recent:
            ts = self._safe_parse_ts(rec.get("ts", ""))
            if ts is None:
                continue
            nid = rec.get("id")
            if not nid:
                continue
            signal = rec.get("signal")
            if signal == FeedbackSignal.DISPATCHED.value:
                if rec.get("action") in _IGNORE_SWEEP_ACTIONS:
                    dispatched_at[nid] = ts
            elif signal in (
                FeedbackSignal.ACCEPTED.value,
                FeedbackSignal.DISMISSED.value,
                FeedbackSignal.NEUTRAL.value,
                FeedbackSignal.IGNORED.value,
            ):
                terminal.add(nid)
        count = 0
        for nid, ts in dispatched_at.items():
            if nid not in terminal and ts <= cutoff:
                self.record_ignored(nid, window_seconds=window_seconds)
                count += 1
        return count

    # ------------------------------------------------------------------
    # Read / stats

    def load(self) -> None:
        """Rehydrate in-memory recent cache from the JSONL log.

        Best-effort: malformed lines are skipped. Safe to call on startup.
        """
        if not self.log_path.exists():
            return
        cutoff = self._now_fn() - self._in_memory_window
        loaded: list[dict[str, Any]] = []
        try:
            with self.log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("ts")
                    if isinstance(ts, str):
                        try:
                            if datetime.fromisoformat(ts) >= cutoff:
                                loaded.append(rec)
                        except ValueError:
                            pass
        except OSError as exc:
            logger.warning("FeedbackTracker load failed: {}", exc)
            return
        self._recent = loaded

    def acceptance_rate(
        self,
        since_days: int = 7,
        *,
        topic_tag: str | None = None,
        min_volume: int = 5,
    ) -> float | None:
        """Fraction of dispatched nudges that received ACCEPTED within window.

        NEUTRAL signals (user replied but reply carried no clear intent)
        are excluded from BOTH numerator and denominator — they're "no
        signal" by design, so ambiguous replies don't deflate the rate.

        ``topic_tag`` (L3): when set, restrict the rate to only events
        whose DISPATCHED record carried matching details.topic_tag.
        Useful for per-topic acceptance gating — "user rejects exercise
        reminders specifically" surfaces here.

        ``min_volume``: returns None below this floor (benefit of the
        doubt — don't penalize until we have enough signal).

        Returns None below ``min_volume`` *scored* dispatches (scored =
        every in-window DISPATCHED minus those whose reply was NEUTRAL).
        A dispatch with no explicit ACCEPTED — DISMISSED, IGNORED, or
        simply un-acknowledged — counts as a non-acceptance: staying quiet
        is not acceptance, matching NudgePolicy's "no implicit accept".
        """
        cutoff = self._now_fn() - timedelta(days=since_days)
        dispatched_ids: set[str] = set()
        accepted_ids: set[str] = set()
        neutral_ids: set[str] = set()
        # When filtering by topic_tag we need to know which DISPATCHED
        # records carry that tag; build the allowlist in the same pass.
        topic_filter_ids: set[str] | None = (
            set() if topic_tag is not None else None
        )
        for rec in self._recent:
            ts = self._safe_parse_ts(rec.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            signal = rec.get("signal")
            nid = rec.get("id")
            if not nid:
                continue
            if signal == FeedbackSignal.DISPATCHED.value:
                dispatched_ids.add(nid)
                if topic_filter_ids is not None:
                    tag = (rec.get("details") or {}).get("topic_tag")
                    if tag == topic_tag:
                        topic_filter_ids.add(nid)
            elif signal == FeedbackSignal.ACCEPTED.value:
                accepted_ids.add(nid)
            elif signal == FeedbackSignal.NEUTRAL.value:
                neutral_ids.add(nid)
        scored = dispatched_ids - neutral_ids
        if topic_filter_ids is not None:
            scored = scored & topic_filter_ids
        if len(scored) < min_volume:
            return None
        return len(accepted_ids & scored) / len(scored)

    def topic_acceptance_rate(
        self,
        topic_tag: str,
        *,
        since_days: int = 14,
        min_volume: int = 3,
    ) -> float | None:
        """L3: per-topic acceptance rate with a smaller min_volume.

        Thin wrapper over ``acceptance_rate(topic_tag=...)``. Reason for
        the separate entry point: per-topic gates use a lower volume
        threshold (3 vs 5) because individual topics fire less than the
        global stream. Defaults to a 14-day window — topics evolve
        slower than aggregate engagement, so a longer memory is fine.
        """
        if not topic_tag:
            return None
        return self.acceptance_rate(
            since_days=since_days,
            topic_tag=topic_tag,
            min_volume=min_volume,
        )

    def counts(self, since_days: int = 7) -> dict[str, int]:
        """Raw counts per signal within window — useful for dashboards."""
        cutoff = self._now_fn() - timedelta(days=since_days)
        counts: dict[str, int] = {s.value: 0 for s in FeedbackSignal}
        for rec in self._recent:
            ts = self._safe_parse_ts(rec.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            counts[rec.get("signal", "unknown")] = counts.get(rec.get("signal", "unknown"), 0) + 1
        return counts

    def recent(self, n: int = 20) -> list[dict[str, Any]]:
        """Last N events (any signal) from in-memory cache."""
        return self._recent[-n:]

    def by_hour_reject_rate(
        self,
        *,
        since_days: int = 14,
        min_volume: int = 5,
    ) -> dict[int, tuple[float, int]]:
        """L2: per-hour-of-day (0..23) DISMISSED+IGNORED rate.

        Returns ``{hour: (reject_rate, scored_count)}`` for hours where
        scored_count ≥ ``min_volume``. Hours below the floor are omitted
        (benefit of the doubt — don't flag an hour as soft-DND on 1-2
        samples). ``scored_count`` excludes NEUTRAL responses; a later
        ACCEPTED overrides a prior IGNORED/DISMISSED (accept-wins).

        Bucket key is the local-time HOUR of the DISPATCHED record's
        timestamp. Tied with the persona's actual usage rhythm (lunch
        hour, kid pickup, etc.) without needing the persona's static
        DND windows.
        """
        cutoff = self._now_fn() - timedelta(days=since_days)
        # (hour, nid) → 1 for each DISPATCHED in window; needed to join
        # back to the reject/accept signals which don't carry the hour
        # themselves.
        dispatched_hour: dict[str, int] = {}
        accepted_ids: set[str] = set()
        rejected_ids: set[str] = set()
        neutral_ids: set[str] = set()
        for rec in self._recent:
            ts = self._safe_parse_ts(rec.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            signal = rec.get("signal")
            nid = rec.get("id")
            if not nid:
                continue
            if signal == FeedbackSignal.DISPATCHED.value:
                dispatched_hour[nid] = ts.hour
            elif signal == FeedbackSignal.ACCEPTED.value:
                accepted_ids.add(nid)
            elif signal in (FeedbackSignal.DISMISSED.value,
                            FeedbackSignal.IGNORED.value):
                rejected_ids.add(nid)
            elif signal == FeedbackSignal.NEUTRAL.value:
                neutral_ids.add(nid)
        # Aggregate by hour, excluding NEUTRAL from both numerator + denominator.
        from collections import defaultdict
        hour_scored: dict[int, int] = defaultdict(int)
        hour_rejects: dict[int, int] = defaultdict(int)
        for nid, hr in dispatched_hour.items():
            if nid in neutral_ids:
                continue
            # Accept-wins: a later ACCEPTED (e.g. a reply after the sweep
            # marked the nudge IGNORED) overrides the prior reject — scored
            # but not a reject. Mirrors recent_topic_rejects.
            if nid in accepted_ids:
                hour_scored[hr] += 1
            elif nid in rejected_ids:
                hour_scored[hr] += 1
                hour_rejects[hr] += 1
        out: dict[int, tuple[float, int]] = {}
        for hr, n in hour_scored.items():
            if n < min_volume:
                continue
            out[hr] = (hour_rejects.get(hr, 0) / n, n)
        return out

    def recent_topic_rejects(
        self, topic_tag: str, *, since_seconds: int = 86400,
    ) -> float:
        """Weighted reject count for ``topic_tag`` in the last ``since_seconds``.

        Explicit DISMISSED counts 1.0; time-based IGNORED (pure silence)
        counts ``_IGNORED_REJECT_WEIGHT`` (0.5) — softer evidence, so silence
        needs ~2x the volume of explicit dismissals to reach L5's threshold.
        An id later ACCEPTED is not counted (engagement wins over a prior
        IGNORED). Joins each reject back to its DISPATCHED event via ``id``
        (topic_tag lives on the DISPATCHED record's ``details.topic_tag``).

        Used by L5: weight ≥ 3 → ``NudgePolicy.check`` denies that topic for
        24h, even for high_priority. Catches stubborn patterns adaptive
        tuning's global rate misses (e.g. "medication" accepted 90%,
        "exercise" rejected always).

        Reachability note: pure silence rarely trips L5 on its own — at 0.5
        each it takes ~6 IGNORED on one topic in 24h, usually more than that
        topic's share of the daily quota. So IGNORED's main back-off effect is
        via L2 (per-hour DND aggregates across topics, so samples accumulate);
        on L5 it mostly matters mixed with explicit dismissals (e.g. 2 dismiss
        + 2 ignored = 3.0). This is intended — silence is weak evidence.
        """
        if not topic_tag:
            return 0.0
        cutoff = self._now_fn() - timedelta(seconds=since_seconds)
        topic_for_id: dict[str, str] = {}
        accepted_ids: set[str] = set()
        reject_weight: dict[str, float] = {}
        for rec in self._recent:
            ts = self._safe_parse_ts(rec.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            nid = rec.get("id")
            if not nid:
                continue
            signal = rec.get("signal")
            if signal == FeedbackSignal.DISPATCHED.value:
                tag = (rec.get("details") or {}).get("topic_tag")
                if isinstance(tag, str):
                    topic_for_id[nid] = tag
            elif signal == FeedbackSignal.ACCEPTED.value:
                accepted_ids.add(nid)
            elif signal == FeedbackSignal.DISMISSED.value:
                reject_weight[nid] = 1.0
            elif signal == FeedbackSignal.IGNORED.value:
                reject_weight[nid] = max(
                    reject_weight.get(nid, 0.0), _IGNORED_REJECT_WEIGHT,
                )
        return sum(
            w for nid, w in reject_weight.items()
            if topic_for_id.get(nid) == topic_tag and nid not in accepted_ids
        )

    def cleanup_older_than(self, days: int = 30) -> dict[str, int]:
        """Rewrite the log keeping only events within the last ``days``.

        ``apply_adaptive_tuning`` uses ``acceptance_rate(since_days=7)``
        so anything past that window only adds disk + parse cost.

        Atomic via temp file + rename so a crash mid-rewrite leaves the
        old log intact. Updates the in-memory cache to match. Malformed
        / undated lines are treated as drops.

        Returns a counts dict: ``{"kept": int, "dropped": int}``.
        Returns ``{"kept": 0, "dropped": 0}`` on no-op (file missing).

        Call cadence: daily — wire from SentinelRunner. Cheap enough to
        run on every tick but no benefit beyond once-per-day.
        """
        if not self.log_path.exists():
            return {"kept": 0, "dropped": 0}

        cutoff = self._now_fn() - timedelta(days=days)
        tmp_path = self.log_path.with_suffix(self.log_path.suffix + ".cleanup")
        kept = 0
        dropped = 0
        try:
            with self.log_path.open("r", encoding="utf-8") as src, \
                 tmp_path.open("w", encoding="utf-8") as dst:
                for line in src:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        rec = json.loads(s)
                    except json.JSONDecodeError:
                        dropped += 1
                        continue
                    ts = rec.get("ts")
                    if not isinstance(ts, str):
                        dropped += 1
                        continue
                    try:
                        rec_dt = datetime.fromisoformat(ts)
                    except ValueError:
                        dropped += 1
                        continue
                    if rec_dt >= cutoff:
                        # Preserve original line bytes (already JSON-valid)
                        # to avoid float-roundtrip drift.
                        dst.write(s + "\n")
                        kept += 1
                    else:
                        dropped += 1
            tmp_path.replace(self.log_path)
        except OSError as exc:
            # Best-effort: nuke the partial temp, leave original alone.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            logger.warning("FeedbackTracker cleanup failed: {}", exc)
            return {"kept": 0, "dropped": 0, "error": 1}

        # Mirror the disk filter in the in-memory cache so subsequent
        # acceptance_rate() / counts() don't see ghosts.
        self._recent = [
            r for r in self._recent
            if isinstance(r.get("ts"), str)
            and self._safe_parse_ts(r["ts"]) is not None
            and self._safe_parse_ts(r["ts"]) >= cutoff
        ]
        return {"kept": kept, "dropped": dropped}

    @staticmethod
    def _safe_parse_ts(ts: str) -> datetime | None:
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Internals

    def _emit(self, payload: dict[str, Any]) -> None:
        record = {"ts": self._now_fn().isoformat(), **payload}
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("FeedbackTracker emit failed: {}", exc)
            return
        self._recent.append(record)
        # Trim cache if it grows unboundedly beyond the window.
        if len(self._recent) > 10_000:
            cutoff = self._now_fn() - self._in_memory_window
            self._recent = [
                r for r in self._recent
                if r.get("ts") and datetime.fromisoformat(r["ts"]) >= cutoff
            ]


def new_nudge_id() -> str:
    """Utility — generate an ID to correlate dispatched→accepted/dismissed."""
    return uuid.uuid4().hex[:16]


__all__ = ["NudgeFeedbackTracker", "FeedbackSignal", "new_nudge_id"]
