"""One pending wake per campaign, keyed on a field rather than on the job's name.

The invariant used to key on the ``ops:<campaign>:`` name prefix, which binds only
the producers willing to name their jobs that way. Five paths create jobs here --
the ops tools, the cron tool, the CLI, the Sentinel executor, the re-arm -- and
the strength of an invariant keyed on a string each producer fills in is the
strength of the least cooperative one. Both breakages measured on 2026-08-06 came
from producers outside the naming rule.

Shape taken from the parallel CFD line, which shipped it first; the tests here are
this repo's, including the round-trip one, since a field that does not survive a
write and a read back is exactly as good as no field at all (``add_job`` reloads
the store before it checks).
"""

from __future__ import annotations

from pathlib import Path

from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule

CAMPAIGN = "armb-embed-r15e"


def _svc(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "jobs.json", allowed_channels={"tui"})


def _add(svc: CronService, name: str, *, campaign: str | None = None, offset_ms: int = 600_000, **kw):
    return svc.add_job(
        name=name,
        schedule=CronSchedule(kind="at", at_ms=svc._now_ms() + offset_ms),
        message=kw.pop("message", name),
        deliver=True,
        channel="tui",
        to="default",
        delete_after_run=True,
        campaign=campaign,
        **kw,
    )


def test_the_invariant_holds_for_a_producer_that_ignores_the_naming_rule(tmp_path: Path) -> None:
    """The point of moving the key off the name: a producer free to call its job
    anything is still bound."""
    svc = _svc(tmp_path)
    _add(svc, f"ops:{CAMPAIGN}:r0", campaign=CAMPAIGN)

    later = _add(svc, "something else entirely", campaign=CAMPAIGN, offset_ms=900_000)

    assert [j.id for j in svc.list_jobs()] == [later.id]


def test_the_field_survives_a_write_and_a_read_back(tmp_path: Path) -> None:
    """add_job reloads the store before checking, so a field lost in
    serialisation leaves the invariant reading None on every prior job and
    matching nothing -- behaviour identical to not having done the work."""
    _add(_svc(tmp_path), f"ops:{CAMPAIGN}:r0", campaign=CAMPAIGN)

    reopened = _svc(tmp_path)

    assert [j.payload.campaign for j in reopened.list_jobs()] == [CAMPAIGN]


def test_an_unrelated_reminder_is_not_swept_up(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    _add(svc, "water the plants")

    _add(svc, f"ops:{CAMPAIGN}:r0", campaign=CAMPAIGN, offset_ms=900_000, dedup=False)

    assert sorted(j.name for j in svc.list_jobs()) == [f"ops:{CAMPAIGN}:r0", "water the plants"]


def test_two_campaigns_keep_one_pending_wake_each(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    _add(svc, f"ops:{CAMPAIGN}:r0", campaign=CAMPAIGN, dedup=False)
    _add(svc, "ops:cfd-transient:r0", campaign="cfd-transient", offset_ms=700_000, dedup=False)

    _add(svc, f"ops:{CAMPAIGN}:r1", campaign=CAMPAIGN, offset_ms=900_000, dedup=False)

    assert sorted(j.name for j in svc.list_jobs()) == [f"ops:{CAMPAIGN}:r1", "ops:cfd-transient:r0"]


def test_a_campaign_wake_is_never_returned_as_a_reminders_duplicate(tmp_path: Path) -> None:
    """The 2026-08-06 chain started here. dedup=False keeps a wake from being
    merged INTO an existing job; nothing kept it from being handed back as the
    duplicate of a later reminder, under the word "Created". The turn removed it
    as its own duplicate and the campaign was left with nothing pending."""
    svc = _svc(tmp_path)
    wake = _add(svc, f"ops:{CAMPAIGN}:r0", campaign=CAMPAIGN, dedup=False)

    reminder = _add(svc, "check on the run", offset_ms=600_000 + 28_000)

    assert reminder.id != wake.id
    assert len(svc.list_jobs()) == 2


def test_removing_the_last_wake_names_the_campaign_left_unwatched(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    wake = _add(svc, f"ops:{CAMPAIGN}:r0", campaign=CAMPAIGN)

    assert svc.sole_pending_wake_for(wake.id) == CAMPAIGN


def test_removing_one_of_two_wakes_names_nothing(tmp_path: Path) -> None:
    """Only the last one leaves the campaign unwatched, and a note on every
    removal would be noise that the one that matters hides in."""
    svc = _svc(tmp_path)
    first = _add(svc, f"ops:{CAMPAIGN}:r0", campaign=CAMPAIGN, dedup=False)
    _add(svc, f"ops:{CAMPAIGN}:recheck", offset_ms=900_000, dedup=False)

    assert svc.sole_pending_wake_for(first.id) is None


def test_removing_an_ordinary_reminder_names_nothing(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    plants = _add(svc, "water the plants")

    assert svc.sole_pending_wake_for(plants.id) is None


def test_a_pre_field_wake_is_still_recognised_by_its_name(tmp_path: Path) -> None:
    """Jobs written before the field existed are still pending on disk somewhere,
    and they carry the prefix; dropping the fallback would leave them unbound
    until they fire."""
    svc = _svc(tmp_path)
    legacy = _add(svc, f"ops:{CAMPAIGN}:r0", dedup=False)

    assert svc.sole_pending_wake_for(legacy.id) == CAMPAIGN
