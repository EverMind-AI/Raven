"""Unit tests for SentinelObservationsProducer — renders the
``## Sentinel Observations (auto)`` diagnostic section into attention.md.

Locks the contract: cooldown-gated 24h rewrite, MIN_FEEDBACK threshold
skip on cold-start, body content (signal counts / topic stats / dismiss
hours / adaptive tuning multiplier). Uses ``AttentionUpdater`` to write
through to attention.md so the lock + splice path is exercised
end-to-end.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from raven.config.raven import NudgePolicyConfig
from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.proactive_engine.sentinel.attention_producers import (
    SentinelObservationsProducer,
)
from raven.proactive_engine.sentinel.attention_updater import AttentionUpdater
from raven.proactive_engine.sentinel.feedback.tracker import NudgeFeedbackTracker
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy


class Clock:
    def __init__(self, t0: datetime) -> None:
        self.t = t0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 5, 15, 7, 0))


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> MemoryStore:
    return MemoryStore(tmp_path, now_fn=clock)


@pytest.fixture
def feedback(tmp_path: Path, clock: Clock) -> NudgeFeedbackTracker:
    return NudgeFeedbackTracker(
        log_path=tmp_path / "feedback.jsonl", now_fn=clock,
    )


@pytest.fixture
def policy(clock: Clock) -> NudgePolicy:
    return NudgePolicy(NudgePolicyConfig(), now_fn=clock)


@pytest.fixture
def producer(store, feedback, policy, clock) -> SentinelObservationsProducer:
    return SentinelObservationsProducer(
        memory_store=store, feedback=feedback, policy=policy, now_fn=clock,
    )


@pytest.fixture
def updater(store, producer, clock) -> AttentionUpdater:
    return AttentionUpdater(
        memory_store=store, producers=[producer], now_fn=clock,
    )


def _seed_feedback(
    fb: NudgeFeedbackTracker, *, dispatched: int, accepted: int, dismissed: int,
) -> None:
    for i in range(dispatched):
        fb.record_dispatched(
            f"n-{i}", action="nudge", session_key="default",
            priority="medium", proactivity_score=0.7, source="planner_tick",
        )
    for i in range(accepted):
        fb.record_accepted(f"n-{i}")
    for i in range(dismissed):
        fb.record_dismissed(f"n-{dispatched - 1 - i}", reason="too noisy")


def _seed_topic_fires(
    policy: NudgePolicy, clock: Clock, topic_counts: dict[str, int],
) -> None:
    for tag, n in topic_counts.items():
        for i in range(n):
            policy.record_fired(
                "nudge", "default", f"msg about {tag} #{i}", topic_tag=tag,
            )
            clock.advance(60)
        clock.advance(3600)


def _run(updater: AttentionUpdater):
    return asyncio.get_event_loop().run_until_complete(updater.update())


# ===========================================================================


class TestShouldRunGate:
    def test_skips_when_feedback_below_threshold(
        self, producer, feedback, clock,
    ) -> None:
        _seed_feedback(feedback, dispatched=2, accepted=1, dismissed=0)
        assert producer.should_run(clock()) is False

    def test_runs_when_threshold_met_and_no_prior(
        self, producer, feedback, clock,
    ) -> None:
        _seed_feedback(feedback, dispatched=5, accepted=4, dismissed=1)
        assert producer.should_run(clock()) is True

    def test_skips_when_recently_updated_within_24h(
        self, producer, feedback, store, clock,
    ) -> None:
        _seed_feedback(feedback, dispatched=5, accepted=4, dismissed=1)
        recent_iso = clock().isoformat(timespec="minutes")
        store.attention_file.parent.mkdir(parents=True, exist_ok=True)
        store.attention_file.write_text(
            f"## Sentinel Observations (auto)\n\n"
            f"<!-- last_updated={recent_iso} -->\n\nfresh body\n",
            encoding="utf-8",
        )
        assert producer.should_run(clock()) is False

    def test_runs_when_24h_passed(
        self, producer, feedback, store, clock,
    ) -> None:
        _seed_feedback(feedback, dispatched=5, accepted=4, dismissed=1)
        old_iso = (clock() - timedelta(hours=25)).isoformat(timespec="minutes")
        store.attention_file.parent.mkdir(parents=True, exist_ok=True)
        store.attention_file.write_text(
            f"## Sentinel Observations (auto)\n\n"
            f"<!-- last_updated={old_iso} -->\n\nold body\n",
            encoding="utf-8",
        )
        assert producer.should_run(clock()) is True


# ===========================================================================


class TestSectionBody:
    def test_writes_three_subsections(
        self, updater, feedback, policy, store, clock,
    ) -> None:
        _seed_feedback(feedback, dispatched=10, accepted=7, dismissed=3)
        _seed_topic_fires(policy, clock, {"deadline_x": 3, "birthday_y": 2})
        _run(updater)
        body = store.attention_file.read_text(encoding="utf-8")
        assert "### Signal counts" in body
        assert "### Topics fired" in body
        assert "### Adaptive tuning" in body
        assert "deadline_x" in body or "birthday_y" in body
        assert "accepted" in body.lower()
        assert "hour_quota" in body.lower()

    def test_per_topic_accept_rate_with_hints(
        self, updater, feedback, store,
    ) -> None:
        # Topic A: 5 dispatch / 5 accept → 100% / ✓ well-received
        for i in range(5):
            feedback.record_dispatched(
                f"a-{i}", action="nudge", session_key="default",
                priority="medium", proactivity_score=0.7,
                details={"topic_tag": "deadline_x"},
            )
            feedback.record_accepted(f"a-{i}")
        # Topic B: 4 dispatch / 3 dismiss → 0% / ⚠ high-dismiss
        for i in range(4):
            feedback.record_dispatched(
                f"b-{i}", action="nudge", session_key="default",
                priority="medium", proactivity_score=0.6,
                details={"topic_tag": "routine_run"},
            )
        for i in range(3):
            feedback.record_dismissed(f"b-{i}", reason="too noisy")
        _run(updater)
        body = store.attention_file.read_text(encoding="utf-8")
        assert (
            "`deadline_x` × 5 (accept 5, dismiss 0, accept_rate 100%)" in body
        )
        assert (
            "`routine_run` × 4 (accept 0, dismiss 3, accept_rate 0%)" in body
        )
        assert "✓ well-received" in body
        assert "⚠ high-dismiss → de-prioritize" in body

    def test_fallback_when_no_topic_tag_in_details(
        self, updater, feedback, policy, store, clock,
    ) -> None:
        _seed_feedback(feedback, dispatched=5, accepted=4, dismissed=1)
        _seed_topic_fires(policy, clock, {"legacy_topic": 4})
        _run(updater)
        body = store.attention_file.read_text(encoding="utf-8")
        assert "`legacy_topic` × 4" in body
        assert "no feedback joined" in body

    def test_last_updated_cookie_present(
        self, updater, feedback, store, clock,
    ) -> None:
        _seed_feedback(feedback, dispatched=5, accepted=4, dismissed=1)
        _run(updater)
        body = store.attention_file.read_text(encoding="utf-8")
        import re as _re
        m = _re.search(r"<!--\s*last_updated=([0-9T:\-]+)\s*-->", body)
        assert m is not None
        assert m.group(1).startswith(clock().date().isoformat())


# ===========================================================================


class TestSplicePreservesOtherSections:
    def test_preserves_other_attention_sections(
        self, updater, feedback, store,
    ) -> None:
        store.attention_file.parent.mkdir(parents=True, exist_ok=True)
        store.attention_file.write_text(
            "## User overrides\n- 凌晨别 nudge\n\n"
            "## Pending proposals\n- prop_42\n",
            encoding="utf-8",
        )
        _seed_feedback(feedback, dispatched=5, accepted=4, dismissed=1)
        _run(updater)
        body = store.attention_file.read_text(encoding="utf-8")
        assert "## User overrides" in body
        assert "凌晨别 nudge" in body
        assert "## Pending proposals" in body
        assert "## Sentinel Observations (auto)" in body
