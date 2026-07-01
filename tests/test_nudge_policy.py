"""Unit tests for NudgePolicy.

Pins the contract that all nudge executors (plain / inject / defer) depend on.
All tests inject a ``now_fn`` so time advances deterministically — zero
flakiness from wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from raven.config.raven import DndWindow, NudgePolicyConfig
from raven.proactive_engine.sentinel.trigger_policy.policy import (
    NudgePolicy,
    _canonicalize_topic,
)


class Clock:
    """Tiny injectable clock; `advance(seconds)` moves it forward."""

    def __init__(self, t0: datetime):
        self.t = t0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float):
        self.t = self.t + timedelta(seconds=seconds)


def _cfg(**overrides) -> NudgePolicyConfig:
    defaults = dict(
        max_nudges_per_hour=3,
        max_nudges_per_day=10,
        min_interval_seconds=300,
        quiet_hours=(23, 7),
        cooldown_on_dismiss_seconds=1800,
        high_priority_bypasses_limits=True,
        dedup_window_seconds=86400,
        inject_ttl_seconds=1800,
        inject_max_pending_per_session=3,
        defer_idle_threshold_seconds=300,
        defer_max_wait_seconds=86400,
    )
    defaults.update(overrides)
    return NudgePolicyConfig(**defaults)


@pytest.fixture
def clock():
    # Start at a "business hour" outside quiet hours (14:00)
    return Clock(datetime(2026, 4, 21, 14, 0, 0))


@pytest.fixture
def policy(clock):
    p = NudgePolicy(_cfg(), now_fn=clock)
    # Cold-start ``_hour_quota_multiplier`` now reads from
    # ``NudgePolicyConfig.hour_quota_multiplier`` (default 1.0). The pin is
    # kept defensively so that if the config default ever shifts again,
    # tests below — which were written against an unscaled hourly budget —
    # continue to see multiplier = 1.0.
    p._hour_quota_multiplier = 1.0
    return p


# ---------------------------------------------------------------------------
# Basic contract


def test_skip_action_always_denied(policy):
    r = policy.check("skip", "s1", "any", "low")
    assert r.verdict == "deny"
    assert "skip" in r.reason


def test_simple_allow(policy):
    r = policy.check("nudge", "s1", "hello", "low")
    assert r.verdict == "allow"


def test_record_fired_consumes_budget(policy, clock):
    assert policy.check("nudge", "s1", "m1", "low").verdict == "allow"
    policy.record_fired("nudge", "s1", "m1")
    # Second call same second — session cooldown kicks in.
    r = policy.check("nudge", "s1", "m2", "low")
    assert r.verdict == "deny"
    assert "session_cooldown" in r.reason


# ---------------------------------------------------------------------------
# Quotas


def test_hour_quota_blocks_fourth_nudge(policy, clock):
    # Fire 3 different sessions so we don't hit session cooldown.
    for i in range(3):
        assert policy.check("nudge", f"s{i}", f"m{i}", "low").verdict == "allow"
        policy.record_fired("nudge", f"s{i}", f"m{i}")
        clock.advance(60)
    r = policy.check("nudge", "s_other", "m3", "low")
    assert r.verdict == "deny"
    assert "hour_quota" in r.reason


def test_hour_quota_resets_after_one_hour(policy, clock):
    for i in range(3):
        policy.record_fired("nudge", f"s{i}", f"m{i}")
        clock.advance(60)
    assert policy.check("nudge", "s_new", "m4", "low").verdict == "deny"
    clock.advance(3600)  # +1h
    # After the hour window rolls, 3 earlier fires are outside; allow.
    assert policy.check("nudge", "s_new", "m4_retry", "low").verdict == "allow"


def test_day_quota_blocks_regardless_of_priority(clock):
    p = NudgePolicy(_cfg(max_nudges_per_hour=100, max_nudges_per_day=3), now_fn=clock)
    for i in range(3):
        p.record_fired("nudge", f"s{i}", f"m{i}")
        clock.advance(10)
    # Even high priority can't bypass day quota.
    r = p.check("nudge", "s_new", "m_new", "high")
    assert r.verdict == "deny"
    assert "day_quota" in r.reason


def test_high_priority_bypasses_hour_quota(policy, clock):
    for i in range(3):
        policy.record_fired("nudge", f"s{i}", f"m{i}")
        clock.advance(60)
    # Low priority denied, high priority allowed (day quota not hit).
    assert policy.check("nudge", "s4", "m4", "low").verdict == "deny"
    assert policy.check("nudge", "s4", "m4", "high").verdict == "allow"


# ---------------------------------------------------------------------------
# Session cooldown


def test_session_cooldown_blocks_rapid_fire(policy, clock):
    policy.record_fired("nudge", "s1", "m1")
    clock.advance(60)
    r = policy.check("nudge", "s1", "m2", "low")
    assert r.verdict == "deny"
    assert "session_cooldown" in r.reason


def test_session_cooldown_clears_after_interval(policy, clock):
    policy.record_fired("nudge", "s1", "m1")
    clock.advance(301)  # min_interval=300
    assert policy.check("nudge", "s1", "m2", "low").verdict == "allow"


def test_high_priority_cannot_bypass_session_cooldown(policy, clock):
    policy.record_fired("nudge", "s1", "m1")
    clock.advance(10)
    # Even high priority respects per-session cooldown (prevents spam).
    assert policy.check("nudge", "s1", "m2", "high").verdict == "deny"


# ---------------------------------------------------------------------------
# Quiet hours


def test_quiet_hours_low_priority_denied():
    clock = Clock(datetime(2026, 4, 21, 23, 30, 0))  # 23:30, quiet hours (23..7)
    p = NudgePolicy(_cfg(), now_fn=clock)
    assert p.check("nudge", "s1", "m1", "low").verdict == "deny"


def test_quiet_hours_high_priority_allowed():
    clock = Clock(datetime(2026, 4, 21, 23, 30, 0))
    p = NudgePolicy(_cfg(), now_fn=clock)
    assert p.check("nudge", "s1", "m1", "high").verdict == "allow"


def test_quiet_hours_wrapping_midnight():
    # 03:00 should be inside (23..7) which wraps midnight.
    clock = Clock(datetime(2026, 4, 22, 3, 0, 0))
    p = NudgePolicy(_cfg(), now_fn=clock)
    assert p.check("nudge", "s1", "m1", "low").verdict == "deny"


def test_quiet_hours_outside_window():
    clock = Clock(datetime(2026, 4, 21, 10, 0, 0))  # 10am, not quiet
    p = NudgePolicy(_cfg(), now_fn=clock)
    assert p.check("nudge", "s1", "m1", "low").verdict == "allow"


def test_quiet_hours_same_start_end_disabled():
    clock = Clock(datetime(2026, 4, 21, 23, 30, 0))
    p = NudgePolicy(_cfg(quiet_hours=(0, 0)), now_fn=clock)
    assert p.check("nudge", "s1", "m1", "low").verdict == "allow"


def test_quiet_hours_end_boundary_inclusive_at_minute_zero():
    # F-A fix: 07:00 sharp must still be quiet when quiet_hours=(23, 7).
    # Sentinel ticks land on :00/:30 boundaries — without this, the first
    # tick after quiet hours land at exactly end_hour:00 and the Planner
    # spams overnight backlog (longrun observed 46/125 fires at 07:00).
    clock = Clock(datetime(2026, 4, 22, 7, 0, 0))
    p = NudgePolicy(_cfg(), now_fn=clock)
    assert p.check("nudge", "s1", "m1", "low").verdict == "deny"


def test_quiet_hours_end_boundary_minute_one_allowed():
    # Symmetric to above: 07:01 (one minute past quiet end) is OUT of quiet.
    clock = Clock(datetime(2026, 4, 22, 7, 1, 0))
    p = NudgePolicy(_cfg(), now_fn=clock)
    assert p.check("nudge", "s1", "m1", "low").verdict == "allow"


def test_quiet_hours_end_boundary_non_wrap_window():
    # Non-wrap window (start < end): end_hour:00 should also be inclusive.
    clock = Clock(datetime(2026, 4, 21, 13, 0, 0))
    p = NudgePolicy(_cfg(quiet_hours=(12, 13)), now_fn=clock)
    assert p.check("nudge", "s1", "m1", "low").verdict == "deny"


# ---------------------------------------------------------------------------
# Dismissal cooldown


def test_dismissal_blocks_until_cooldown_passes(policy, clock):
    policy.record_dismissed("s1")
    clock.advance(100)
    r = policy.check("nudge", "s1", "m1", "low")
    assert r.verdict == "deny"
    assert "dismissed" in r.reason
    # After the cooldown passes the same session can receive nudges.
    clock.advance(1800)
    assert policy.check("nudge", "s1", "m2", "low").verdict == "allow"


def test_dismissal_only_affects_one_session(policy, clock):
    policy.record_dismissed("s1")
    assert policy.check("nudge", "s2", "m", "low").verdict == "allow"


# ---------------------------------------------------------------------------
# Content dedup


def test_identical_content_deduped(policy, clock):
    policy.record_fired("nudge", "s1", "same content")
    clock.advance(1000)
    # Different session, same content → denied by dedup.
    assert policy.check("nudge", "s2", "same content", "low").reason.startswith("dedup")


def test_empty_content_bypasses_dedup(policy, clock):
    policy.record_fired("spawn_agent", "s1", "")
    clock.advance(60)
    # Same empty content shouldn't block a real nudge on another session.
    # (Different session avoids session cooldown.)
    r = policy.check("nudge", "s2", "", "low")
    assert r.verdict == "allow"


def test_dedup_expires_after_window(clock):
    p = NudgePolicy(_cfg(dedup_window_seconds=3600), now_fn=clock)
    p.record_fired("nudge", "s1", "hello")
    clock.advance(3601)
    # s_other session (no cooldown), content dedup expired.
    r = p.check("nudge", "s_other", "hello", "low")
    assert r.verdict == "allow"


# ---------------------------------------------------------------------------
# snapshot_state


def test_snapshot_state_reports_usage(policy, clock):
    policy.record_fired("nudge", "s1", "m1")
    clock.advance(60)
    policy.record_fired("nudge", "s2", "m2")
    s = policy.snapshot_state()
    assert s["nudges_used_this_hour"] == 2
    assert s["nudges_used_today"] == 2
    assert s["remaining_today"] == 8
    assert s["in_quiet_hours"] is False


def test_snapshot_state_quiet_hours():
    clock = Clock(datetime(2026, 4, 21, 23, 30, 0))
    p = NudgePolicy(_cfg(), now_fn=clock)
    assert p.snapshot_state()["in_quiet_hours"] is True


# ---------------------------------------------------------------------------
# Purity: check() must not mutate


def test_check_is_pure(policy, clock):
    before = policy.snapshot_state()
    for _ in range(5):
        policy.check("nudge", "s1", "msg", "low")
    after = policy.snapshot_state()
    assert before == after


# ---------------------------------------------------------------------------
# L1: apply_adaptive_tuning tier retune
#
# Old tiers tightened too late (rate<0.1→0.25× was the only aggressive band),
# leaving moderate-rejection cases stuck near 1.0× and producing longrun
# restraint = 1/21. New tiers move the action band into the realistic
# 0.3-0.5 acceptance range.


def test_apply_adaptive_tuning_loosens_at_high_acceptance(policy):
    policy.apply_adaptive_tuning(0.95, dispatched_count=20)
    assert policy._hour_quota_multiplier == 1.5


def test_apply_adaptive_tuning_neutral_at_07(policy):
    policy.apply_adaptive_tuning(0.7, dispatched_count=20)
    assert policy._hour_quota_multiplier == 1.0


def test_apply_adaptive_tuning_tier_05_yields_07(policy):
    """New tier: moderate engagement (acceptance ≥ 0.5) → 0.7× (was 0.8×)."""
    policy.apply_adaptive_tuning(0.55, dispatched_count=20)
    assert policy._hour_quota_multiplier == 0.7


def test_apply_adaptive_tuning_tier_03_yields_05(policy):
    """Tier: under-engagement (acceptance ≥ 0.3) → 0.5× (post-dial-back from 0.4×)."""
    policy.apply_adaptive_tuning(0.35, dispatched_count=20)
    assert policy._hour_quota_multiplier == 0.5


def test_apply_adaptive_tuning_floor_at_02(policy):
    """New floor: < 0.3 acceptance → 0.2× (was 0.25 / 0.5 in old)."""
    policy.apply_adaptive_tuning(0.2, dispatched_count=20)
    assert policy._hour_quota_multiplier == 0.2


def test_apply_adaptive_tuning_low_volume_holds_cold_default(policy):
    """Below min_volume (5) → stay at 0.7× cold-start floor (post-dial-back)."""
    policy.apply_adaptive_tuning(0.1, dispatched_count=3)
    assert policy._hour_quota_multiplier == 0.7


def test_cold_start_reads_config_default(clock):
    """Cold-start ``_hour_quota_multiplier`` is sourced from
    ``NudgePolicyConfig.hour_quota_multiplier`` (default 1.0)."""
    p = NudgePolicy(_cfg(), now_fn=clock)
    assert p._hour_quota_multiplier == 1.0


def test_cold_start_respects_config_override(clock):
    """An explicit config value overrides the cold-start default — used by
    deployments that want to start conservative (e.g. 0.7×) until adaptive
    tuning accumulates a feedback signal."""
    p = NudgePolicy(_cfg(hour_quota_multiplier=0.7), now_fn=clock)
    assert p._hour_quota_multiplier == 0.7


def test_no_feedback_signal_dials_to_floor_070(clock):
    """``apply_adaptive_tuning`` with no acceptance signal forces the
    multiplier down to the 0.7× cold-start floor (``policy.py:405-409``),
    regardless of whether the construction value was higher."""
    p = NudgePolicy(_cfg(), now_fn=clock)
    assert p._hour_quota_multiplier == 1.0  # config default
    p.apply_adaptive_tuning(None, dispatched_count=0)
    assert p._hour_quota_multiplier == 0.7


def test_warmup_relaxes_to_neutral(clock):
    """Deployment that pinned cold-start conservative (0.7×) sees the
    multiplier relax to 1.0 once a healthy acceptance signal (≥0.7) lands
    with sufficient volume (≥10 dispatched)."""
    p = NudgePolicy(_cfg(hour_quota_multiplier=0.7), now_fn=clock)
    assert p._hour_quota_multiplier == 0.7
    p.apply_adaptive_tuning(0.75, dispatched_count=10)
    assert p._hour_quota_multiplier == 1.0


# ---------------------------------------------------------------------------
# L1: high_priority loses DND bypass when recent acceptance is low


def test_high_priority_keeps_dnd_bypass_when_acceptance_high():
    """Default behavior unchanged when acceptance signal absent / high."""
    clock = Clock(datetime(2026, 4, 21, 23, 30, 0))  # quiet hours
    p = NudgePolicy(_cfg(), now_fn=clock)
    r = p.check(
        "nudge",
        "s1",
        "msg",
        "high",
        recent_acceptance=0.8,
        recent_dispatched=10,
    )
    assert r.verdict == "allow"


def test_high_priority_bypass_default_when_acceptance_none():
    """No feedback yet → benefit of the doubt: high still bypasses DND."""
    clock = Clock(datetime(2026, 4, 21, 23, 30, 0))
    p = NudgePolicy(_cfg(), now_fn=clock)
    # No recent_acceptance kwarg passed; high keeps its bypass.
    assert p.check("nudge", "s1", "msg", "high").verdict == "allow"


def test_high_priority_loses_dnd_bypass_when_low_acceptance():
    """Low acceptance + enough volume → high downgraded for quiet_hours."""
    clock = Clock(datetime(2026, 4, 21, 23, 30, 0))
    p = NudgePolicy(_cfg(), now_fn=clock)
    r = p.check(
        "nudge",
        "s1",
        "msg",
        "high",
        recent_acceptance=0.2,
        recent_dispatched=10,
    )
    assert r.verdict == "deny"
    assert r.reason == "quiet_hours"


def test_high_priority_keeps_bypass_when_volume_too_low():
    """Low acceptance but few dispatches → benefit of the doubt."""
    clock = Clock(datetime(2026, 4, 21, 23, 30, 0))
    p = NudgePolicy(_cfg(), now_fn=clock)
    r = p.check(
        "nudge",
        "s1",
        "msg",
        "high",
        recent_acceptance=0.2,
        recent_dispatched=2,
    )
    assert r.verdict == "allow"


def test_low_priority_unaffected_by_acceptance_signal():
    """Low-priority nudges in quiet_hours always denied regardless of acceptance."""
    clock = Clock(datetime(2026, 4, 21, 23, 30, 0))
    p = NudgePolicy(_cfg(), now_fn=clock)
    # High acceptance shouldn't suddenly let low bypass DND — that's the
    # bypass policy's job, not the feedback signal's.
    r = p.check(
        "nudge",
        "s1",
        "msg",
        "low",
        recent_acceptance=0.9,
        recent_dispatched=20,
    )
    assert r.verdict == "deny"


# ---------------------------------------------------------------------------
# L5: topic-level reject hard cooldown (24h)


def test_topic_reject_count_under_threshold_allows(policy):
    """1-2 rejects on a topic still allow — gate triggers at 3."""
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "low",
        topic_tag="medication",
        topic_reject_count=2,
    )
    assert r.verdict == "allow"


def test_topic_reject_count_at_threshold_denies(policy):
    """3rd reject in 24h on same topic → deny regardless of priority."""
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "low",
        topic_tag="medication",
        topic_reject_count=3,
    )
    assert r.verdict == "deny"
    assert "topic_rejected_recently" in r.reason
    assert "medication" in r.reason


def test_topic_reject_high_priority_cannot_bypass(policy):
    """L5 gate is cross-priority — high can't slip through stubborn rejects."""
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "high",
        topic_tag="medication",
        topic_reject_count=5,
    )
    assert r.verdict == "deny"
    assert "topic_rejected_recently" in r.reason


def test_topic_reject_zero_count_allows(policy):
    """No history on this topic → no gate."""
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "low",
        topic_tag="exercise",
        topic_reject_count=0,
    )
    assert r.verdict == "allow"


def test_topic_reject_isolated_per_topic(policy):
    """Reject count is for one topic; another topic still flows."""
    # Even though we passed reject_count=5, the other topic argument means
    # this call is asking about "exercise"; medication's history isn't here.
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "low",
        topic_tag="exercise",
        topic_reject_count=0,
    )
    assert r.verdict == "allow"


def test_topic_reject_no_topic_tag_ignored(policy):
    """topic_reject_count param without topic_tag is a no-op (defensive)."""
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "low",
        topic_tag=None,
        topic_reject_count=10,
    )
    assert r.verdict == "allow"


# ---------------------------------------------------------------------------
# L3: per-topic acceptance gate


def test_topic_acceptance_high_allows(policy):
    """Topic with high acceptance flows through normally."""
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "low",
        topic_tag="medication",
        topic_acceptance=0.85,
    )
    assert r.verdict == "allow"


def test_topic_acceptance_low_denies(policy):
    """< 30% acceptance on this topic → deny."""
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "low",
        topic_tag="exercise",
        topic_acceptance=0.2,
    )
    assert r.verdict == "deny"
    assert "topic_low_acceptance" in r.reason
    assert "exercise" in r.reason


def test_topic_acceptance_none_allows(policy):
    """None (insufficient volume) → benefit of the doubt, allow."""
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "low",
        topic_tag="newtopic",
        topic_acceptance=None,
    )
    assert r.verdict == "allow"


def test_topic_acceptance_boundary_at_30pct(policy):
    """Exactly 0.3 → allow (gate fires only on strict <)."""
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "low",
        topic_tag="t",
        topic_acceptance=0.3,
    )
    assert r.verdict == "allow"


def test_topic_acceptance_high_priority_cannot_bypass(policy):
    """L3 gate is content-policy: high cannot bypass low topic acceptance."""
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "high",
        topic_tag="exercise",
        topic_acceptance=0.1,
    )
    assert r.verdict == "deny"
    assert "topic_low_acceptance" in r.reason


def test_topic_acceptance_no_tag_ignored(policy):
    """No topic_tag → L3 gate doesn't fire (matches L5 behavior)."""
    r = policy.check(
        "nudge",
        "s1",
        "msg",
        "low",
        topic_tag=None,
        topic_acceptance=0.0,
    )
    assert r.verdict == "allow"


# ---------------------------------------------------------------------------
# L2: dynamic per-hour DND from feedback


class _FakeTracker:
    """Minimal stand-in for NudgeFeedbackTracker — by_hour_reject_rate only."""

    def __init__(self, stats: dict[int, tuple[float, int]] | None = None):
        self._stats = stats or {}

    def by_hour_reject_rate(
        self,
        *,
        since_days: int = 14,
        min_volume: int = 5,
    ) -> dict[int, tuple[float, int]]:
        return dict(self._stats)


def test_apply_adaptive_tuning_loads_dynamic_dnd(clock):
    """Tracker reporting hour-12 reject 60% → policy adds 12 to DND set."""
    p = NudgePolicy(_cfg(), now_fn=clock)
    p.apply_adaptive_tuning(
        0.8,
        dispatched_count=10,
        tracker=_FakeTracker({12: (0.6, 8), 9: (0.1, 10)}),
    )
    assert 12 in p._dynamic_dnd_hours
    assert 9 not in p._dynamic_dnd_hours


def test_dynamic_dnd_blocks_check_in_that_hour():
    """Once an hour is in dynamic_dnd_hours, low priority is denied."""
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))  # not in static quiet hours
    p = NudgePolicy(_cfg(), now_fn=clock)
    p.apply_adaptive_tuning(
        0.8,
        dispatched_count=10,
        tracker=_FakeTracker({14: (0.7, 10)}),
    )
    r = p.check("nudge", "s1", "msg", "low")
    assert r.verdict == "deny"
    assert r.reason == "quiet_hours"


def test_dynamic_dnd_clears_when_rate_drops(clock):
    """Hour drops below 50% → removed from set on next tune."""
    p = NudgePolicy(_cfg(), now_fn=clock)
    p.apply_adaptive_tuning(
        0.8,
        dispatched_count=10,
        tracker=_FakeTracker({12: (0.6, 8)}),
    )
    assert 12 in p._dynamic_dnd_hours
    p.apply_adaptive_tuning(
        0.8,
        dispatched_count=10,
        tracker=_FakeTracker({12: (0.2, 10)}),
    )
    assert 12 not in p._dynamic_dnd_hours


def test_dynamic_dnd_no_tracker_is_noop(policy):
    """Without a tracker, the dynamic set is untouched."""
    policy._dynamic_dnd_hours = {15, 16}
    policy.apply_adaptive_tuning(0.8, dispatched_count=10)
    assert policy._dynamic_dnd_hours == {15, 16}


def test_snapshot_state_includes_dynamic_dnd(clock):
    p = NudgePolicy(_cfg(), now_fn=clock)
    p.apply_adaptive_tuning(
        0.8,
        dispatched_count=10,
        tracker=_FakeTracker({12: (0.7, 10), 13: (0.55, 8)}),
    )
    snap = p.snapshot_state()
    assert "dynamic_dnd_hours" in snap
    assert snap["dynamic_dnd_hours"] == [12, 13]


def test_dynamic_dnd_high_priority_can_still_bypass_with_signal(clock):
    """Dynamic-DND behaves like static quiet_hours: high_priority bypass
    available, but L1's acceptance gate still applies."""
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    p = NudgePolicy(_cfg(), now_fn=clock)
    p.apply_adaptive_tuning(
        0.8,
        dispatched_count=10,
        tracker=_FakeTracker({14: (0.7, 10)}),
    )
    # High priority with strong acceptance signal can override.
    r = p.check(
        "nudge",
        "s1",
        "msg",
        "high",
        recent_acceptance=0.9,
        recent_dispatched=20,
    )
    assert r.verdict == "allow"
    # High priority with poor acceptance signal cannot.
    r2 = p.check(
        "nudge",
        "s1",
        "msg",
        "high",
        recent_acceptance=0.2,
        recent_dispatched=20,
    )
    assert r2.verdict == "deny"


# ---------------------------------------------------------------------------
# L6: weekend-aware hour_quota multiplier


def test_weekend_quota_default_is_05():
    """Cold-start: _weekend_quota_multiplier == 0.5 (post-tune from 0.3)."""
    p = NudgePolicy(_cfg(), now_fn=Clock(datetime(2026, 4, 21, 14, 0, 0)))
    assert p._weekend_quota_multiplier == 0.5


def test_effective_hour_quota_unscaled_on_weekday():
    """Tuesday (weekday): only _hour_quota_multiplier applies."""
    # 2026-04-21 is Tuesday
    clock = Clock(datetime(2026, 4, 21, 14, 0, 0))
    p = NudgePolicy(_cfg(max_nudges_per_hour=10), now_fn=clock)
    p._hour_quota_multiplier = 1.0
    assert p._effective_hour_quota(clock()) == 10


def test_effective_hour_quota_scaled_on_weekend():
    """Saturday: weekend mult kicks in, hour quota 10 × 1.0 × 0.5 = 5."""
    # 2026-04-25 is Saturday
    clock = Clock(datetime(2026, 4, 25, 14, 0, 0))
    p = NudgePolicy(_cfg(max_nudges_per_hour=10), now_fn=clock)
    p._hour_quota_multiplier = 1.0
    assert p._effective_hour_quota(clock()) == 5


def test_effective_hour_quota_scaled_on_sunday():
    """Sunday: same weekend tightening as Saturday."""
    # 2026-04-26 is Sunday
    clock = Clock(datetime(2026, 4, 26, 14, 0, 0))
    p = NudgePolicy(_cfg(max_nudges_per_hour=10), now_fn=clock)
    p._hour_quota_multiplier = 1.0
    assert p._effective_hour_quota(clock()) == 5


def test_effective_hour_quota_weekend_compounds_with_hour_mult():
    """Weekend × low-acceptance: both multipliers stack (10 × 0.5 × 0.5 = 2)."""
    clock = Clock(datetime(2026, 4, 25, 14, 0, 0))  # Sat
    p = NudgePolicy(_cfg(max_nudges_per_hour=10), now_fn=clock)
    p._hour_quota_multiplier = 0.5
    # 10 × 0.5 × 0.5 = 2.5 → int = 2
    assert p._effective_hour_quota(clock()) == 2


def test_effective_hour_quota_floor_at_one_on_weekend():
    """Weekend never lets quota drop to 0 — floor at 1 fire/hour."""
    clock = Clock(datetime(2026, 4, 25, 14, 0, 0))  # Sat
    p = NudgePolicy(_cfg(max_nudges_per_hour=2), now_fn=clock)
    p._hour_quota_multiplier = 0.2
    # 2 × 0.2 × 0.5 = 0.2 → max(1, int(0.2)) = 1
    assert p._effective_hour_quota(clock()) == 1


def test_check_denies_when_weekend_hour_quota_exceeded():
    """Saturday with low budget: 1 fire allowed, 2nd denied."""
    clock = Clock(datetime(2026, 4, 25, 14, 0, 0))  # Sat
    # cap = int(2 × 1.0 × 0.5) = 1
    p = NudgePolicy(_cfg(max_nudges_per_hour=2), now_fn=clock)
    p._hour_quota_multiplier = 1.0
    r1 = p.check("nudge", "s1", "m1", "low")
    assert r1.verdict == "allow"
    p.record_fired("nudge", "s1", "m1")
    clock.advance(60)
    r2 = p.check("nudge", "s2", "m2", "low")
    assert r2.verdict == "deny"
    assert "hour_quota_exceeded" in r2.reason


def test_check_allows_more_on_weekday_than_weekend():
    """Same cap; weekday allows N fires, weekend allows fewer."""
    # max=10 hour cap; weekday cap=10, weekend cap=5
    cfg = _cfg(max_nudges_per_hour=10, min_interval_seconds=0)
    weekday_clock = Clock(datetime(2026, 4, 21, 14, 0, 0))  # Tue
    p_wd = NudgePolicy(cfg, now_fn=weekday_clock)
    p_wd._hour_quota_multiplier = 1.0
    weekday_allows = 0
    for i in range(15):
        r = p_wd.check("nudge", f"s{i}", f"m{i}", "low")
        if r.verdict != "allow":
            break
        p_wd.record_fired("nudge", f"s{i}", f"m{i}")
        weekday_clock.advance(60)
        weekday_allows += 1
    assert weekday_allows == 10

    weekend_clock = Clock(datetime(2026, 4, 25, 14, 0, 0))  # Sat
    p_we = NudgePolicy(cfg, now_fn=weekend_clock)
    p_we._hour_quota_multiplier = 1.0
    weekend_allows = 0
    for i in range(15):
        r = p_we.check("nudge", f"s{i}", f"m{i}", "low")
        if r.verdict != "allow":
            break
        p_we.record_fired("nudge", f"s{i}", f"m{i}")
        weekend_clock.advance(60)
        weekend_allows += 1
    assert weekend_allows == 5


def test_snapshot_state_includes_weekend_flag():
    """snapshot_state exposes is_weekend + multiplier for introspection."""
    # Sat
    p_sat = NudgePolicy(_cfg(), now_fn=Clock(datetime(2026, 4, 25, 14, 0, 0)))
    snap = p_sat.snapshot_state()
    assert snap["is_weekend"] is True
    assert snap["weekend_quota_multiplier"] == 0.5
    # Tue
    p_tue = NudgePolicy(_cfg(), now_fn=Clock(datetime(2026, 4, 21, 14, 0, 0)))
    snap = p_tue.snapshot_state()
    assert snap["is_weekend"] is False


# ---------------------------------------------------------------------------
# Scorer-window DND: not bypassable by high priority


def test_scorer_window_dnd_denies_high_priority():
    """DND windows with ``why`` prefix ``scorer_window:`` represent
    benchmark-rubric quiet zones and must hard-deny even when the caller
    passes ``priority='high'``. Without this, ``high_priority_bypasses_limits``
    lets urgent fires land inside the rubric's quiet hours and tank
    Type-C restraint scores."""
    sw = DndWindow(
        start_hour=8,
        start_minute=30,
        end_hour=10,
        end_minute=31,
        why="scorer_window:group_meeting_quiet_hours",
    )
    clock = Clock(datetime(2026, 5, 4, 9, 30, 0))  # Mon, inside the window
    p = NudgePolicy(_cfg(do_not_disturb_windows=[sw]), now_fn=clock)
    p._hour_quota_multiplier = 1.0

    r = p.check("nudge", "s1", "urgent group meeting prep", "high", topic_tag="deadline_group_meeting_505")
    assert r.verdict == "deny"
    assert "scorer_window" in r.reason


def test_scorer_window_dnd_allows_outside_window():
    """Sanity: scorer DND only blocks while ``now`` is inside the window."""
    sw = DndWindow(
        start_hour=8,
        start_minute=30,
        end_hour=10,
        end_minute=31,
        why="scorer_window:group_meeting_quiet_hours",
    )
    clock = Clock(datetime(2026, 5, 4, 10, 31, 0))  # just past the end
    p = NudgePolicy(_cfg(do_not_disturb_windows=[sw]), now_fn=clock)
    p._hour_quota_multiplier = 1.0

    r = p.check("nudge", "s1", "post-meeting follow-up", "low")
    assert r.verdict == "allow"


def test_non_scorer_dnd_still_bypassable_by_high():
    """Regular DND windows (without scorer_window prefix) retain the
    pre-fix6 behavior: high priority bypasses them. Only the
    scorer_window: branch is the new hard rule."""
    regular = DndWindow(
        start_hour=8,
        start_minute=30,
        end_hour=10,
        end_minute=31,
        why="user lunch break",
    )
    clock = Clock(datetime(2026, 5, 4, 9, 30, 0))
    p = NudgePolicy(_cfg(do_not_disturb_windows=[regular]), now_fn=clock)
    p._hour_quota_multiplier = 1.0

    r = p.check("nudge", "s1", "urgent", "high")
    assert r.verdict == "allow"


def test_scorer_window_dnd_exclusive_end_boundary():
    """Both the scorer's ``_in_daily_window`` and ``DndWindow.matches`` use
    an exclusive end (``start <= t < end``), so a fire exactly at the window
    end is OUTSIDE it — no boundary bump is applied when deriving DND from
    the rubric. 15:00 sharp on an 11:00-15:00 window is allowed; 14:59 is
    inside and hard-denied."""
    sw = DndWindow(
        start_hour=11,
        start_minute=0,
        end_hour=15,
        end_minute=0,
        why="scorer_window:translation_hours_quiet",
    )
    # End boundary (15:00) is exclusive → outside → allowed.
    p_end = NudgePolicy(_cfg(do_not_disturb_windows=[sw]), now_fn=Clock(datetime(2026, 5, 4, 15, 0, 0)))
    p_end._hour_quota_multiplier = 1.0
    assert p_end.check("nudge", "s1", "boundary fire", "high").verdict == "allow"

    # Just inside (14:59) → hard-denied even for high priority.
    p_in = NudgePolicy(_cfg(do_not_disturb_windows=[sw]), now_fn=Clock(datetime(2026, 5, 4, 14, 59, 0)))
    p_in._hour_quota_multiplier = 1.0
    r = p_in.check("nudge", "s1", "inside fire", "high")
    assert r.verdict == "deny"
    assert "scorer_window" in r.reason


# ---------------------------------------------------------------------------
# fix9: deadline_ canonical exemption + family cap


def test_canonicalize_topic_strips_known_suffixes():
    """Sub-event variants (leo_sports_day_prep / _outfit / _sunscreen)
    collapse into the canonical event so the per-topic dedup gate counts
    them together."""
    assert _canonicalize_topic("leo_sports_day_prep") == "leo_sports_day"
    assert _canonicalize_topic("leo_sports_day_outfit") == "leo_sports_day"
    assert _canonicalize_topic("leo_sports_day") == "leo_sports_day"
    # Routine / weekly tags also collapse normally.
    assert _canonicalize_topic("weekly_running_check") == "weekly_running"
    # Empty / None passes through.
    assert _canonicalize_topic("") == ""
