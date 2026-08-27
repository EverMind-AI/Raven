"""What a campaign may spend, and how concurrent use adds up.

The unit is the campaign's to declare and this layer never interprets it. It used
to price everything in GPU minutes, so a CFD campaign inherited a field named
budget_minutes_total whose value meant core-minutes -- one name, two quantities,
indistinguishable to every reader including the loop.

The accumulation rule is declared for the same reason it exists: it follows the
unit, not the backend. Occupancy of one device counts an overlap once; work done
on cores counts every core. Putting that in each backend would mean a new
accumulator per platform, when what differs is one word.
"""

from __future__ import annotations

import pytest

from raven.ops.budget import (
    ADDITIVE,
    COMPUTE,
    LOOKS,
    SHARED,
    WALL_CLOCK,
    Budget,
    accumulate,
    from_meta,
)

MINUTE = 60.0


def test_a_declared_budget_is_read_as_written() -> None:
    b = from_meta({"budget": {"unit": "licence-hour", "total": 12, "overlap": "additive"}})

    assert b == Budget(unit="licence-hour", total=12.0, overlap=ADDITIVE)


def test_an_unknown_overlap_rule_falls_back_to_shared_rather_than_guessing() -> None:
    """Charging an overlap once is the milder error of the two: it under-counts a
    campaign that should have been billed per job, where the other direction stops
    a campaign that still had budget."""
    assert from_meta({"budget": {"unit": "u", "total": 1, "overlap": "sideways"}}).overlap == SHARED


def test_the_gpu_minute_field_still_reads(tmp_path=None) -> None:
    """r11 through r16 are on disk with this field. A campaign's meta records the
    device it ran under, so it is read, not rewritten."""
    assert from_meta({"budget_minutes_total": 140}) == Budget("gpu-minute", 140.0, SHARED)


def test_the_core_minute_field_reads_as_additive() -> None:
    """The CFD line's field, and the reason the rule cannot be global: eight cores
    and eight more for a minute is sixteen core-minutes."""
    assert from_meta({"budget_core_minutes_total": 202}) == Budget("core-minute", 202.0, ADDITIVE)


def test_the_new_field_wins_over_a_legacy_one_left_beside_it() -> None:
    meta = {"budget": {"unit": "u", "total": 5}, "budget_minutes_total": 140}

    assert from_meta(meta).total == 5.0


def test_the_core_minute_field_wins_when_a_campaign_carries_both() -> None:
    """Precedence taken from the CFD factory, which read it that way because that
    line inherited the GPU field name while meaning core-minutes. A campaign with
    both is one of theirs, and reading it as GPU minutes would price a decomposed
    solver as though the cores were free."""
    meta = {"budget_core_minutes_total": 202, "budget_minutes_total": 140}

    assert from_meta(meta) == Budget("core-minute", 202.0, ADDITIVE)


def test_no_budget_is_none_and_not_zero() -> None:
    """Plenty of work has no budget, and a default nobody chose reads exactly like
    one the operator set. Requiring every domain to declare one is the same
    mistake facing the other way."""
    assert from_meta({"host": "h"}) is None


def test_a_malformed_total_is_no_budget_rather_than_a_wrong_one() -> None:
    assert from_meta({"budget": {"unit": "u", "total": "soon"}}) is None


def test_the_machine_holds_the_reading_unless_the_campaign_says_otherwise() -> None:
    """Every campaign on disk was metered by the host, and none of them says so."""
    assert from_meta({"budget": {"unit": "core-minute", "total": 150}}).meter == COMPUTE
    assert from_meta({"budget_minutes_total": 140}).meter == COMPUTE


def test_a_watch_declares_what_measures_it() -> None:
    """A campaign sitting on a price feed runs no jobs, so the host's answer for
    what it has spent is zero however long it has been watching."""
    wall = from_meta({"budget": {"unit": "minute", "total": 240, "meter": "wall-clock"}})
    looks = from_meta({"budget": {"unit": "look", "total": 40, "meter": "look"}})

    assert (wall.meter, wall.off_machine) == (WALL_CLOCK, True)
    assert (looks.meter, looks.off_machine) == (LOOKS, True)
    assert from_meta({"budget": {"unit": "core-minute", "total": 150}}).off_machine is False


def test_an_unknown_meter_falls_back_to_the_host_rather_than_guessing() -> None:
    assert from_meta({"budget": {"unit": "u", "total": 1, "meter": "vibes"}}).meter == COMPUTE


def test_the_meter_is_never_inferred_from_the_unit() -> None:
    """"minute" is a wall-clock minute on a watch and a core-minute on a solver
    campaign. Guessing puts a spend nobody measured against a total somebody set."""
    assert from_meta({"budget": {"unit": "minute", "total": 240}}).meter == COMPUTE


def test_additive_pays_for_every_span() -> None:
    spans = [(0.0, 10 * MINUTE, 8.0), (0.0, 10 * MINUTE, 8.0)]

    assert accumulate(spans, overlap=ADDITIVE) == pytest.approx(160.0)


def test_shared_counts_an_overlap_once() -> None:
    """One device, two jobs, twenty minutes of wall clock: the card was busy for
    twenty minutes, not forty. Measured on r11, where summing charged 39.4 across
    20 minutes and the campaign ran to 152 of a 140 budget."""
    spans = [(0.0, 20 * MINUTE, 1.0), (0.0, 20 * MINUTE, 1.0)]

    assert accumulate(spans, overlap=SHARED) == pytest.approx(20.0)


def test_shared_charges_the_widest_job_across_an_overlap() -> None:
    """A four-wide job overlapping a one-wide job is not billed as the narrow one:
    the wide one was running, and its width is what the device gave it."""
    spans = [(0.0, 10 * MINUTE, 4.0), (0.0, 10 * MINUTE, 1.0)]

    assert accumulate(spans, overlap=SHARED) == pytest.approx(40.0)


def test_shared_still_charges_a_sequence_in_full() -> None:
    """Kill and resubmit occupies the device twice and pays twice; the rule is
    about overlap, not about counting jobs."""
    spans = [(0.0, 40 * MINUTE, 1.0), (40 * MINUTE, 80 * MINUTE, 1.0)]

    assert accumulate(spans, overlap=SHARED) == pytest.approx(80.0)


def test_a_gap_between_spans_is_not_charged() -> None:
    spans = [(0.0, 10 * MINUTE, 1.0), (120 * MINUTE, 130 * MINUTE, 1.0)]

    assert accumulate(spans, overlap=SHARED) == pytest.approx(20.0)


def test_partial_overlap_is_charged_end_to_end() -> None:
    spans = [(0.0, 40 * MINUTE, 1.0), (30 * MINUTE, 50 * MINUTE, 1.0)]

    assert accumulate(spans, overlap=SHARED) == pytest.approx(50.0)


def test_no_spans_is_no_spend() -> None:
    assert accumulate([], overlap=SHARED) == 0.0
    assert accumulate([], overlap=ADDITIVE) == 0.0
