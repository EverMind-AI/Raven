"""What a campaign's own record says about the watch it kept.

Two callers read these numbers and neither can get them anywhere else: a budget
metered in wall-clock or in looks has no host to ask, and a delivered watch whose
correct outcome was to do nothing is indistinguishable from one that never looked
unless the count comes off the trail.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from raven.ops.attendance import attendance, off_machine_spend
from raven.ops.budget import Budget, COMPUTE, LOOKS, WALL_CLOCK
from raven.ops.instrument import log_event
from raven.ops.state_claims import StateFacts, write_facts


def _declared(tmp_path, **extra):
    cdir = tmp_path / "watch-volt"
    cdir.mkdir()
    (cdir / "meta.json").write_text(json.dumps({"connection": "c", **extra}), encoding="utf-8")
    return cdir


def test_a_campaign_that_has_never_been_read_reports_no_looks(tmp_path) -> None:
    assert attendance(_declared(tmp_path)).looks == 0


def test_a_missing_campaign_directory_reports_nothing_rather_than_raising(tmp_path) -> None:
    """Read on the delivery path, where a crash would cost the report."""
    kept = attendance(tmp_path / "never-declared")

    assert kept.looks == 0 and kept.wakes == 0 and kept.minutes_open is None


def test_looks_come_from_the_probe_counter(tmp_path) -> None:
    """The counter the status tool advances when it reads the world. A wake that
    read nothing did not look, so turns are the wrong thing to count."""
    cdir = _declared(tmp_path)
    write_facts(cdir, StateFacts(probe_seq=7))

    assert attendance(cdir).looks == 7


def test_wakes_count_the_times_it_arranged_to_come_back(tmp_path) -> None:
    cdir = _declared(tmp_path)
    log_event(cdir, "check_later", eta_seconds=600)
    log_event(cdir, "wake_scheduled", round_due=0)
    log_event(cdir, "note", text="not a wake")

    assert attendance(cdir).wakes == 2


def test_the_watch_opened_when_it_was_declared(tmp_path) -> None:
    opened = datetime.now() - timedelta(minutes=90)
    cdir = _declared(tmp_path, declared_at=opened.isoformat(timespec="seconds"))

    kept = attendance(cdir)

    assert kept.opened_at is not None
    assert 89 <= (kept.minutes_open or 0) <= 91


def test_an_amended_declaration_does_not_restart_the_clock(tmp_path) -> None:
    """Re-declaring after a round 0 that died on a shell mismatch is the same
    watch. If the file's mtime were the reading, every amendment would hand back
    the wall-clock budget already spent."""
    opened = datetime.now() - timedelta(hours=3)
    cdir = _declared(tmp_path, declared_at=opened.isoformat(timespec="seconds"))

    assert (attendance(cdir).minutes_open or 0) > 175


def test_a_wall_clock_budget_is_spent_by_the_clock(tmp_path) -> None:
    opened = datetime.now() - timedelta(minutes=30)
    cdir = _declared(tmp_path, declared_at=opened.isoformat(timespec="seconds"))

    spent = off_machine_spend(cdir, Budget("minute", 120.0, meter=WALL_CLOCK))

    assert spent is not None and 29 <= spent <= 31


def test_a_look_budget_is_spent_by_looking(tmp_path) -> None:
    cdir = _declared(tmp_path)
    write_facts(cdir, StateFacts(probe_seq=12))

    assert off_machine_spend(cdir, Budget("look", 40.0, meter=LOOKS)) == 12.0


def test_a_compute_budget_is_not_answered_here_and_is_not_zero(tmp_path) -> None:
    """Zero would read as "nothing spent yet", which is the state a loop acts on
    most freely. This function does not hold that reading; the host does."""
    assert off_machine_spend(_declared(tmp_path), Budget("core-minute", 150.0, meter=COMPUTE)) is None
