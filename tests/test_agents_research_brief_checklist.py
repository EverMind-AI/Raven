"""The request, read literally and handed back before the answer is written.

The 2026-09-04 head-to-head lost three dimensions to constraints that were stated in the
request and satisfied by intention rather than by checking: a named section was absent, five
candidates outside the stated envelope took slots in the main table, and the do-not-bother
list carried items the request had already ruled out.

Every test here is written from the failure direction that matters. A checklist that misses
a constraint costs nothing - the reminder is what it always was. A checklist that invents
one, or quotes half a name, tells the model about a requirement the reader never wrote, and
there is no gate downstream that can tell the difference. So the extractors are pinned on
what they refuse as much as on what they read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.report_shape import (  # noqa: E402
    REMINDER_CLOSE,
    REMINDER_OPEN,
    render_reminder,
    strip_reminder,
)
from research_flow.support.brief_checklist import read_brief, render_checklist  # noqa: E402

#: The request behind the head-to-head, as its own report restated it.
TASK_B = (
    "Shortlist 15-25 public long-context benchmarks we have not run, excluding LongBench, "
    "LooGLE, ZeroSCROLLS and LoCoMo, each scored 1-5 on genre match, context necessity, "
    "evidence dispersion, multi-query reuse and comparability, plus a do-not-bother list "
    "and a community-convention table."
)


# ---------------------------------------------------------------------------
# What it reads
# ---------------------------------------------------------------------------


def test_the_real_request_is_read_in_all_three_parts():
    brief = read_brief(TASK_B)
    assert brief.count == (15, 25)
    assert brief.exclusions == ("LongBench", "LooGLE", "ZeroSCROLLS", "LoCoMo")
    assert brief.artefacts == ("do-not-bother list", "community-convention table")


def test_the_scoring_range_does_not_win_over_the_item_count():
    # "each scored 1-5" is also a range, and it comes second; the count is the first one.
    assert read_brief(TASK_B).count == (15, 25)


@pytest.mark.parametrize(
    "task",
    [
        "Summarise methods that work at 16k-256k words of context.",
        "Cover the 2024-2026 literature.",
        "Report throughput at 4x-8x compression.",
        "We have 8-80GB cards.",
    ],
)
def test_a_size_or_a_span_is_not_an_item_count(task):
    assert read_brief(task).count is None


def test_a_camel_cased_name_is_kept_whole():
    """An earlier rule anchored on a leading capital and quoted `xRAG` back as `RAG`,
    which is a checklist naming a benchmark that does not exist."""
    assert read_brief("Compare them, excluding ICAE and xRAG.").exclusions == ("ICAE", "xRAG")


def test_an_exclusion_the_request_states_in_prose_is_left_alone():
    assert read_brief("Survey the field, excluding the four we already ran.").exclusions == ()


def test_the_answer_itself_is_not_an_artefact():
    assert read_brief("Write a report and a summary table.").artefacts == ("summary table",)


def test_a_bare_noun_is_not_an_artefact():
    # "a table" gives a reader nothing to check for; only a named one does.
    assert read_brief("Give me a table.").artefacts == ()


def test_a_request_with_no_checkable_constraint_reads_as_empty():
    brief = read_brief("What is the H100 memory bandwidth?")
    assert not brief
    assert render_checklist("What is the H100 memory bandwidth?") == ""


def test_an_empty_task_is_not_an_error():
    assert not read_brief("")
    assert render_checklist("") == ""


# ---------------------------------------------------------------------------
# What it says, and where it says it
# ---------------------------------------------------------------------------


def test_the_checklist_quotes_the_request_and_leaves_an_escape():
    text = render_checklist(TASK_B)
    assert "15 to 25 items" in text
    assert "nothing from LongBench, LooGLE, ZeroSCROLLS, LoCoMo" in text
    assert "a do-not-bother list" in text and "a community-convention table" in text
    # Read literally, not ordered: a misread must be visibly a misread of the request.
    assert "Read literally" in text
    # And an item that turns out not to apply has somewhere to go that is not silence.
    assert "`## Limitations`" in text


def test_the_reminder_carries_the_checklist_inside_its_own_block():
    block = render_reminder(TASK_B)
    assert block.startswith(REMINDER_OPEN) and block.endswith(REMINDER_CLOSE)
    assert "15 to 25 items" in block
    # One delimiter pair still bounds everything injected, so the strip needs no new case.
    assert f"question\n\n{block}" and strip_reminder(f"question\n\n{block}") == "question"


def test_a_request_with_no_constraints_renders_the_bytes_it_always_rendered():
    assert render_reminder("What is the H100 memory bandwidth?") == render_reminder()
    assert render_reminder("") == render_reminder()


def test_the_template_instruction_is_never_displaced_by_the_checklist():
    block = render_reminder(TASK_B)
    assert "`## Answer`" in block and "`## Findings`" in block and "`## Limitations`" in block
    assert block.index("## Answer") < block.index("Read literally")


# ── what it must never turn a constraint into ─────────────────────────
# Review found two ways to invert rather than miss. An inverted constraint is strictly
# worse than a missing one: this sentence is the last thing before generation, so a
# constraint it reverses is one the harness invented against the request.


def test_a_scoring_scale_is_not_the_deliverable_count_whichever_comes_first():
    """The defect: the count was whichever range came first and was not a size, so a
    rubric stated before the shortlist became the number of items to deliver."""
    scale_first = "Score candidates 1-5 on relevance, then shortlist 15-25 benchmarks"
    count_first = "Shortlist 15-25 benchmarks, scoring each 1-5 on relevance"

    assert read_brief(scale_first).count == (15, 25)
    assert read_brief(count_first).count == (15, 25)
    assert "15 to 25 items" in render_checklist(scale_first)


def test_a_range_that_is_only_a_scale_says_nothing():
    for task in ("On a scale of 1-5, judge the three axes", "Rank the methods 1-5 on three axes", "Rate each 1-5"):
        assert read_brief(task).count is None, task


def test_a_scale_word_reaches_its_own_range_and_no_further():
    """A prohibition governs the list it introduces, so it scopes to the clause. A scale
    marker introduces one range, so it does not silence a later one - suppressing that
    would hide a requirement the request really made."""
    assert read_brief("Rate each 1-5 and give 15-25 benchmarks").count == (15, 25)
    assert read_brief("On a large-scale corpus, shortlist 15-25 benchmarks").count == (15, 25)


def test_a_range_followed_by_a_preposition_is_not_a_count():
    assert read_brief("Judge 1-5 on three axes").count is None
    assert read_brief("Compare 2-4 of the released checkpoints").count is None


def test_a_negated_artefact_is_not_something_to_deliver():
    """The second defect, verbatim: the request rules the table out and the checklist
    ordered the model to produce it or explain its absence."""
    task = "Do not include a comparison table; write a concise narrative instead."

    assert read_brief(task).artefacts == ()
    assert render_checklist(task) == ""


def test_a_prohibition_covers_the_whole_list_it_introduces():
    assert read_brief("Do not include a comparison table, a matrix, or an appendix.").artefacts == ()


def test_a_prohibition_does_not_reach_back_over_what_was_asked_for():
    brief = read_brief("Include a convention table, but do not include a comparison table.")

    assert brief.artefacts == ("convention table",)


def test_a_negated_exclusion_is_not_an_exclusion():
    """The same inversion on the other extractor, which is why the guard is not scoped to
    noun phrases: "do not exclude X" would otherwise be quoted back as "nothing from X"."""
    assert read_brief("Do not exclude LongBench or LooGLE.").exclusions == ()


def test_the_briefs_own_hyphenated_artefact_is_not_read_as_a_negation():
    """``do-not-bother list`` is what the 2026-09-04 brief asked for by name, and a word
    boundary finds a "not" inside it. A cue that fires there silences every constraint
    stated after it in the same clause - which is how the first form of this guard
    dropped the convention table."""
    task = (
        "Shortlist 15-25 benchmarks, excluding LongBench, LooGLE and ZeroSCROLLS, "
        "with a do-not-bother list and a community-convention table"
    )
    brief = read_brief(task)

    assert brief.count == (15, 25)
    assert brief.exclusions == ("LongBench", "LooGLE", "ZeroSCROLLS")
    assert brief.artefacts == ("do-not-bother list", "community-convention table")


def test_a_qualifier_on_what_to_include_is_not_a_prohibition():
    """ "benchmarks we have not run, excluding LongBench" - the real brief's own wording.
    A bare "not" carries no direction, and a cue that fired on this one would suppress the
    exclusions the request did state, which is the same inversion the other way round."""
    brief = read_brief("Shortlist 15-25 benchmarks we have not run, excluding LongBench and LooGLE")

    assert brief.count == (15, 25)
    assert brief.exclusions == ("LongBench", "LooGLE")


# ── the second round of counter-examples ──────────────────────────────
# Review's first fix passed its own examples and left both failure classes reachable
# through ordinary wording. Both of these were its counter-examples.


def test_a_rating_written_after_its_numbers_is_not_a_count():
    """`4-5 star benchmarks` rates the benchmarks; it does not ask for four or five.

    The first guard only looked for a scale word BEFORE the range, so a rating marked on
    the noun sailed through and displaced the request's real count."""
    task = "From 4-5 star benchmarks, shortlist 15-25 candidates."

    assert read_brief(task).count == (15, 25)
    assert "15 to 25 items" in render_checklist(task)


def test_a_rubric_stated_after_the_counted_noun_is_still_a_count():
    """The rating check reads only the span between the numbers and the counted noun.
    Widening it to the tail would reject this, which is a real count of benchmarks."""
    assert read_brief("Shortlist 15-25 benchmarks scored 1-5 on genre match").count == (15, 25)
    assert read_brief("Score each 3-4 out of 5 and shortlist 15-25 benchmarks").count == (15, 25)


def test_an_exclusion_directive_rules_an_artefact_out_of_the_deliverables():
    """These are the words the module already knows introduce an exclusion, and the
    artefact extractor was not reading them - so `Exclude a comparison table` came back as
    a table to deliver. Two questions, two cue sets: what is ruled out as a deliverable
    includes every exclusion cue, while what introduces a list of excluded NAMES may only
    be suppressed by a directive negation, or the extractor suppresses itself."""
    for task in (
        "Exclude a comparison table from the report.",
        "Any format other than a comparison table.",
        "Leaving out a ranking table, write a narrative.",
    ):
        assert read_brief(task).artefacts == (), task
        assert render_checklist(task) == "", task


def test_an_exclusion_reaches_over_its_own_list_and_no_further():
    """The real brief excludes four benchmarks and then asks for two artefacts. An
    exclusion that reached to the end of its clause would delete both - the same inversion
    from the other side. The reach ends at the first item that is not name-shaped."""
    brief = read_brief(TASK_B)

    assert brief.exclusions == ("LongBench", "LooGLE", "ZeroSCROLLS", "LoCoMo")
    assert brief.artefacts == ("do-not-bother list", "community-convention table")


def test_the_next_demand_is_not_swallowed_as_an_excluded_name():
    """`excluding LongBench, give a do-not-bother list and 15-25 benchmarks` once reported
    "nothing from 15-25 benchmarks" - the request's own deliverable quoted back as
    something to avoid, because the exclusion body ran to the end of the sentence."""
    brief = read_brief("Excluding LongBench, give a do-not-bother list and 15-25 benchmarks.")

    assert brief.exclusions == ("LongBench",)
    assert brief.artefacts == ("do-not-bother list",)
    assert brief.count == (15, 25)
