"""Unit tests for NudgeFeedbackTracker.cleanup_older_than — the daily
retention pass that keeps the append-only JSONL bounded so the 7-day
adaptive tuner doesn't drag old events through every startup.

Verifies: keep-fresh, drop-stale, malformed-line handling, in-memory
cache mirroring, no-op on missing file, and the post-cleanup
acceptance_rate matches the filtered events."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from raven.proactive_engine.sentinel.feedback.tracker import NudgeFeedbackTracker


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance_days(self, n: int) -> None:
        self.t = self.t + timedelta(days=n)


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 5, 15, 12, 0))


@pytest.fixture
def tracker(tmp_path: Path, clock: Clock) -> NudgeFeedbackTracker:
    return NudgeFeedbackTracker(tmp_path / "feedback.jsonl", now_fn=clock)


def _seed_at(
    tracker: NudgeFeedbackTracker, clock: Clock, days_ago: int, nudge_id: str, signal: str = "dispatched"
) -> None:
    """Record one event N days before the tracker's "now"."""
    save = clock.t
    clock.t = save - timedelta(days=days_ago)
    if signal == "dispatched":
        tracker.record_dispatched(nudge_id, action="nudge", session_key="default", priority="medium")
    elif signal == "accepted":
        tracker.record_accepted(nudge_id)
    elif signal == "dismissed":
        tracker.record_dismissed(nudge_id)
    clock.t = save


def test_cleanup_keeps_fresh_events(tracker, clock):
    """Events within the retention window must survive."""
    for i in range(5):
        _seed_at(tracker, clock, days_ago=3, nudge_id=f"n-{i}")
    res = tracker.cleanup_older_than(days=30)
    assert res == {"kept": 5, "dropped": 0}
    assert len(tracker._recent) == 5


def test_cleanup_drops_stale_events(tracker, clock):
    """Events older than the window must be removed."""
    for i in range(7):
        _seed_at(tracker, clock, days_ago=45, nudge_id=f"old-{i}")
    res = tracker.cleanup_older_than(days=30)
    assert res == {"kept": 0, "dropped": 7}
    assert len(tracker._recent) == 0
    # Disk should be a (possibly empty) file, not an unread error path.
    assert tracker.log_path.exists()
    assert tracker.log_path.read_text() == ""


def test_cleanup_mixed_window(tracker, clock):
    """Fresh + stale mixed: keep fresh, drop stale, atomically rewrite."""
    for i in range(3):
        _seed_at(tracker, clock, days_ago=2, nudge_id=f"fresh-{i}")
    for i in range(4):
        _seed_at(tracker, clock, days_ago=60, nudge_id=f"stale-{i}")
    res = tracker.cleanup_older_than(days=30)
    assert res == {"kept": 3, "dropped": 4}
    # Verify disk content matches what we kept
    remaining_ids = []
    for line in tracker.log_path.read_text().splitlines():
        if line.strip():
            remaining_ids.append(json.loads(line).get("id"))
    assert sorted(remaining_ids) == ["fresh-0", "fresh-1", "fresh-2"]


def test_cleanup_handles_malformed_lines(tracker, clock):
    """Lines that aren't valid JSON or lack ``ts`` are dropped without crashing."""
    _seed_at(tracker, clock, days_ago=1, nudge_id="good")
    # Append garbage directly so we exercise the parse path
    with tracker.log_path.open("a", encoding="utf-8") as f:
        f.write("not-json\n")
        f.write('{"id": "no-ts"}\n')  # no ts
        f.write('{"id": "bad-ts", "ts": "garbage"}\n')  # unparseable ts
    res = tracker.cleanup_older_than(days=30)
    # 1 kept, 3 dropped (the malformed ones)
    assert res["kept"] == 1
    assert res["dropped"] == 3


def test_cleanup_noop_when_log_missing(tmp_path: Path, clock: Clock):
    """No-file path returns zero-counts without raising."""
    t = NudgeFeedbackTracker(tmp_path / "nonexistent.jsonl", now_fn=clock)
    res = t.cleanup_older_than(days=30)
    assert res == {"kept": 0, "dropped": 0}


def test_acceptance_rate_reflects_post_cleanup(tracker, clock):
    """After cleanup, acceptance_rate uses only the filtered events —
    so the adaptive tuner can't see ghost dispatches."""
    # 5 dispatched + 5 accepted recently → 100% acceptance
    for i in range(5):
        _seed_at(tracker, clock, days_ago=1, nudge_id=f"recent-{i}", signal="dispatched")
        _seed_at(tracker, clock, days_ago=1, nudge_id=f"recent-{i}", signal="accepted")
    # 20 stale dispatched + 0 accepted → would drag rate down if not pruned
    for i in range(20):
        _seed_at(tracker, clock, days_ago=90, nudge_id=f"stale-{i}", signal="dispatched")

    # Before cleanup: 25 dispatched in last 365d window, rate would be 5/25 = 0.2
    # But acceptance_rate uses 7d window by default — stale (90d ago) already
    # excluded; rate should be 1.0 on the 5 recent events.
    pre = tracker.acceptance_rate(since_days=7)
    assert pre == 1.0

    # Now run a 30d cleanup; the 20 stale events get dropped from disk + cache.
    res = tracker.cleanup_older_than(days=30)
    assert res == {"kept": 10, "dropped": 20}  # 5 dispatched + 5 accepted kept

    # acceptance_rate is unchanged — it was already filtered by 7d window.
    post = tracker.acceptance_rate(since_days=7)
    assert post == 1.0
    # But the 90d-since window now shows the cleanup effect.
    long_rate = tracker.acceptance_rate(since_days=365)
    assert long_rate == 1.0  # 5/5 = 1.0, no longer 5/25


def test_cleanup_idempotent(tracker, clock):
    """Running cleanup twice in a row is safe — second call is a no-op
    on the kept tail."""
    for i in range(3):
        _seed_at(tracker, clock, days_ago=2, nudge_id=f"n-{i}")
    for i in range(2):
        _seed_at(tracker, clock, days_ago=60, nudge_id=f"old-{i}")
    r1 = tracker.cleanup_older_than(days=30)
    assert r1 == {"kept": 3, "dropped": 2}
    r2 = tracker.cleanup_older_than(days=30)
    assert r2 == {"kept": 3, "dropped": 0}
