"""Unit tests for NudgeFeedbackTracker.

Covers: append-only JSONL, in-memory rollup, acceptance_rate, counts(),
load() on startup, and malformed-line resilience.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from raven.proactive_engine.sentinel.feedback.tracker import (
    NudgeFeedbackTracker,
    new_nudge_id,
)


class Clock:
    def __init__(self, t0: datetime):
        self.t = t0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float):
        self.t = self.t + timedelta(seconds=seconds)


def _tracker(path, clock=None) -> NudgeFeedbackTracker:
    return NudgeFeedbackTracker(path, now_fn=clock or (lambda: datetime(2026, 4, 21, 14, 0, 0)))


def test_record_dispatched_writes_jsonl(tmp_path):
    log = tmp_path / "fb.jsonl"
    tr = _tracker(log)
    tr.record_dispatched("n1", action="nudge", session_key="cli:direct", priority="low", proactivity_score=0.7)
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["id"] == "n1"
    assert rec["signal"] == "dispatched"
    assert rec["action"] == "nudge"
    assert rec["priority"] == "low"
    assert rec["proactivity_score"] == 0.7


def test_record_accepted_and_dismissed(tmp_path):
    log = tmp_path / "fb.jsonl"
    tr = _tracker(log)
    tr.record_dispatched("n1", action="nudge", session_key="s1")
    tr.record_accepted("n1", context="user replied")
    tr.record_dismissed("n2", reason="not helpful")
    signals = [json.loads(l)["signal"] for l in log.read_text().splitlines()]
    assert signals == ["dispatched", "accepted", "dismissed"]


def test_acceptance_rate_needs_minimum_volume(tmp_path):
    log = tmp_path / "fb.jsonl"
    tr = _tracker(log)
    # Only 3 dispatched — below min 5 → None.
    for i in range(3):
        tr.record_dispatched(f"n{i}", action="nudge", session_key="s")
        tr.record_accepted(f"n{i}")
    assert tr.acceptance_rate() is None


def test_acceptance_rate_basic(tmp_path):
    log = tmp_path / "fb.jsonl"
    tr = _tracker(log)
    for i in range(10):
        tr.record_dispatched(f"n{i}", action="nudge", session_key="s")
    for i in range(6):
        tr.record_accepted(f"n{i}")
    rate = tr.acceptance_rate()
    assert rate is not None
    assert abs(rate - 0.6) < 1e-6


def test_counts_in_window(tmp_path):
    log = tmp_path / "fb.jsonl"
    tr = _tracker(log)
    tr.record_dispatched("n1", action="nudge", session_key="s")
    tr.record_dispatched("n2", action="nudge_inject", session_key="s")
    tr.record_accepted("n1")
    tr.record_dismissed("n2")
    tr.record_ignored("n3", window_seconds=3600)
    tr.record_neutral("n4", reason="no_llm_classification")
    c = tr.counts()
    assert c["dispatched"] == 2
    assert c["accepted"] == 1
    assert c["dismissed"] == 1
    assert c["ignored"] == 1
    assert c["neutral"] == 1


def test_acceptance_rate_excludes_neutral_from_denominator(tmp_path):
    """NEUTRAL is "no signal" by design — a dispatch that receives
    NEUTRAL feedback must NOT drag the acceptance_rate down (nor up),
    otherwise a stream of ambiguous replies would tighten the adaptive
    quota the same way the legacy "non-/dismiss => ACCEPTED" path used
    to loosen it."""
    log = tmp_path / "fb.jsonl"
    tr = _tracker(log)
    for i in range(10):
        tr.record_dispatched(f"n{i}", action="nudge", session_key="s")
    # 5 accepted, 2 dismissed, 3 neutral.
    for i in range(5):
        tr.record_accepted(f"n{i}")
    for i in range(5, 7):
        tr.record_dismissed(f"n{i}")
    for i in range(7, 10):
        tr.record_neutral(f"n{i}")
    rate = tr.acceptance_rate()
    # Denominator excludes the 3 neutrals → 5 / (10 - 3) = 0.714…
    assert rate is not None
    assert abs(rate - (5 / 7)) < 1e-6


def test_load_rehydrates_from_disk(tmp_path):
    log = tmp_path / "fb.jsonl"
    tr1 = _tracker(log)
    for i in range(6):
        tr1.record_dispatched(f"n{i}", action="nudge", session_key="s")
        tr1.record_accepted(f"n{i}")
    # New tracker; empty in-memory; load from disk.
    tr2 = _tracker(log)
    assert tr2.acceptance_rate() is None  # cache empty pre-load
    tr2.load()
    rate = tr2.acceptance_rate()
    assert rate is not None
    assert abs(rate - 1.0) < 1e-6


def test_load_skips_malformed_lines(tmp_path):
    log = tmp_path / "fb.jsonl"
    log.write_text(
        "not json\n" + json.dumps({"ts": "2026-04-21T10:00:00", "id": "x", "signal": "dispatched"}) + "\n" + "{bogus}\n"
    )
    tr = _tracker(log)
    tr.load()  # should not raise
    assert len(tr.recent()) == 1


def test_window_excludes_old_records(tmp_path):
    log = tmp_path / "fb.jsonl"
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = NudgeFeedbackTracker(log, now_fn=clock, in_memory_window_days=30)
    # 5 old records
    for i in range(5):
        tr.record_dispatched(f"old{i}", action="nudge", session_key="s")
    # Advance time 60 days
    clock.advance(60 * 86400)
    # 6 fresh records
    for i in range(6):
        tr.record_dispatched(f"new{i}", action="nudge", session_key="s")
    for i in range(4):
        tr.record_accepted(f"new{i}")
    # acceptance rate in last 7 days uses only fresh.
    rate = tr.acceptance_rate(since_days=7)
    assert rate is not None
    assert abs(rate - 4 / 6) < 1e-6


def test_recent_returns_last_n(tmp_path):
    log = tmp_path / "fb.jsonl"
    tr = _tracker(log)
    for i in range(30):
        tr.record_dispatched(f"n{i}", action="nudge", session_key="s")
    assert len(tr.recent(10)) == 10
    assert len(tr.recent(5)) == 5


def test_new_nudge_id_unique():
    ids = {new_nudge_id() for _ in range(100)}
    assert len(ids) == 100


# ---------------------------------------------------------------------------
# L5: recent_topic_rejects — counts DISMISSED + IGNORED for a given topic_tag


def test_recent_topic_rejects_counts_dismissed(tmp_path):
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    for i in range(3):
        nid = f"n{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s", details={"topic_tag": "medication"})
        tr.record_dismissed(nid, reason="too noisy")
    assert tr.recent_topic_rejects("medication") == 3


def test_recent_topic_rejects_ignored_weighted_half(tmp_path):
    # IGNORED is softer than DISMISSED — counts 0.5 each toward L5.
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    for i in range(2):
        nid = f"n{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s", details={"topic_tag": "exercise"})
        tr.record_ignored(nid, window_seconds=600)
    assert tr.recent_topic_rejects("exercise") == 1.0


def test_recent_topic_rejects_mixes_dismissed_and_ignored(tmp_path):
    # 3 dismiss (3.0) + 2 ignore (1.0) = 4.0 weighted.
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    for i in range(3):
        nid = f"d{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s", details={"topic_tag": "exercise"})
        tr.record_dismissed(nid)
    for i in range(2):
        nid = f"g{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s", details={"topic_tag": "exercise"})
        tr.record_ignored(nid, window_seconds=600)
    assert tr.recent_topic_rejects("exercise") == 4.0


def test_recent_topic_rejects_later_accept_wins(tmp_path):
    # A nudge IGNORED by the sweep but later engaged is not a reject.
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    tr.record_dispatched("n1", action="nudge", session_key="s", details={"topic_tag": "exercise"})
    tr.record_ignored("n1", window_seconds=600)
    tr.record_accepted("n1")
    assert tr.recent_topic_rejects("exercise") == 0.0


# sweep_ignored — the producer of IGNORED for long-silent nudges


def test_sweep_ignored_marks_silent_after_window(tmp_path):
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    tr.record_dispatched("n1", action="nudge", session_key="s", details={"topic_tag": "t"})
    clock.advance(21601)  # > 6h
    assert tr.sweep_ignored(window_seconds=21600) == 1
    assert tr.counts()["ignored"] == 1


def test_sweep_ignored_skips_recent(tmp_path):
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    tr.record_dispatched("n1", action="nudge", session_key="s")
    clock.advance(3600)  # 1h < 6h window
    assert tr.sweep_ignored(window_seconds=21600) == 0


def test_sweep_ignored_skips_outcomed(tmp_path):
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    tr.record_dispatched("acc", action="nudge", session_key="s")
    tr.record_accepted("acc")
    tr.record_dispatched("dis", action="nudge", session_key="s")
    tr.record_dismissed("dis")
    clock.advance(21601)
    assert tr.sweep_ignored(window_seconds=21600) == 0


def test_sweep_ignored_skips_non_nudge_actions(tmp_path):
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    tr.record_dispatched("sp", action="spawn_agent", session_key="s")
    tr.record_dispatched("df", action="nudge_defer", session_key="s")
    clock.advance(21601)
    assert tr.sweep_ignored(window_seconds=21600) == 0


def test_sweep_ignored_idempotent(tmp_path):
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    tr.record_dispatched("n1", action="nudge", session_key="s")
    clock.advance(21601)
    assert tr.sweep_ignored(window_seconds=21600) == 1
    assert tr.sweep_ignored(window_seconds=21600) == 0  # already IGNORED


def test_recent_topic_rejects_separates_topics(tmp_path):
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    tr.record_dispatched("n1", action="nudge", session_key="s", details={"topic_tag": "medication"})
    tr.record_dismissed("n1")
    tr.record_dispatched("n2", action="nudge", session_key="s", details={"topic_tag": "exercise"})
    tr.record_dismissed("n2")
    assert tr.recent_topic_rejects("medication") == 1
    assert tr.recent_topic_rejects("exercise") == 1
    assert tr.recent_topic_rejects("nutrition") == 0


def test_recent_topic_rejects_window_excludes_old(tmp_path):
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    # Two old rejects (>24h ago) — should not count.
    tr.record_dispatched("old1", action="nudge", session_key="s", details={"topic_tag": "medication"})
    tr.record_dismissed("old1")
    clock.advance(86401)  # 24h + 1s later
    tr.record_dispatched("new1", action="nudge", session_key="s", details={"topic_tag": "medication"})
    tr.record_dismissed("new1")
    assert tr.recent_topic_rejects("medication") == 1


def test_recent_topic_rejects_accepted_not_counted(tmp_path):
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    tr.record_dispatched("n1", action="nudge", session_key="s", details={"topic_tag": "med"})
    tr.record_accepted("n1")
    assert tr.recent_topic_rejects("med") == 0


def test_recent_topic_rejects_empty_tag_returns_zero(tmp_path):
    tr = _tracker(tmp_path / "fb.jsonl")
    assert tr.recent_topic_rejects("") == 0


def test_recent_topic_rejects_no_topic_in_dispatch_ignored(tmp_path):
    """DISPATCHED without topic_tag in details → won't match any topic."""
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    tr.record_dispatched("n1", action="nudge", session_key="s")  # no topic
    tr.record_dismissed("n1")
    assert tr.recent_topic_rejects("medication") == 0


# ---------------------------------------------------------------------------
# L3: acceptance_rate filtered by topic + topic_acceptance_rate helper


def test_acceptance_rate_filtered_by_topic(tmp_path):
    """Filter restricts numerator + denominator to one topic."""
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    # 5 medication dispatches: 4 accepted, 1 dismissed → 80%
    for i in range(5):
        nid = f"med{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s", details={"topic_tag": "medication"})
        if i < 4:
            tr.record_accepted(nid)
        else:
            tr.record_dismissed(nid)
    # 5 exercise dispatches: 1 accepted, 4 dismissed → 20%
    for i in range(5):
        nid = f"ex{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s", details={"topic_tag": "exercise"})
        if i < 1:
            tr.record_accepted(nid)
        else:
            tr.record_dismissed(nid)
    assert tr.acceptance_rate(topic_tag="medication") == pytest.approx(0.8)
    assert tr.acceptance_rate(topic_tag="exercise") == pytest.approx(0.2)


def test_topic_acceptance_rate_below_min_volume_returns_none(tmp_path):
    """Topic helper uses min_volume=3 — 2 events is not enough."""
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    for i in range(2):
        nid = f"n{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s", details={"topic_tag": "exercise"})
        tr.record_dismissed(nid)
    assert tr.topic_acceptance_rate("exercise") is None


def test_topic_acceptance_rate_at_min_volume_returns_value(tmp_path):
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    for i in range(3):
        nid = f"n{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s", details={"topic_tag": "exercise"})
        tr.record_dismissed(nid)
    assert tr.topic_acceptance_rate("exercise") == pytest.approx(0.0)


def test_topic_acceptance_rate_neutral_excluded(tmp_path):
    """NEUTRAL signals don't count toward denominator (consistent with global rate)."""
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    for i in range(3):
        nid = f"acc{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s", details={"topic_tag": "med"})
        tr.record_accepted(nid)
    # 2 NEUTRAL — should be excluded
    for i in range(2):
        nid = f"neu{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s", details={"topic_tag": "med"})
        tr.record_neutral(nid, reason="ambiguous")
    # Rate = 3 accepted / 3 scored = 1.0 (not 3/5)
    assert tr.topic_acceptance_rate("med") == pytest.approx(1.0)


def test_topic_acceptance_rate_empty_topic_returns_none(tmp_path):
    tr = _tracker(tmp_path / "fb.jsonl")
    assert tr.topic_acceptance_rate("") is None


# ---------------------------------------------------------------------------
# L2: by_hour_reject_rate — per-hour-of-day rolling rate


def test_by_hour_reject_rate_groups_correctly(tmp_path):
    """Each DISPATCHED's timestamp hour is the bucket; rejects on that
    bucket increment its reject count."""
    clock = Clock(datetime(2026, 4, 21, 12, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    # 5 events at 12:xx — 3 dismissed, 2 accepted → 60% reject
    for i in range(5):
        nid = f"n{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s")
        clock.advance(1)
        if i < 3:
            tr.record_dismissed(nid)
        else:
            tr.record_accepted(nid)
        clock.advance(1)
    stats = tr.by_hour_reject_rate()
    assert 12 in stats
    rate, n = stats[12]
    assert n == 5
    assert rate == pytest.approx(0.6)


def test_by_hour_excludes_low_volume_hours(tmp_path):
    """Hours with fewer than min_volume scored dispatches are omitted."""
    clock = Clock(datetime(2026, 4, 21, 12, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    # Only 2 events at 12:xx — below min_volume=5
    for i in range(2):
        nid = f"n{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s")
        tr.record_dismissed(nid)
    assert 12 not in tr.by_hour_reject_rate()


def test_by_hour_excludes_neutral(tmp_path):
    """NEUTRAL not counted in numerator or denominator."""
    clock = Clock(datetime(2026, 4, 21, 12, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    # 5 events: 3 dismissed + 2 neutral. Scored = 3; reject = 3 → 100%.
    for i in range(3):
        nid = f"dis{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s")
        tr.record_dismissed(nid)
    for i in range(2):
        nid = f"neu{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s")
        tr.record_neutral(nid, reason="ambiguous")
    stats = tr.by_hour_reject_rate(min_volume=3)
    assert 12 in stats
    rate, n = stats[12]
    assert n == 3  # 5 dispatched - 2 neutral
    assert rate == pytest.approx(1.0)


def test_by_hour_separates_buckets(tmp_path):
    """Two hours with different rates appear as separate keys."""
    clock = Clock(datetime(2026, 4, 21, 9, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    # 09:xx — 5 dispatches, all accepted
    for i in range(5):
        nid = f"morn{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s")
        tr.record_accepted(nid)
    # advance to 14:00
    clock.advance(5 * 3600)
    # 14:xx — 5 dispatches, all dismissed
    for i in range(5):
        nid = f"aft{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s")
        tr.record_dismissed(nid)
    stats = tr.by_hour_reject_rate()
    assert 9 in stats and 14 in stats
    assert stats[9][0] == pytest.approx(0.0)
    assert stats[14][0] == pytest.approx(1.0)


def test_by_hour_accept_wins_over_ignored(tmp_path):
    """A nudge IGNORED by the sweep then later ACCEPTED is scored, not a
    reject — mirrors recent_topic_rejects' accept-wins."""
    clock = Clock(datetime(2026, 4, 21, 12, 0, 0))
    tr = _tracker(tmp_path / "fb.jsonl", clock)
    # 4 ignored-then-accepted + 1 dismissed, all at 12:xx.
    for i in range(4):
        nid = f"ia{i}"
        tr.record_dispatched(nid, action="nudge", session_key="s")
        tr.record_ignored(nid, window_seconds=600)
        tr.record_accepted(nid)
    tr.record_dispatched("dis", action="nudge", session_key="s")
    tr.record_dismissed("dis")
    stats = tr.by_hour_reject_rate(min_volume=3)
    assert 12 in stats
    rate, n = stats[12]
    assert n == 5  # all 5 scored
    assert rate == pytest.approx(0.2)  # only the 1 dismiss is a reject
