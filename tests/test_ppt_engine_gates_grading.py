"""The grading table, held to the registry it grades and to a run that did not converge.

Three things can go wrong here and each has a test rather than a convention. A check
added to `DISPATCH` and not to `TIERS` would fall into whichever batch happened to run,
so the two tables are asserted equal. A batch that dropped a blocking row would weaken
fail-closed publication without changing a severity, so every batch is asserted to keep
them. And a convergence rule is only as good as the run it was written for, so the
history in `_LOG` is transcribed from a real launcher log rather than invented.
"""

from __future__ import annotations

import importlib

import pytest

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.gates.grading import (
    BLOCKING_CHECKS,
    CHANGES,
    NEEDS_RENDER,
    NOISE_FLOOR,
    POINTS_PER_RENDERED_PIXEL,
    SHARPER_WITH_RENDER,
    TIERS,
    UNMOVED_BUILDS,
    WHOLE_DECK_ONLY,
    BuildRecord,
    Reported,
    Tier,
    checks_for,
    converged,
    in_points,
    in_rendered_pixels,
    names,
    unmoved,
)
from raven_ppt.services.gates.registry import DISPATCH, checks


def test_every_dispatched_check_is_graded_and_nothing_else_is():
    assert set(TIERS) == set(DISPATCH)


def test_every_graded_check_is_one_the_registry_can_actually_run():
    assert set(TIERS) == set(checks())


def test_the_tiers_partition_the_table():
    assert sum(len(names(tier)) for tier in Tier) == len(TIERS)


def test_the_blocking_list_is_the_dispatch_table_and_not_a_second_opinion():
    assert BLOCKING_CHECKS == {name for name, severity in DISPATCH.items() if severity is Severity.BLOCKING}


def test_no_grading_set_names_a_check_that_does_not_exist():
    for group in (BLOCKING_CHECKS, WHOLE_DECK_ONLY, NEEDS_RENDER, SHARPER_WITH_RENDER):
        assert group <= set(TIERS)


def test_render_dependence_matches_the_tiers_it_is_derived_from():
    assert NEEDS_RENDER == names(Tier.RENDERED) | names(Tier.RASTER)
    assert not (NEEDS_RENDER & SHARPER_WITH_RENDER)


def test_the_draft_exemptions_the_build_stage_already_holds_are_kept():
    """`stages/build.py` filters three kinds out of a draft; two of them are checks here.

    The third, `unseen_page`, is built by the stage itself and is not a registry row. A
    grading table that disagreed with the exemption already in force would mean a draft
    running a check whose findings the stage then throws away.
    """
    from raven_ppt.stages.build import _DRAFT_EXEMPT

    assert _DRAFT_EXEMPT & set(TIERS) <= WHOLE_DECK_ONLY


def test_the_delivering_build_runs_every_check():
    assert checks_for("delivery", has_render=True) == set(TIERS)


def test_no_batch_ever_drops_a_check_that_can_refuse_the_deck():
    """Except on a draft, where the length is not asked and the stage already says so."""
    for changed in CHANGES:
        batch = checks_for(changed, has_render=True)
        expected = BLOCKING_CHECKS if changed == "delivery" else BLOCKING_CHECKS - WHOLE_DECK_ONLY
        assert expected <= batch, changed


def test_coverage_rows_run_on_every_build_because_silence_is_not_clean():
    for changed in CHANGES:
        for has_render in (True, False):
            assert names(Tier.COVERAGE) <= checks_for(changed, has_render=has_render)


def test_a_machine_with_no_render_is_asked_for_nothing_that_needs_one():
    for changed in CHANGES:
        batch = checks_for(changed, has_render=False)
        assert not (batch & NEEDS_RENDER), changed
        assert "unrendered" in batch


def test_a_rerun_that_changed_nothing_is_the_smallest_batch():
    smallest = checks_for("nothing", has_render=True)
    for changed in CHANGES:
        assert smallest <= checks_for(changed, has_render=True), changed
    assert smallest < checks_for("delivery", has_render=True)


def test_a_prelude_edit_skips_the_checks_only_a_copy_edit_can_move():
    batch = checks_for("prelude", has_render=True)
    assert not (batch & (names(Tier.COPY) - BLOCKING_CHECKS))
    assert names(Tier.DECLARED) - WHOLE_DECK_ONLY <= batch


def test_a_page_edit_runs_everything_a_draft_can_be_held_to():
    assert checks_for("pages", has_render=True) == set(TIERS) - WHOLE_DECK_ONLY
    assert checks_for("deck_shape", has_render=True) == checks_for("pages", has_render=True)


def test_an_unknown_change_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError, match="unknown change"):
        checks_for("t3", has_render=True)


# --- convergence, against the run that did not converge ------------------------------
#
# Transcribed from /Evermind/sh_evermind/chenhongda/ppt_runs/hy4_tpl/launcher.glm.log,
# whose `ppt_build` result lines are truncated at 200 characters -- so what is recorded
# below is each build's page count and the first cause its reply carried, which is all
# the log states. Two builds raised before producing a deck; the `band` cause stands on
# pages 6 and 7 from 07:14:25 and on 6, 7 and 11 from 07:22:46 to the end.
_BAND = "an accent strip carries nothing"
_FLOOR = "boxes on this page show their copy under the floor"

_LOG = (
    BuildRecord(built=False),  # 07:11:27   304ms  the program raised
    BuildRecord(findings=(Reported("type_floor", None, _FLOOR),), pages=4),  # 07:12:11  8412ms
    BuildRecord(findings=(Reported("type_floor", None, _FLOOR),), pages=4),  # 07:12:47  6705ms
    BuildRecord(built=False),  # 07:13:13   322ms  the program raised
    BuildRecord(  # 07:14:25  11043ms
        findings=(Reported("band", 6, _BAND), Reported("band", 7, _BAND)), pages=8
    ),
    BuildRecord(  # 07:22:46 411743ms
        findings=(Reported("band", 6, _BAND), Reported("band", 7, _BAND), Reported("band", 11, _BAND)), pages=12
    ),
    BuildRecord(  # 07:27:52 193494ms
        findings=(Reported("band", 6, _BAND), Reported("band", 7, _BAND), Reported("band", 11, _BAND)), pages=12
    ),
    BuildRecord(  # 07:34:08 285863ms
        findings=(Reported("band", 6, _BAND), Reported("band", 7, _BAND), Reported("band", 11, _BAND)), pages=12
    ),
    BuildRecord(  # 07:39:10 224864ms
        findings=(Reported("band", 6, _BAND), Reported("band", 7, _BAND), Reported("band", 11, _BAND)), pages=12
    ),
    BuildRecord(  # 07:43:02 203952ms
        findings=(Reported("band", 6, _BAND), Reported("band", 7, _BAND), Reported("band", 11, _BAND)), pages=12
    ),
    BuildRecord(  # 07:43:37  13185ms
        findings=(Reported("band", 6, _BAND), Reported("band", 7, _BAND), Reported("band", 11, _BAND)), pages=12
    ),
)

# The durations above, in milliseconds, in the same order.
_DURATIONS_MS = (304, 8412, 6705, 322, 11043, 411743, 193494, 285863, 224864, 203952, 13185)


def test_the_live_run_is_called_at_the_eighth_build_and_not_before():
    for upto in range(1, 8):
        stop, _ = converged(_LOG[:upto])
        assert stop is False, upto
    stop, said = converged(_LOG[:8])
    assert stop is True
    assert "band" in said
    assert "6, 7, 11" in said


def test_it_stays_called_for_every_build_the_live_run_went_on_to_make():
    for upto in range(8, len(_LOG) + 1):
        assert converged(_LOG[:upto])[0] is True, upto


def test_calling_it_at_the_eighth_build_would_have_saved_three_builds():
    """What the rule is worth on the run it was written for, stated as a number."""
    saved = sum(_DURATIONS_MS[8:])
    assert saved == 442001
    assert saved / sum(_DURATIONS_MS) > 0.32


def test_the_band_cause_is_reported_as_unchanged_for_three_builds():
    streaks = unmoved(_LOG[:8])
    assert streaks[("band", 6, _BAND)] == UNMOVED_BUILDS
    assert streaks[("band", 11, _BAND)] == UNMOVED_BUILDS


def test_a_deck_that_changed_length_is_not_compared_across_the_change():
    """Page 6 of an eight-page deck is not page 6 of a twelve-page one.

    `services/regress.py` re-baselines on exactly this. Without the same rule here the
    band cause would have counted from 07:14 and the run would have been called one
    build earlier, on a comparison between two different pages.
    """
    assert unmoved(_LOG[:7])[("band", 6, _BAND)] == 2


def test_a_build_that_produced_no_deck_neither_advances_nor_resets():
    stuck = Reported("band", 3, _BAND)
    history = (
        BuildRecord(findings=(stuck,), pages=5),
        BuildRecord(built=False),
        BuildRecord(findings=(stuck,), pages=5),
        BuildRecord(findings=(stuck,), pages=5),
    )
    assert converged(history)[0] is True
    assert unmoved(history)[stuck.cause] == 3


def test_a_deck_that_is_still_refused_is_never_called_converged():
    refused = Reported("word_collision", 2, "two words share a place", blocking=True)
    history = tuple(BuildRecord(findings=(refused,), pages=6) for _ in range(6))
    stop, said = converged(history)
    assert stop is False
    assert "word_collision" in said


def test_findings_that_are_still_moving_are_not_called_converged():
    history = (
        BuildRecord(findings=(Reported("band", 3, _BAND),), pages=5),
        BuildRecord(findings=(Reported("band", 3, _BAND),), pages=5),
        BuildRecord(findings=(Reported("band", 3, _BAND), Reported("orphan_line", 4, "broke short")), pages=5),
    )
    stop, said = converged(history)
    assert stop is False
    assert "changed" in said


def test_a_build_reporting_nothing_has_converged():
    assert converged((BuildRecord(pages=4),))[0] is True


def test_too_few_builds_to_call_it_says_so():
    one = BuildRecord(findings=(Reported("band", 1, _BAND),), pages=3)
    stop, said = converged((one, one))
    assert stop is False
    assert str(UNMOVED_BUILDS) in said


def test_an_empty_history_is_not_convergence():
    assert converged(())[0] is False
    assert unmoved(()) == {}


def test_a_cause_folded_across_pages_compares_equal_to_its_unfolded_form():
    """`registry._one_per_cause` rewrites the message when one cause spans pages.

    Without stripping that prefix a cause spreading from two pages to three reads as a
    brand new finding, and the streak restarts on a build that changed nothing.
    """
    plain = "an accent strip carries nothing"
    folded = Finding(
        kind="band",
        severity=Severity.WARNING,
        message=f"pages 6, 7 all report this, so it is one cause and not 2: {plain}",
        detail={"on_pages": [6, 7]},
    )
    apart = [
        Finding(kind="band", severity=Severity.WARNING, message=plain, page=6),
        Finding(kind="band", severity=Severity.WARNING, message=plain, page=7),
    ]
    assert BuildRecord.of([folded], pages=8).findings == BuildRecord.of(apart, pages=8).findings


def test_a_route_can_name_a_warning_fatal_and_the_record_reads_it_as_refusing():
    warned = Finding(kind="page_budget", severity=Severity.WARNING, message="two pages over", page=None)
    record = BuildRecord.of([warned], blocking_kinds=("page_budget",))
    assert record.findings[0].blocking is True


# --- the noise floors -----------------------------------------------------------------


def test_every_floor_in_force_still_equals_the_constant_it_mirrors():
    for name, floor in NOISE_FLOOR.items():
        if not floor.in_force:
            continue
        module, _, constant = floor.source.rpartition(".")
        live = getattr(importlib.import_module(f"raven_ppt.services.{module}"), constant)
        assert live == pytest.approx(floor.value), name


def test_a_proposed_floor_is_marked_as_one_and_names_no_constant():
    proposed = [floor for floor in NOISE_FLOOR.values() if not floor.in_force]
    assert proposed
    assert all(floor.source == "" for floor in proposed)


def test_every_floor_names_checks_this_table_grades():
    for name, floor in NOISE_FLOOR.items():
        assert set(floor.covers) <= set(TIERS), name


def test_the_canvas_edge_floor_is_exactly_one_rendered_pixel():
    """144dpi puts a 16:9 page at 1920x1080, so a pixel is half a point and 6350 EMU."""
    assert in_points("canvas_edge") == pytest.approx(POINTS_PER_RENDERED_PIXEL)
    assert in_rendered_pixels("canvas_edge") == pytest.approx(1.0)


def test_the_proposed_spacing_floor_is_the_four_pixels_it_was_borrowed_as():
    assert in_rendered_pixels("spacing_noise") == pytest.approx(4.0)


def test_lengths_convert_and_everything_else_declines_to():
    assert in_points("cloned_shape") == pytest.approx(3.6)
    assert in_points("card_rim") == pytest.approx(3.0)
    assert in_points("collision_share") is None
    assert in_rendered_pixels("overset_slack") is None


def test_publishing_never_narrows_the_pass(tmp_path) -> None:
    """The grading only ever shortens a draft. Delivery runs the whole table.

    The saving this table exists for is real -- one live run spent 442 seconds
    rebuilding for a reading that never moved -- and it is worth nothing if it can
    also drop a refusal on the way out. `_changed` answers "delivery" for every
    build that is not a draft, and this is the assertion that keeps it that way.
    """
    from raven_ppt.contracts import BuildOutcome, Project
    from raven_ppt.services.gates.registry import DISPATCH
    from raven_ppt.stages.build import _changed

    outcome = BuildOutcome(ok=True, pages=3)
    project = Project(workspace=tmp_path, slug="grading")

    assert _changed(project, outcome, draft=False) == "delivery"
    assert checks_for("delivery", has_render=True) == frozenset(DISPATCH)
    # And a draft with no record to compare against spends the wider pass rather than
    # the narrower one: not knowing is not the same as knowing nothing changed.
    assert _changed(project, outcome, draft=True) == "deck_shape"
