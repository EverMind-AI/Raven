"""The numbers a campaign watches: the table, the record, and the series.

A campaign could say what it was optimising and nothing about what it was
watching. Measured 2026-08-17: an FEA campaign declared max_penetration as its
target and the ledger held only gpu_minutes_used -- the number the campaign
existed to move was obtained by sending 74 ad-hoc commands at the job directory
and then lived only inside the loop's own prose. Nothing could rank the rounds by
it, nothing could check the parse, and a reader had to go through the log.

Four times to take a reading, and the third is the one nothing had: during a
trial. "It has been running 40 minutes and the timestep is collapsing" cannot be
seen after the fact, which is how one CFD leg spent 152 core-minutes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import readings as r  # noqa: E402


def _meta(**over):
    base = {"connection": "conn_local", "transport": "local", "staged_case": "/srv/case"}
    base.update(over)
    return base


def _table(*rows):
    return [{"name": n, "command": c, "when": w} for n, c, w in rows]


# ---- the table ----


def test_a_table_with_a_bad_time_is_refused_and_names_the_four(tmp_path) -> None:
    problem = r.table_problem(_table(("x", "echo 1", "sometimes")))

    assert problem.startswith("REFUSED")
    for when in r.WHENS:
        assert when in problem


def test_a_reading_with_no_command_is_refused(tmp_path) -> None:
    assert r.table_problem(_table(("x", "", "each_wake"))).startswith("REFUSED")


def test_a_reading_that_changes_something_is_refused(tmp_path) -> None:
    """It is taken again and again -- every wake, or every round -- so whatever it
    changes, it changes that many times, and the record of what was read stops
    being a record of what was there."""
    problem = r.table_problem(_table(("cleanup", "rm -rf {job_dir}/tmp", "after_trial")))

    assert problem.startswith("REFUSED") and "again and again" in problem


def test_a_reading_that_writes_a_file_is_refused(tmp_path) -> None:
    assert r.table_problem(_table(("x", "grep Time log.foam > /tmp/mine.txt", "during_trial"))).startswith("REFUSED")


def test_discarding_stderr_is_not_a_write(tmp_path) -> None:
    """The commands people actually write end in 2>/dev/null, and refusing those
    would make the narrow net useless."""
    assert r.table_problem(_table(("x", "grep -c Time log 2>/dev/null", "during_trial"))) is None
    assert r.table_problem(_table(("y", "ls /data &>/dev/null; echo 1", "each_wake"))) is None


def test_a_job_dir_reading_must_be_taken_inside_a_trial(tmp_path) -> None:
    """Nothing fills {job_dir} in on a wake that is not looking at a round."""
    problem = r.table_problem(_table(("x", "cat {job_dir}/result.json", "each_wake")))

    assert problem.startswith("REFUSED") and "job_dir" in problem


def test_redefining_a_readings_recipe_is_refused_and_asks_for_a_new_name(tmp_path) -> None:
    """Values already recorded under that name came from the old command, and
    nothing in the record would separate them from the new ones."""
    existing = [r.Reading("pen", "awk '/CDIS/{print $3}' job.dat", "after_trial")]

    problem = r.table_problem(_table(("pen", "grep -m1 CDIS job.dat", "after_trial")), existing)

    assert problem.startswith("REFUSED") and "own name" in problem


def test_redeclaring_the_same_recipe_is_fine(tmp_path) -> None:
    existing = [r.Reading("pen", "awk '{print $3}' job.dat", "after_trial")]

    assert r.table_problem(_table(("pen", "awk '{print $3}' job.dat", "after_trial")), existing) is None


def test_merging_keeps_what_was_already_declared(tmp_path) -> None:
    """A declaration that omits the table keeps the one on disk: dropping a reading
    silently would end a series a later round is still comparing against."""
    existing = [r.Reading("pen", "cmd-a", "after_trial")]

    assert r.merge(existing, None) == [{"name": "pen", "command": "cmd-a", "when": "after_trial"}]
    assert len(r.merge(existing, _table(("drops", "cmd-b", "after_trial")))) == 2


# ---- taking one ----


@pytest.mark.asyncio
async def test_a_number_is_recorded_as_a_number(tmp_path) -> None:
    meta = _meta(readings=_table(("free", "echo 41.5", "each_wake")))

    taken = await r.take(meta, tmp_path, r.EACH_WAKE, runner=lambda cmd: (0, "41.5\n"))

    assert taken[0]["value"] == 41.5
    assert r.read(tmp_path)[0]["value"] == 41.5


@pytest.mark.asyncio
async def test_a_table_of_things_is_one_value(tmp_path) -> None:
    """The design's "several values at one moment": a command that prints a whole
    listing is a reading, and judging it is the loop's job, not this layer's."""
    meta = _meta(readings=_table(("jobs", "curl .../postings", "each_wake")))
    body = '[{"id": 41, "req": "COBOL"}, {"id": 42, "req": "Go"}]'

    taken = await r.take(meta, tmp_path, r.EACH_WAKE, runner=lambda cmd: (0, body))

    assert taken[0]["value"] == [{"id": 41, "req": "COBOL"}, {"id": 42, "req": "Go"}]


@pytest.mark.asyncio
async def test_a_command_that_fails_is_recorded_as_what_happened(tmp_path) -> None:
    """Not as a zero. A reading that could not be taken and one that read nought
    are different facts, and the second is the one a loop acts on freely."""
    meta = _meta(readings=_table(("pen", "cat job.dat", "each_wake")))

    taken = await r.take(meta, tmp_path, r.EACH_WAKE, runner=lambda cmd: (1, "No such file"))

    assert "value" not in taken[0]
    assert "No such file" in taken[0]["error"]


@pytest.mark.asyncio
async def test_the_job_directory_is_filled_in(tmp_path) -> None:
    seen: list[str] = []
    meta = _meta(readings=_table(("pen", "awk '{print}' {job_dir}/job.dat", "after_trial")))

    await r.take(
        meta,
        tmp_path,
        r.AFTER_TRIAL,
        job_dir="/srv/rounds/jobs/r0",
        trial="r0",
        runner=lambda cmd: (seen.append(cmd), (0, "1.0"))[1],
    )

    assert seen == ["awk '{print}' /srv/rounds/jobs/r0/job.dat"]


@pytest.mark.asyncio
async def test_only_the_readings_due_now_are_taken(tmp_path) -> None:
    meta = _meta(readings=_table(("a", "echo 1", "each_wake"), ("b", "echo 2", "after_trial")))

    taken = await r.take(meta, tmp_path, r.EACH_WAKE, runner=lambda cmd: (0, "1"))

    assert [t["name"] for t in taken] == ["a"]


@pytest.mark.asyncio
async def test_an_unreachable_machine_is_recorded_rather_than_raised(tmp_path) -> None:
    meta = _meta(readings=_table(("a", "echo 1", "each_wake")))

    def _dead(cmd):
        raise OSError("host unreachable")

    taken = await r.take(meta, tmp_path, r.EACH_WAKE, runner=_dead)

    assert "unreachable" in taken[0]["error"]


# ---- reading it back ----


@pytest.mark.asyncio
async def test_the_series_is_kept_oldest_first(tmp_path) -> None:
    """F2's four rounds read -70.88, -0.44, -10.47 and 9.9e-07. The fact worth
    having is that the sequence is not monotone, and no single point carries it."""
    meta = _meta(readings=_table(("pen", "echo", "after_trial")))
    for value, trial in ((-70.88, "r0"), (-0.44, "r1"), (-10.47, "r2"), (9.9e-07, "r3")):
        await r.take(meta, tmp_path, r.AFTER_TRIAL, trial=trial, runner=lambda cmd, v=value: (0, str(v)))

    got = [row["value"] for row in r.series(tmp_path)["pen"]]

    assert got == [-70.88, -0.44, -10.47, 9.9e-07]


@pytest.mark.asyncio
async def test_the_starting_value_is_the_one_taken_at_declare(tmp_path) -> None:
    meta = _meta(readings=_table(("volt", "echo", "at_declare")))
    await r.take(meta, tmp_path, r.AT_DECLARE, runner=lambda cmd: (0, "248.42"))

    assert r.baseline(tmp_path) == {"volt": 248.42}


@pytest.mark.asyncio
async def test_an_after_trial_reading_is_taken_once_per_trial(tmp_path) -> None:
    """A job directory that has been cleaned up fails the same way on every look,
    and retrying it would write one error per wake forever."""
    meta = _meta(readings=_table(("pen", "echo", "after_trial")))
    await r.take(meta, tmp_path, r.AFTER_TRIAL, trial="r0", runner=lambda cmd: (0, "1.0"))

    assert r.taken_for(tmp_path, "r0") == {"pen"}
    assert r.taken_for(tmp_path, "r1") == set()


@pytest.mark.asyncio
async def test_a_during_trial_reading_is_meant_to_repeat(tmp_path) -> None:
    """The repetition IS the series -- that is the whole point of during_trial --
    so it must not be filtered out by having been taken once."""
    meta = _meta(readings=_table(("deltaT", "echo", "during_trial")))
    for value in ("1e-5", "3.2e-9", "3e-19"):
        await r.take(meta, tmp_path, r.DURING_TRIAL, trial="r0", runner=lambda cmd, v=value: (0, v))

    assert r.taken_for(tmp_path, "r0") == set(), "not an after-trial reading"
    assert len(r.series(tmp_path)["deltaT"]) == 3


def test_declaring_the_same_name_twice_is_refused_and_says_why(tmp_path) -> None:
    """The obvious way to ask for a baseline -- the same quantity at_declare and
    each_wake -- silently does not work: the table is keyed by name, so the second
    row replaces the first and the starting point is never taken. It is not needed
    either, which is what the refusal has to say."""
    problem = r.table_problem(_table(("volt", "curl -s x", "at_declare"), ("volt", "curl -s x", "each_wake")))

    assert problem.startswith("REFUSED")
    assert "taken once while the campaign is declared" in problem
