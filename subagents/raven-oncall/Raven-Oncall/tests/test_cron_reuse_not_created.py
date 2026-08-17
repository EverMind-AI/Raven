"""A reused job must not be reported as a created one, and a campaign wake must
not stand in as the duplicate of a user reminder.

Measured 2026-08-06, the full chain in four log lines:

    22:34:28  ops_submit scheduled 'ops:cfd-transient:r1' (id 5c500cfb)
    22:34:38  the agent asked cron for a check at 00:34:00 -- 28 seconds from that
              wake, so time-window dedup returned it, and the tool answered
              "Created job 'ops:cfd-transient:r1' (id: 5c500cfb)"
    22:34:42  the agent, holding a job it had not asked for and had apparently just
              created, removed it as a duplicate
    23:31     the solver finished. Nothing was scheduled to look. Twelve hours later
              nothing had.

Two independent faults, so two fixes and two sets of tests: the wording said
"created" for something it found, and a system wake was eligible to be found.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule


def _svc(tmp_path) -> CronService:
    return CronService(tmp_path / "jobs.json", allowed_channels={"tui"})


def _at(minutes_ahead: float) -> CronSchedule:
    when = datetime.now() + timedelta(minutes=minutes_ahead)
    return CronSchedule(kind="at", at_ms=int(when.timestamp() * 1000))


def _tool(svc):
    from raven.proactive_engine.schedulers.cron.tool import CronTool

    tool = CronTool(cron_service=svc)
    tool.set_context("tui", "chat")
    return tool


# --- 系统唤醒不该被当成用户提醒的重复项 -------------------------------------


def test_a_campaign_wake_is_not_returned_as_a_users_duplicate(tmp_path):
    svc = _svc(tmp_path)
    wake = svc.add_job(
        name="ops:cfd-transient:r1",
        schedule=_at(120),
        message="campaign wake",
        channel="tui",
        to="chat",
        dedup=False,
    )

    reminder = svc.add_job(
        name="reminder",
        schedule=_at(120),
        message="check the dambreak run",
        channel="tui",
        to="chat",
    )

    assert reminder.id != wake.id, "a wake and a reminder are different things"
    assert len(svc.list_jobs()) == 2


def test_two_ordinary_reminders_still_dedup(tmp_path):
    """The other direction: the fix must not switch dedup off for real reminders."""
    svc = _svc(tmp_path)
    first = svc.add_job(name="r1", schedule=_at(60), message="take meds", channel="tui", to="chat")

    second = svc.add_job(name="r2", schedule=_at(60.2), message="take meds now", channel="tui", to="chat")

    assert second.id == first.id, "near-simultaneous reminders to one chat still merge"


# --- 复用不能说成创建 -------------------------------------------------------


@pytest.mark.asyncio
async def test_reuse_says_reuse_and_warns_against_deleting_it(tmp_path):
    svc = _svc(tmp_path)
    existing = svc.add_job(name="r1", schedule=_at(45), message="take meds", channel="tui", to="chat")

    out = await _tool(svc).execute(action="add", at=_iso(45.1), message="take meds again")

    assert "Nothing new was created" in out
    assert existing.id in out
    # Wording from this line ("Do not remove it as a duplicate"), which also names
    # the mistake it is warning against. The sentence and the defect behind it came
    # from the CFD line; only the verb differs, and this side's tests pin theirs.
    assert "Do not remove it as a duplicate" in out
    assert "Created job" not in out


@pytest.mark.asyncio
async def test_a_genuinely_new_job_still_says_created(tmp_path):
    svc = _svc(tmp_path)

    out = await _tool(svc).execute(action="add", at=_iso(30), message="something else")

    assert out.startswith("Created job")


def _iso(minutes_ahead: float) -> str:
    return (datetime.now() + timedelta(minutes=minutes_ahead)).strftime("%Y-%m-%dT%H:%M:%S")


# --- 不变量挂在 campaign 字段上,不挂在名字上 -------------------------------


def test_the_invariant_holds_for_a_producer_that_ignores_the_naming_rule(tmp_path):
    """The point of moving the key off the name. A producer that tags its job with
    the campaign is bound by the invariant even if it names the job anything it
    likes -- and five paths create jobs here, two of which do not follow the
    ops:<campaign>: convention at all.
    """
    svc = _svc(tmp_path)
    svc.add_job(
        name="ops:c1:r0", schedule=_at(90), message="wake", channel="tui", to="chat", dedup=False, campaign="c1"
    )

    later = svc.add_job(
        name="something-else-entirely",
        schedule=_at(30),
        message="wake again",
        channel="tui",
        to="chat",
        dedup=False,
        campaign="c1",
    )

    pending = svc.list_jobs()
    assert len(pending) == 1, "one campaign, one pending wake"
    assert pending[0].id == later.id, "the last request is the one that was wanted"


def test_an_untagged_job_is_untouched_by_a_campaign_add(tmp_path):
    """The invariant must not sweep up jobs that have nothing to do with it."""
    svc = _svc(tmp_path)
    reminder = svc.add_job(name="dentist", schedule=_at(200), message="dentist at 5", channel="tui", to="chat")

    svc.add_job(
        name="ops:c1:r0", schedule=_at(90), message="wake", channel="tui", to="chat", dedup=False, campaign="c1"
    )

    assert reminder.id in {j.id for j in svc.list_jobs()}


def test_two_campaigns_keep_one_wake_each(tmp_path):
    svc = _svc(tmp_path)
    a = svc.add_job(name="ops:a:r0", schedule=_at(60), message="a", channel="tui", to="chat", dedup=False, campaign="a")
    b = svc.add_job(name="ops:b:r0", schedule=_at(61), message="b", channel="tui", to="chat", dedup=False, campaign="b")

    ids = {j.id for j in svc.list_jobs()}
    assert ids == {a.id, b.id}, "one campaign replacing another's wake would be worse"


# --- 删掉最后一个待唤醒:告知,不阻拦 ---------------------------------------


@pytest.mark.asyncio
async def test_deleting_a_campaigns_last_wake_says_what_it_costs(tmp_path):
    """Told, not refused. Every step the agent took on 2026-08-06 was right given
    what the tool had said, so the fix is to say the true thing; refusing would
    leave it believing the job was gone and reasoning from there.
    """
    svc = _svc(tmp_path)
    wake = svc.add_job(
        name="ops:c1:r1", schedule=_at(120), message="wake", channel="tui", to="chat", dedup=False, campaign="c1"
    )

    out = await _tool(svc).execute(action="remove", job_id=wake.id)

    assert "last pending wake" in out
    assert "c1" in out
    assert "ops_check_later" in out
    assert svc.list_jobs() == [], "it still goes -- this informs, it does not block"


@pytest.mark.asyncio
async def test_deleting_one_of_two_wakes_says_nothing_extra(tmp_path):
    """The campaign is still watched, so there is nothing to warn about; a notice
    on every delete would be noise and would stop being read."""
    svc = _svc(tmp_path)
    first = svc.add_job(name="ops:c1:r1", schedule=_at(120), message="wake", channel="tui", to="chat", dedup=False)
    svc.add_job(name="ops:c1:recheck", schedule=_at(200), message="later look", channel="tui", to="chat", dedup=False)

    out = await _tool(svc).execute(action="remove", job_id=first.id)

    assert "last pending wake" not in out


@pytest.mark.asyncio
async def test_deleting_an_ordinary_reminder_says_nothing_extra(tmp_path):
    svc = _svc(tmp_path)
    job = svc.add_job(name="dentist", schedule=_at(200), message="dentist at 5", channel="tui", to="chat")

    out = await _tool(svc).execute(action="remove", job_id=job.id)

    assert out == f"Removed job {job.id}"


def test_the_campaign_is_stored_rather_than_parsed_back_out_of_the_name(tmp_path):
    svc = _svc(tmp_path)

    job = svc.add_job(
        name="whatever", schedule=_at(45), message="w", channel="tui", to="chat", dedup=False, campaign="c9"
    )

    assert job.payload.campaign == "c9"
