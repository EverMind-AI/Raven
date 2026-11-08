"""Tests for CronService.add_job dedup layers.

Three layers, applied in order at add time, first match wins:
  1. L7 topic_tag dedup — strictest, runs first when caller supplies tag.
  2. Message-equal dedup — byte-identical payload.message + same channel/to
     + a fire still ahead.
  3. Schedule dedup — same schedule slot + same channel/to + byte-identical
     message, for a job whose fire has passed or was cleared (a shared
     topic_tag is settled by layer 1).

Dedup keys on explicit identity only. Nearby fire times are not identity:
a former 15-minute time-window layer silently dropped a distinct reminder
whose fire was close to an existing job's (issue #161) and was removed --
see the tests at the bottom pinning that distinct nearby reminders are kept.
An identical schedule slot is not identity either: two adds on one slot are
the same reminder only when the message or topic_tag says so, otherwise the
second is stored beside the first instead of overwriting it.

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


def _add(svc, msg, schedule, *, channel="tui", to="direct", topic_tag=None):
    return svc.add_job(
        name=msg[:30],
        schedule=schedule,
        message=msg,
        channel=channel,
        to=to,
        topic_tag=topic_tag,
    )


def test_topic_tag_dedup_same_topic_returns_existing(svc):
    """Two add_job calls with the same topic_tag share the same job id."""
    j1 = _add(
        svc,
        "妈妈吃多奈哌齐 5mg (中午)",
        CronSchedule(kind="cron", expr="20 11 * * *"),
        topic_tag="medication_noon",
    )
    j2 = _add(
        svc,
        "妈妈吃多奈哌齐 5mg (中午, 妈妈刚说想吃)",
        CronSchedule(kind="cron", expr="30 11 * * *"),
        topic_tag="medication_noon",
    )
    assert j1.id == j2.id, "same topic_tag must dedup to single job"


def test_topic_tag_dedup_updates_message_in_place(svc):
    """Second add_job with same topic_tag overwrites message + schedule."""
    j1 = _add(
        svc,
        "old message",
        CronSchedule(kind="cron", expr="0 11 * * *"),
        topic_tag="medication_noon",
    )
    j2 = _add(
        svc,
        "new message",
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
        svc,
        "msg A",
        CronSchedule(kind="cron", expr="0 9 * * *"),
        channel="tui",
        to="alice",
        topic_tag="exercise",
    )
    j2 = _add(
        svc,
        "msg B",
        CronSchedule(kind="cron", expr="0 9 * * *"),
        channel="feishu",
        to="bob",
        topic_tag="exercise",
    )
    assert j1.id != j2.id, "different channel/to must NOT dedup"


def test_topic_tag_dedup_skipped_when_no_tag(svc):
    """Without topic_tag, falls through to message-equal / schedule dedup."""
    j1 = _add(
        svc,
        "stretch reminder",
        CronSchedule(kind="cron", expr="0 9 * * *"),
        topic_tag=None,
    )
    j2 = _add(
        svc,
        "stretch reminder",  # identical message → message-equal dedup
        CronSchedule(kind="cron", expr="30 14 * * *"),  # different time
        topic_tag=None,
    )
    # Message-equal dedup should still catch this, but the path matters —
    # this test pins that no-tag is a legitimate fallthrough.
    assert j1.id == j2.id


def test_topic_tag_dedup_distinct_tags_keep_separate(svc):
    """Different topic_tag values on same channel/to → two distinct jobs."""
    j1 = _add(
        svc,
        "ms morning meds",
        CronSchedule(kind="cron", expr="0 7 * * *"),
        topic_tag="medication_morning",
    )
    j2 = _add(
        svc,
        "ms noon meds",
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
        svc,
        "OKR review reminder",
        CronSchedule(kind="cron", expr="0 9 * * 1"),  # weekly Monday
        topic_tag="okr_quarterly",
    )
    j2 = _add(
        svc,
        "OKR review reminder (urgent)",
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
        svc,
        "wholly different text A",
        CronSchedule(kind="cron", expr="0 8 * * *"),
        topic_tag="meds_morning",
    )
    j2 = _add(
        svc,
        "completely different text B",
        CronSchedule(kind="cron", expr="0 8 * * *"),
        topic_tag="meds_morning",
    )
    assert j1.id == j2.id


def test_schedule_dedup_updates_in_place_once_the_job_has_fired(svc):
    """The commonest re-registration path updates rather than duplicates.

    A plain re-add of the same recurring schedule/channel/to carries no
    topic_tag, so it lands on ``_find_duplicate_schedule`` rather than the
    topic_tag branch. Reaching it needs the existing job's ``next_run_at_ms``
    cleared: with a fire still scheduled the message-equal layer returns the
    job as it stands, and this layer keeps its scheduled fire instead of
    re-enabling or recomputing -- that is the state this layer is written
    for -- see its "already fired or was disabled" comment. The re-add
    repeats the message, because a same-slot add with different content is
    a distinct reminder and now gets a job of its own.

    Kept when the flag it was originally written for was retired, because its
    setup is the only thing in the suite that reaches the re-enable and the
    next_run recompute.
    """
    sched = CronSchedule(kind="every", every_ms=60_000)
    first = _add(svc, "water", sched)
    # Persisted, not just set: add_job reloads the store under its lock
    # (``self._store = None``) on every call, so an in-memory edit is read back
    # over and the assertions below would land on the wrong branch.
    first.state.next_run_at_ms = None
    svc._save_store()

    again = _add(svc, "water", sched)

    assert again.id == first.id, "should have updated in place via schedule dedup"
    assert again.payload.message == "water", "the in-place update keeps the reminder text"
    assert again.enabled is True, "a job that had fired is re-enabled by the update"
    assert again.state.next_run_at_ms is not None, "the recompute has to give it a next fire"
    assert svc.list_jobs()[0].payload.message == "water"


def test_distinct_nearby_oneshots_are_kept(svc):
    """Two one-shots for the same recipient ten minutes apart with different
    messages are distinct reminders, not duplicates (issue #161).

    The removed 15-minute time-window layer returned the first job here, so
    the second reminder was silently dropped while the caller saw a
    successful-looking add."""
    base = 2_000_000_000_000
    j1 = _add(svc, "prepare the daily brief", CronSchedule(kind="at", at_ms=base))
    j2 = _add(
        svc,
        "review the presentation",
        CronSchedule(kind="at", at_ms=base + 10 * 60 * 1000),
    )

    assert j1.id != j2.id, "distinct messages must mint distinct jobs, however close the fires"
    stored = svc.list_jobs()
    assert {j.payload.message for j in stored} == {
        "prepare the daily brief",
        "review the presentation",
    }


def test_distinct_nearby_cross_kind_reminders_are_kept(svc):
    """A recurring reminder and a same-fire one-shot with different messages
    both survive: close fires across kinds are not identity either."""
    j1 = _add(svc, "take the morning meds", CronSchedule(kind="cron", expr="0 8 * * *"))
    assert j1.state.next_run_at_ms is not None
    j2 = _add(
        svc,
        "log the morning weight",
        CronSchedule(kind="at", at_ms=j1.state.next_run_at_ms + 5 * 60 * 1000),
    )

    assert j1.id != j2.id
    assert len(svc.list_jobs()) == 2


def test_identical_message_oneshot_still_dedups_when_close(svc):
    """Byte-identical messages still dedup even with fires minutes apart —
    removing the time-window layer must not weaken the explicit layers."""
    base = 2_000_000_000_000
    j1 = _add(svc, "water the plants", CronSchedule(kind="at", at_ms=base))
    j2 = _add(svc, "water the plants", CronSchedule(kind="at", at_ms=base + 5 * 60 * 1000))

    assert j1.id == j2.id
    assert len(svc.list_jobs()) == 1


# ── Distance zero: same schedule slot, content decides identity ──────
# Two adds that land on the *same* schedule are still two reminders when
# their content differs. The schedule layer is the identity check's last
# line, so a same-slot add has to keep both jobs; only an identical
# message (or a shared topic_tag) is the same reminder at that distance.
# These read the store back through a second CronService so the assertion
# is on what was persisted, not on the returned object.


def _stored(svc: CronService) -> list:
    return CronService(svc.store_path).list_jobs()


def test_same_cron_expr_distinct_messages_are_kept(svc):
    first = _add(svc, "send the weekly report", CronSchedule(kind="cron", expr="0 9 * * 1"))
    second = _add(svc, "water the office plants", CronSchedule(kind="cron", expr="0 9 * * 1"))

    assert first.id != second.id, "different messages on one slot are two reminders"
    stored = _stored(svc)
    assert len(stored) == 2
    assert {j.payload.message for j in stored} == {
        "send the weekly report",
        "water the office plants",
    }
    assert {j.id for j in stored} == {first.id, second.id}


def test_same_at_instant_distinct_messages_are_kept(svc):
    instant = 2_000_000_000_000
    first = _add(svc, "call the dentist", CronSchedule(kind="at", at_ms=instant))
    second = _add(svc, "pay the electricity bill", CronSchedule(kind="at", at_ms=instant))

    assert first.id != second.id, "two one-shots on the same instant are two reminders"
    stored = _stored(svc)
    assert len(stored) == 2
    assert {j.payload.message for j in stored} == {
        "call the dentist",
        "pay the electricity bill",
    }


def test_same_every_seconds_distinct_messages_are_kept(svc):
    every = CronSchedule(kind="every", every_ms=600_000)
    first = _add(svc, "check the deploy", every)
    second = _add(svc, "check the queue depth", every)

    assert first.id != second.id, "one cadence can carry two different reminders"
    stored = _stored(svc)
    assert len(stored) == 2
    assert {j.payload.message for j in stored} == {"check the deploy", "check the queue depth"}


def test_same_slot_identical_message_stays_one_job(svc):
    every = CronSchedule(kind="every", every_ms=600_000)
    first = _add(svc, "drink water", every)
    second = _add(svc, "drink water", every)

    assert first.id == second.id, "the same reminder on the same slot stays one job"
    stored = _stored(svc)
    assert len(stored) == 1
    assert stored[0].payload.message == "drink water"


def test_same_slot_shared_topic_tag_updates_in_place(svc):
    instant = 2_000_000_000_000
    first = _add(
        svc,
        "take the noon meds",
        CronSchedule(kind="at", at_ms=instant),
        topic_tag="medication_noon",
    )
    second = _add(
        svc,
        "take the noon meds (double dose)",
        CronSchedule(kind="at", at_ms=instant),
        topic_tag="medication_noon",
    )

    assert first.id == second.id, "a shared topic_tag is identity, even at distance zero"
    stored = _stored(svc)
    assert len(stored) == 1
    assert stored[0].payload.message == "take the noon meds (double dose)"


def test_same_slot_identical_message_reuses_an_overdue_job(svc):
    """The message-equal layer only answers while a fire is still ahead, so an
    overdue job is matched by the schedule layer: the re-add reuses it and
    leaves its pending fire where it was."""
    sched = CronSchedule(kind="cron", expr="0 8 * * *")
    first = _add(svc, "take the morning meds", sched)
    overdue = svc._now_ms() - 60_000
    first.state.next_run_at_ms = overdue
    svc._save_store()

    again = _add(svc, "take the morning meds", sched)

    assert again.id == first.id, "the same reminder on the same slot stays one job"
    stored = _stored(svc)
    assert len(stored) == 1
    assert stored[0].state.next_run_at_ms == overdue, "the overdue fire must stay pending"


def test_same_cron_expr_in_another_zone_is_another_slot(svc):
    """A cron expression names a wall-clock time, so the same expression in
    another zone fires at another instant: the schedule layer must not reuse
    the existing job for it."""
    first = _add(svc, "take the morning meds", CronSchedule(kind="cron", expr="0 8 * * *", tz="Asia/Shanghai"))
    first.state.next_run_at_ms = None
    svc._save_store()

    again = _add(svc, "take the morning meds", CronSchedule(kind="cron", expr="0 8 * * *", tz="America/Vancouver"))

    assert again.id != first.id
    assert sorted(j.schedule.tz for j in _stored(svc)) == ["America/Vancouver", "Asia/Shanghai"]


@pytest.mark.parametrize("gap_minutes", [5, 10])
def test_same_kind_distinct_messages_minutes_apart_are_kept(svc, gap_minutes):
    base = 2_000_000_000_000
    first = _add(svc, "prepare the daily brief", CronSchedule(kind="at", at_ms=base))
    second = _add(
        svc,
        "review the presentation",
        CronSchedule(kind="at", at_ms=base + gap_minutes * 60 * 1000),
    )

    assert first.id != second.id
    stored = _stored(svc)
    assert len(stored) == 2
    assert {j.payload.message for j in stored} == {
        "prepare the daily brief",
        "review the presentation",
    }


# ── add_job_with_outcome: what each layer reports ────────────────────────


def test_outcome_created_for_a_fresh_add(svc):
    res = svc.add_job_with_outcome(
        name="fresh", schedule=CronSchedule(kind="cron", expr="0 9 * * *"), message="fresh", channel="tui", to="direct"
    )
    assert res.outcome == "created"
    assert res.schedule_replaced is True
    assert res.schedule_matches is True
    assert res.job.id


def test_outcome_skipped_when_an_identical_message_is_still_ahead(svc):
    """The message-equal layer writes nothing: the returned job is the one that
    matched, and the requested schedule was not applied."""
    first = _add(svc, "take the pill", CronSchedule(kind="cron", expr="0 8 * * *"))
    res = svc.add_job_with_outcome(
        name="take the pill",
        schedule=CronSchedule(kind="cron", expr="0 15 * * *"),
        message="take the pill",
        channel="tui",
        to="direct",
    )
    assert res.outcome == "skipped"
    assert res.schedule_replaced is False
    assert res.schedule_matches is False
    assert res.job.id == first.id
    assert res.job.schedule.expr == "0 8 * * *", "the requested schedule was not applied"
    assert [j.schedule.expr for j in svc.list_jobs()] == ["0 8 * * *"], "nothing was written"


def test_outcome_skipped_still_reports_a_matching_schedule(svc):
    """A byte-identical message on the *same* schedule also skips at layer 2,
    but nothing needs applying: the stored schedule is the requested one, so the
    outcome has to say so instead of claiming the request was dropped."""
    first = _add(svc, "take the pill", CronSchedule(kind="cron", expr="0 8 * * *"))
    res = svc.add_job_with_outcome(
        name="take the pill",
        schedule=CronSchedule(kind="cron", expr="0 8 * * *"),
        message="take the pill",
        channel="tui",
        to="direct",
    )
    assert res.outcome == "skipped"
    assert res.schedule_replaced is False
    assert res.schedule_matches is True
    assert res.job.id == first.id
    assert [j.id for j in svc.list_jobs()] == [first.id], "nothing was written"


def test_outcome_skipped_in_another_zone_is_not_a_matching_schedule(svc):
    """The same cron expression in another zone fires at another instant, so a
    skip on it must not report the requested schedule as already in effect."""
    first = _add(svc, "take the pill", CronSchedule(kind="cron", expr="0 8 * * *", tz="Asia/Shanghai"))
    res = svc.add_job_with_outcome(
        name="take the pill",
        schedule=CronSchedule(kind="cron", expr="0 8 * * *", tz="America/Vancouver"),
        message="take the pill",
        channel="tui",
        to="direct",
    )
    assert res.outcome == "skipped"
    assert res.schedule_matches is False
    assert res.job.id == first.id
    assert res.job.schedule.tz == "Asia/Shanghai", "the stored zone stays"


def test_outcome_updated_with_schedule_replaced_for_topic_tag(svc):
    first = _add(svc, "old", CronSchedule(kind="cron", expr="0 11 * * *"), topic_tag="meds")
    res = svc.add_job_with_outcome(
        name="new",
        schedule=CronSchedule(kind="cron", expr="30 13 * * *"),
        message="new",
        channel="tui",
        to="direct",
        topic_tag="meds",
    )
    assert res.outcome == "updated"
    assert res.schedule_replaced is True
    assert res.schedule_matches is True
    assert res.job.id == first.id
    assert res.job.schedule.expr == "30 13 * * *"


def test_outcome_updated_keeping_schedule_for_a_same_slot_reuse(svc):
    """A byte-identical message on an identical schedule reuses the job once
    its fire has passed; the schedule is kept, not replaced."""
    sched = CronSchedule(kind="every", every_ms=60_000)
    first = _add(svc, "water", sched)
    first.state.next_run_at_ms = None
    svc._save_store()
    res = svc.add_job_with_outcome(name="water again", schedule=sched, message="water", channel="tui", to="direct")
    assert res.outcome == "updated"
    assert res.schedule_replaced is False
    assert res.schedule_matches is True
    assert res.job.id == first.id
    assert res.job.name == "water again", "the name follows the request"
    assert res.job.schedule.every_ms == 60_000


def test_outcome_updated_with_schedule_replaced_for_a_job_id_edit(svc):
    first = _add(svc, "before", CronSchedule(kind="cron", expr="0 9 * * *"))
    res = svc.add_job_with_outcome(
        name="after",
        schedule=CronSchedule(kind="cron", expr="30 10 * * *"),
        message="after",
        channel="tui",
        to="direct",
        job_id=first.id,
    )
    assert res.outcome == "updated"
    assert res.schedule_replaced is True
    assert res.schedule_matches is True
    assert res.job.id == first.id


def test_add_job_still_returns_just_the_job():
    """The plain entry keeps its signature: every existing caller reads a
    CronJob off it."""
    svc = CronService(Path(__import__("tempfile").mkdtemp()) / "jobs.json")
    job = svc.add_job(name="daily", schedule=CronSchedule(kind="cron", expr="0 9 * * *"), message="daily")
    assert job.name == "daily"
    assert not hasattr(job, "outcome")
