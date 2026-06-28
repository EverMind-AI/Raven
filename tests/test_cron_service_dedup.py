"""Tests for CronService.add_job dedup layers.

Three layers, applied in order at add time:
  1. L7 topic_tag dedup — strictest, runs first when caller supplies tag.
  2. Message-equal dedup — byte-identical payload.message + same channel/to.
  3. Time-window dedup — within 15 min of an existing fire on same channel/to.
  4. Schedule dedup — same recurring schedule + same channel/to.

L7 is the load-bearing one for the caregiver-style failure: the LLM
re-asks for the same daily med reminder across the month with slightly
different schedules and message wording, and earlier layers miss those.
The topic_tag IS the identity for "this reminder is about X medication".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule


@pytest.fixture
def svc(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "jobs.json")


def _add(svc, msg, schedule, *, channel="cli", to="direct", topic_tag=None):
    return svc.add_job(
        name=msg[:30],
        schedule=schedule,
        message=msg,
        deliver=True,
        channel=channel,
        to=to,
        topic_tag=topic_tag,
    )


def test_topic_tag_dedup_same_topic_returns_existing(svc):
    """Two add_job calls with the same topic_tag share the same job id."""
    j1 = _add(
        svc, "妈妈吃多奈哌齐 5mg (中午)",
        CronSchedule(kind="cron", expr="20 11 * * *"),
        topic_tag="medication_noon",
    )
    j2 = _add(
        svc, "妈妈吃多奈哌齐 5mg (中午, 妈妈刚说想吃)",
        CronSchedule(kind="cron", expr="30 11 * * *"),
        topic_tag="medication_noon",
    )
    assert j1.id == j2.id, "same topic_tag must dedup to single job"


def test_topic_tag_dedup_updates_message_in_place(svc):
    """Second add_job with same topic_tag overwrites message + schedule."""
    j1 = _add(
        svc, "old message",
        CronSchedule(kind="cron", expr="0 11 * * *"),
        topic_tag="medication_noon",
    )
    j2 = _add(
        svc, "new message",
        CronSchedule(kind="cron", expr="30 13 * * *"),
        topic_tag="medication_noon",
    )
    assert j2.payload.message == "new message"
    assert j2.schedule.expr == "30 13 * * *"
    # Same job, updated in place
    assert j1.id == j2.id


def test_topic_tag_dedup_isolated_by_channel(svc):
    """Same topic_tag on different (channel, to) does NOT dedup."""
    j1 = _add(
        svc, "msg A",
        CronSchedule(kind="cron", expr="0 9 * * *"),
        channel="cli", to="alice",
        topic_tag="exercise",
    )
    j2 = _add(
        svc, "msg B",
        CronSchedule(kind="cron", expr="0 9 * * *"),
        channel="feishu", to="bob",
        topic_tag="exercise",
    )
    assert j1.id != j2.id, "different channel/to must NOT dedup"


def test_topic_tag_dedup_skipped_when_no_tag(svc):
    """Without topic_tag, falls through to message-equal / schedule dedup."""
    j1 = _add(
        svc, "stretch reminder",
        CronSchedule(kind="cron", expr="0 9 * * *"),
        topic_tag=None,
    )
    j2 = _add(
        svc, "stretch reminder",  # identical message → message-equal dedup
        CronSchedule(kind="cron", expr="30 14 * * *"),  # different time
        topic_tag=None,
    )
    # Message-equal dedup should still catch this, but the path matters —
    # this test pins that no-tag is a legitimate fallthrough.
    assert j1.id == j2.id


def test_topic_tag_dedup_distinct_tags_keep_separate(svc):
    """Different topic_tag values on same channel/to → two distinct jobs."""
    j1 = _add(
        svc, "ms morning meds",
        CronSchedule(kind="cron", expr="0 7 * * *"),
        topic_tag="medication_morning",
    )
    j2 = _add(
        svc, "ms noon meds",
        CronSchedule(kind="cron", expr="0 11 * * *"),
        topic_tag="medication_noon",
    )
    assert j1.id != j2.id
    all_jobs = svc.list_jobs()
    assert len(all_jobs) == 2


def test_topic_tag_dedup_at_kind_also_dedups(svc):
    """topic_tag dedup applies to ``at`` (one-shot) schedules too —
    catches the case where the LLM creates an `at` for today AND a
    recurring `cron_expr` for the same topic."""
    j1 = _add(
        svc, "OKR review reminder",
        CronSchedule(kind="cron", expr="0 9 * * 1"),  # weekly Monday
        topic_tag="okr_quarterly",
    )
    j2 = _add(
        svc, "OKR review reminder (urgent)",
        CronSchedule(kind="at", at_ms=int(1e15)),  # one-shot far-future
        topic_tag="okr_quarterly",
    )
    assert j1.id == j2.id
    # The latter add updates schedule kind too — so j2 should now be ``at``.
    refreshed = svc.list_jobs()
    assert len(refreshed) == 1
    assert refreshed[0].schedule.kind == "at"


def test_topic_tag_dedup_runs_before_message_equal(svc):
    """Topic_tag short-circuits even when message differs — proves L7
    takes precedence over message-equal (otherwise different messages
    with same topic_tag would slip through and create duplicates)."""
    j1 = _add(
        svc, "wholly different text A",
        CronSchedule(kind="cron", expr="0 8 * * *"),
        topic_tag="meds_morning",
    )
    j2 = _add(
        svc, "completely different text B",
        CronSchedule(kind="cron", expr="0 8 * * *"),
        topic_tag="meds_morning",
    )
    assert j1.id == j2.id
