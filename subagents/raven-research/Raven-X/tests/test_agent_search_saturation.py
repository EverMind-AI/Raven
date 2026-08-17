"""The saturation rule must be provable from its own output, not inferred.

Its predecessor is the reason this file is long. dr@2.8's cross-query dedup shipped
with a counter named for the mechanism, ``dedup_skipped``, and that counter reads 0
on every search row of every batch since - 13,198 and 10,615 rows on one batch
alone. So there is no on-disk evidence the mechanism ever fired, and whether it did
has to be argued from an unrelated field. Two separate causes produced that: the
knob was never switched on in any arm's config, AND the counting branch is
unreachable on the live-web path anyway because the result pool is never deeper
than the rendered width. Neither cause is visible in a 0.

The tests below therefore assert three different things, and the third is the one
that class of defect needs:

1. that the rule fires when it should (ordinary correctness),
2. that it does NOT fire when it should not (no over-triggering),
3. that its ledger payload distinguishes "did not fire" from "was not installed",
   and reports what the rule DID rather than what it was configured to do.
"""

from __future__ import annotations

import pytest

from raven.agent.search_saturation import SearchSaturation


def _run(sat: SearchSaturation, rounds: list[list[str]]) -> None:
    for identities in rounds:
        sat.observe(identities)


def test_a_streak_of_dry_searches_trips_the_rule():
    sat = SearchSaturation(k=3, on_saturate="stop")
    _run(sat, [["a", "b"], [], [], []])
    assert sat.stopped


def test_one_fresh_document_resets_the_streak():
    """The rule is about exhaustion, not about volume.

    A turn that keeps finding new pages may search as long as it likes; that is the
    difference between this and an iteration cap, and it is why the priced loss upper
    bound is zero - the questions it cuts are the ones that had stopped progressing.
    """
    sat = SearchSaturation(k=3, on_saturate="stop")
    _run(sat, [["a"], [], [], ["b"], [], []])
    assert not sat.stopped
    _run(sat, [[]])
    assert sat.stopped


def test_a_repeat_of_documents_already_seen_counts_as_dry():
    """Non-empty is not the same as new.

    The measured failure is a turn re-finding the same pages, not a turn getting
    empty responses: one question ran 295 searches whose last 234 returned results,
    all of them already shown. A rule keyed on "did anything come back" would have
    let every one of those through.
    """
    sat = SearchSaturation(k=2, on_saturate="stop")
    _run(sat, [["a", "b"], ["a"], ["b", "a"]])
    assert sat.stopped


def test_paginate_deepens_before_it_gives_up():
    sat = SearchSaturation(k=2, on_saturate="paginate", max_pages=3)
    assert sat.page == 1
    _run(sat, [[], []])
    assert sat.page == 2 and not sat.stopped
    _run(sat, [[], []])
    assert sat.page == 3 and not sat.stopped
    _run(sat, [[], []])
    assert sat.stopped and sat.page == 3


def test_opening_a_page_restarts_the_streak():
    """A fresh page must be judged on its own results.

    Carrying the streak across would stop the turn on the first search of a pool it
    has not read yet, which would make ``maxPages`` a decoration: the rule would
    paginate once and stop immediately regardless of what page two contained.
    """
    sat = SearchSaturation(k=2, on_saturate="paginate", max_pages=2)
    _run(sat, [[], []])
    assert sat.page == 2
    _run(sat, [[]])
    assert not sat.stopped


def test_an_endpoint_that_cannot_page_degrades_to_stopping_and_says_so():
    """Configured intent and actual behaviour are separate fields on purpose.

    The corpus service takes a width but no offset. A paginate-configured corpus arm
    that silently behaved like a stopping one would be the exact shape this codebase
    keeps paying for: capability on, activation site unreachable, no symptom.
    """
    sat = SearchSaturation(k=2, on_saturate="paginate", max_pages=3, paginates=False)
    _run(sat, [[], []])
    assert sat.stopped
    assert sat.page == 1
    assert sat.counters()["sat_action"] == "stopped_degraded"


def test_the_counters_separate_did_not_fire_from_was_not_installed():
    """The ``dedup_skipped`` lesson, as an assertion.

    An arm with the rule installed and quiet must be distinguishable from an arm that
    never had it, using the ledger row alone. Here that is ``sat_action == "none"``
    against the absent-rule row's ``sat_action is None``, which the tool writes.
    """
    quiet = SearchSaturation(k=10)
    _run(quiet, [["a"], ["b"], ["c"]])
    c = quiet.counters()
    assert c["sat_action"] == "none"
    assert c["sat_dry_streak"] == 0
    assert c["sat_seen"] == 3


def test_suppressed_searches_are_counted_not_just_prevented():
    """How much the rule saved is a number the batch must be able to report.

    Without it, "27% of searches removed" is a claim about an offline replay, not
    about the run that actually happened.
    """
    sat = SearchSaturation(k=1, on_saturate="stop")
    _run(sat, [[]])
    assert sat.stopped
    sat.suppress()
    sat.suppress()
    assert sat.counters()["sat_suppressed"] == 2


def test_reset_clears_every_field_a_turn_can_dirty():
    """Scoped to the turn, like every other memory on this tool.

    A leaked ``_stopped`` would close search for the rest of a gateway session, and
    a leaked ``_seen`` would make question two look saturated because question one
    had already read those pages.
    """
    sat = SearchSaturation(k=1, on_saturate="paginate", max_pages=2)
    _run(sat, [["a"], []])
    sat.suppress()
    sat.reset()
    assert not sat.stopped
    assert sat.page == 1
    assert sat.counters() == {
        "sat_action": "none",
        "sat_event": None,
        "sat_dry_streak": 0,
        "sat_page": 1,
        "sat_width": None,
        "sat_pages_opened": 0,
        "sat_suppressed": 0,
        "sat_seen": 0,
    }


@pytest.mark.parametrize("blank", ["", None])
def test_falsy_identities_never_count_as_a_new_document(blank):
    """A result row with no link must not reset the streak.

    Both renderers coerce a missing link to the empty string, so treating it as an
    identity would make one malformed row per search enough to keep a fully saturated
    turn searching forever - and malformed rows are commonest exactly where the pool
    has run out.
    """
    sat = SearchSaturation(k=2, on_saturate="stop")
    _run(sat, [[blank], [blank]])
    assert sat.stopped


# --------------------------------------------------------------------------- #
# The widen rung: the free half, and why it goes first                        #
# --------------------------------------------------------------------------- #


def test_widen_escalates_before_it_spends_a_second_call():
    """Cheapest rung first.

    ``num`` saturates upward on this endpoint but is honoured downward - num=5 returns
    exactly five where num=10 returns eight or nine for the same query - and the
    default rendered width is five. So the first response to "this query family is
    exhausted" is to display rows the arm has already paid for, not to buy another
    call. Paginating first would spend quota to obtain what was on the table.
    """
    sat = SearchSaturation(k=2, on_saturate="widen", max_pages=2, widen_to=10)
    assert sat.width(5) == 5
    _run(sat, [[], []])
    assert sat.width(5) == 10
    assert sat.page == 1
    assert sat.counters()["sat_action"] == "widened"


def test_widen_then_paginates_then_stops():
    sat = SearchSaturation(k=1, on_saturate="widen", max_pages=2, widen_to=10)
    _run(sat, [[]])
    assert sat.width(5) == 10 and sat.page == 1
    _run(sat, [[]])
    assert sat.page == 2 and not sat.stopped
    _run(sat, [[]])
    assert sat.stopped


def test_widen_never_narrows_what_the_caller_asked_for():
    """A rule whose job is to surface more documents must not be able to surface fewer.

    The model may pass an explicit ``count`` above the widen target; ``max`` makes
    honouring it structural rather than a promise, which matters because the failure
    would be silent - a shorter result list reads as a thinner corpus.
    """
    sat = SearchSaturation(k=1, on_saturate="widen", widen_to=10)
    _run(sat, [[]])
    assert sat.width(20) == 20


def test_the_default_ladder_is_unchanged_so_the_offline_pricing_still_applies():
    """k=10 removing 27.0% of searches at a 0.00pp loss bound was computed on the
    two-rung ladder. Making ``widen`` the default would silently invalidate the one
    number this rule is sold on, so the default must stay where it was priced."""
    assert SearchSaturation().on_saturate == "paginate"
    sat = SearchSaturation(k=1)
    _run(sat, [[]])
    assert sat.width(5) == 5
    assert sat.page == 2
