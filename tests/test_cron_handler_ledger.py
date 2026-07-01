"""F-G: cron triggers write to the shared NudgePolicy ledger.

When a cron fires, the on_cron_job handler must update topic_fired_at +
record_dispatched so the L3 Sentinel suppresses redundant proactive
nudges on the same topic. Without this, Sentinel and Cron are blind to
each other and the user gets double-nudged on the same subject.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from raven.cli._cron_handler import _record_cron_dispatch_to_ledger
from raven.config.raven import NudgePolicyConfig
from raven.proactive_engine.schedulers.cron.types import (
    CronJob,
    CronPayload,
    CronSchedule,
)
from raven.proactive_engine.sentinel.feedback.tracker import (
    FeedbackSignal,
    NudgeFeedbackTracker,
)
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _make_runner(tmp_path: Path) -> object:
    """Lightweight SentinelRunner stand-in with just the two attrs F-G
    touches: ``policy`` and ``feedback``. Building a full runner here
    pulls in the entire predictor/spawn graph, which the test doesn't
    need."""
    clock = _Clock(datetime(2026, 5, 20, 12, 0, 0))
    policy = NudgePolicy(NudgePolicyConfig(), now_fn=clock)
    feedback = NudgeFeedbackTracker(
        tmp_path / "sentinel_feedback.jsonl",
        now_fn=clock,
    )

    class _Runner:
        pass

    runner = _Runner()
    runner.policy = policy
    runner.feedback = feedback
    return runner


def _make_job(*, topic_tag: str | None, message: str = "吃药提醒") -> CronJob:
    return CronJob(
        id="testjob1",
        name=message[:30],
        schedule=CronSchedule(kind="at", at_ms=1779144600000),
        payload=CronPayload(
            kind="agent_turn",
            message=message,
            deliver=True,
            channel="cli",
            to="direct",
            topic_tag=topic_tag,
        ),
    )


def test_cron_fire_updates_topic_fired_at_when_tag_set(tmp_path):
    runner = _make_runner(tmp_path)
    job = _make_job(topic_tag="medication_morning")

    _record_cron_dispatch_to_ledger(runner, job)

    # Sentinel's topic_quota gate now sees this fire — next tick on the
    # same topic within 24h will count it.
    assert "medication_morning" in runner.policy._topic_fired_at
    assert len(runner.policy._topic_fired_at["medication_morning"]) == 1


def test_cron_fire_no_topic_still_records_dispatched(tmp_path):
    # Without a topic_tag, dedup-by-topic doesn't apply but the fire is
    # still logged to the feedback tracker so analytics see it.
    runner = _make_runner(tmp_path)
    job = _make_job(topic_tag=None, message="random one-off")

    _record_cron_dispatch_to_ledger(runner, job)

    # No topic dedup, but global fire counter advances.
    assert "random one-off" not in runner.policy._topic_fired_at
    counts = runner.feedback.counts(since_days=1)
    assert counts[FeedbackSignal.DISPATCHED.value] >= 1


def test_cron_fire_records_neutral_for_acceptance_rate(tmp_path):
    # B4: cron fires should land as DISPATCHED + NEUTRAL so they don't
    # drag down acceptance_rate (user explicitly scheduled them, so
    # measuring "did the user accept this prompt we sent" is non-
    # sensical for cron — the user IS the proposer).
    runner = _make_runner(tmp_path)
    job = _make_job(topic_tag="medication_morning")

    _record_cron_dispatch_to_ledger(runner, job)

    counts = runner.feedback.counts(since_days=1)
    # Both signals recorded:
    assert counts[FeedbackSignal.DISPATCHED.value] >= 1
    assert counts[FeedbackSignal.NEUTRAL.value] >= 1
    # acceptance_rate should return None (no scored dispatches — neutral
    # excludes from denominator) instead of 0.0:
    rate = runner.feedback.acceptance_rate(since_days=7, min_volume=1)
    assert rate is None, f"cron fires should not count in acceptance_rate, got {rate}"


def test_cron_fire_records_dispatched_with_source_cron(tmp_path):
    # Source = "cron" lets adaptive tuner know this event came from the
    # cron surface, not Sentinel — separable in dashboards / per-source
    # acceptance rates.
    runner = _make_runner(tmp_path)
    job = _make_job(topic_tag="birthday_xiaotang")

    _record_cron_dispatch_to_ledger(runner, job)

    recent = runner.feedback.recent(n=5)
    assert len(recent) >= 2  # DISPATCHED + NEUTRAL pair
    # Find the dispatched record (NEUTRAL is the trailing record post-B4).
    dispatched = next(r for r in recent if r.get("signal") == "dispatched")
    assert dispatched["source"] == "cron"
    assert dispatched["details"]["topic_tag"] == "birthday_xiaotang"
    assert dispatched["details"]["cron_id"] == "testjob1"


def test_cron_fire_does_not_block_user_intent(tmp_path):
    # F-G is INFORM-only: even if the policy would normally deny (e.g.
    # quiet hours), the cron still gets logged. The cron itself fires
    # unconditionally because the user explicitly scheduled it. This
    # test pins that contract — ledger writes must not check verdicts.
    runner = _make_runner(tmp_path)
    job = _make_job(topic_tag="medication_morning")

    # No verdict check, no AssertionError, no exception:
    _record_cron_dispatch_to_ledger(runner, job)
    assert "medication_morning" in runner.policy._topic_fired_at


def test_cron_fire_ledger_failure_is_silent(tmp_path):
    # A flaky ledger must NOT crash the cron delivery path. F-G wraps
    # the entire write in try/except.
    class _BrokenRunner:
        @property
        def policy(self):
            raise RuntimeError("simulated ledger outage")

    job = _make_job(topic_tag="medication_morning")
    # Should not raise:
    _record_cron_dispatch_to_ledger(_BrokenRunner(), job)


def test_cron_fire_suppresses_next_sentinel_tick_same_topic(tmp_path):
    # The whole point of F-G: after cron fires topic X, the Sentinel's
    # topic_quota gate should deny a same-topic nudge within the dedup
    # window.
    runner = _make_runner(tmp_path)
    job = _make_job(topic_tag="birthday_xiaotang")
    _record_cron_dispatch_to_ledger(runner, job)

    # Simulate a Sentinel tick attempting the same topic immediately
    # after. With default topic_quota config (max 1 per 24h), this is
    # denied.
    result = runner.policy.check(
        "nudge",
        "sim:dev-01:main",
        "girlfriend birthday in 7 days",
        "low",
        topic_tag="birthday_xiaotang",
    )
    assert result.verdict == "deny"
    assert "topic" in result.reason  # topic_quota or similar
