"""dr@2.8 research trail: computed from the record, never asked of the model.

The appendix exists because the alternative - a contract clause asking the model
to describe its own search strategy - produces an unverifiable self-report. So
every test here is about the properties that make it *not* a self-report: it can
only say what the ledger says, it says nothing when the ledger is absent rather
than rendering zeroes, and it never reaches the model.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.process_appendix import build_appendix, build_trail

LEDGER = [
    {"op": "search", "query": "arwa university founding", "replay": False, "zero_hit": False},
    {"op": "search", "query": "Arwa  University   founding", "replay": True, "zero_hit": False},
    {"op": "search", "query": "arwa charter 1974", "replay": False, "zero_hit": True},
    {"op": "fetch", "url": "https://a.example/one", "chars": 12431, "ok": True},
    {"op": "fetch", "url": "https://b.example/two", "chars": 0, "ok": False},
    {"op": "verify", "outcome": "reject", "unsupported_claims": ["the 1974 charter date"]},
    {"op": "verify", "outcome": "pass", "unsupported_claims": []},
]


def test_the_trail_reports_only_what_the_ledger_recorded():
    t = build_trail(LEDGER, "see https://a.example/one")

    assert t.searches == 3
    assert t.replays == 1
    assert t.zero_hit == 1
    # Whitespace and case folded: the same query typed twice is one query, and a
    # trail that called it two would overstate the breadth of the research.
    assert t.distinct_queries == ["arwa university founding", "arwa charter 1974"]
    assert t.counters()["pages_ok"] == 1 and t.counters()["pages_opened"] == 2
    # Last verdict wins - the turn ended on a pass, and its open points went with it.
    assert t.verify_outcome == "pass"
    assert t.unsupported == []


def test_a_cited_url_that_was_never_opened_is_named_as_such():
    """The one line here that accuses the answer, and the only report-quality
    number in this build that needs no judge and does not reward length."""
    t = build_trail(LEDGER, "per https://a.example/one and https://never.example/x it holds")

    assert t.cited_not_opened == ["https://never.example/x"]
    assert t.counters()["citation_grounding_rate"] == 0.5
    assert "never opened" in t.render()
    assert "https://never.example/x" in t.render()


def test_trailing_punctuation_does_not_manufacture_a_fabricated_citation():
    """A URL ending a sentence is the common case; treating the period as part of
    it would report a correctly-read page as invented - a false alarm on the one
    assertion here that calls the answer wrong."""
    t = build_trail(LEDGER, "It is stated at https://a.example/one.")
    assert t.cited_not_opened == []
    assert t.counters()["citation_grounding_rate"] == 1.0


def test_canonicalisation_is_folded_but_a_deeper_path_is_still_fabricated():
    """The two directions this check can fail, in one test.

    Folding too little raises a false alarm on a page that really was read; folding
    too much - the first implementation matched on prefix - absolves any deep link
    invented on top of a page that was read, which is the most plausible
    fabrication there is and the one this check exists to catch.
    """
    t = build_trail(
        LEDGER,
        "as shown at HTTP://A.example/one/ and in https://a.example/one/appendix-c",
    )

    # Asserted present first. Without this line the check below passes whenever the
    # URL is not extracted at all - which is exactly what happened: the extractor was
    # case-sensitive, ``HTTP://`` never became a citation, and "it was not flagged"
    # was true for the wrong reason.
    assert "HTTP://A.example/one/" in t.cited, "uppercase scheme was never extracted"
    assert "https://a.example/one/appendix-c" in t.cited_not_opened, "deep link absolved"
    assert "HTTP://A.example/one/" not in t.cited_not_opened, (
        "scheme/case/trailing-slash difference reported as a fabricated citation"
    )


def test_an_answer_citing_nothing_scores_null_not_perfect():
    t = build_trail(LEDGER, "The founding year is 1974.")
    c = t.counters()
    assert c["urls_cited"] == 0
    # Not 1.0. An answer with no sources is unmeasured, not perfectly grounded, and
    # a rate of 1.0 here would put it at the top of any ranking built on this field.
    assert c["citation_grounding_rate"] is None


def test_no_ledger_yields_no_appendix_and_says_why(tmp_path):
    """Missing data must not render as zero activity.

    "0 searches, 0 pages read" printed under an answer that did plenty of both is
    the shape of every missing-data-read-as-zero defect this project has logged.
    """
    text, counters = build_appendix(None, "answer")
    assert text == ""
    assert counters == {"emitted": False, "reason": "ledger_not_configured"}

    missing = tmp_path / "nope.jsonl"
    text, counters = build_appendix(str(missing), "answer")
    assert text == "" and counters["reason"] == "ledger_empty"

    empty_research = tmp_path / "l.jsonl"
    empty_research.write_text(json.dumps({"op": "verify", "outcome": "pass"}) + "\n")
    text, counters = build_appendix(str(empty_research), "answer")
    assert text == "" and counters["reason"] == "no_research_events"


def test_a_torn_last_line_does_not_lose_the_whole_trail(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in LEDGER) + '\n{"op": "sear')
    text, counters = build_appendix(str(p), "https://a.example/one")
    assert counters["emitted"] is True
    assert counters["searches"] == 3
    assert "Research trail" in text


def test_the_rendered_trail_leads_with_the_summary_and_folds_the_detail():
    """Readability is the product requirement: answer first, detail collapsible."""
    text = build_trail(LEDGER, "https://a.example/one").render()
    lines = [line for line in text.splitlines() if line.strip()]

    assert lines[0] == "---"
    assert lines[1].startswith("**Research trail**")
    assert "3 searches (2 distinct), 1 pages read" in lines[1]
    assert text.count("<details>") == 2  # queries + pages; no open points on a pass
    assert text.index("<details>") > text.index("**Research trail**")


def test_the_appendix_is_gated_on_the_assembly_so_the_anchor_cannot_reach_it():
    from raven.agent.flow.dr import build_dr_flow
    from raven.config.raven import DRFlowConfig

    assert build_dr_flow(DRFlowConfig(enabled=False), None, 10, 1000) is None
    on = build_dr_flow(DRFlowConfig(enabled=True), None, 10, 1000)
    assert on.process_appendix is True
    off = build_dr_flow(
        DRFlowConfig(enabled=True, final_shape={"process_appendix": False}), None, 10, 1000
    )
    assert off.process_appendix is False


def test_the_appendix_never_reaches_the_model():
    """Stated as a source-level assertion because it cannot be observed from output.

    The seam appends to ``final_content`` after ``messages`` already holds the raw
    answer, so the persisted trajectory and the next turn's history are unchanged.
    If that ordering is ever inverted, the model starts reading our commentary on
    its own work as if it had written it, and the ledger stops being an instrument
    that cannot steer what it measures.
    """
    import inspect

    from raven.agent.loop import main

    src = inspect.getsource(main.AgentLoop)
    # Anchored on the render call, not on the first mention of the knob: dr@2.9 added an
    # earlier reference (the turn-scoped ledger is opened at the top of the loop), and a
    # "first occurrence" anchor silently retargets to whichever line mentions it soonest.
    seam = src.index("appendix, trail = build_appendix")
    tail = src[seam:seam + 1200]
    assert "final_content = (final_content or \"\").rstrip()" in tail
    assert "add_assistant_message" not in tail, (
        "the appendix must not be persisted as a message - that is the feedback path"
    )


def test_dead_end_fetches_are_counted_but_not_listed():
    """A research turn spends fetches finding the right URL; they are not "pages read".

    Measured on the first two product runs: one answer's appendix reached 26% of the
    output because every guessed path that returned a 200-with-nothing got its own
    line. They still get counted - "23 pages read" over 9 real ones stops being an
    audit and becomes decoration - they just do not each get a URL.
    """
    rows = [
        {"op": "search", "query": "q", "replay": False},
        {"op": "fetch", "url": "https://good.example/real", "chars": 9000, "ok": True},
        {"op": "fetch", "url": "https://guess.example/wrong", "chars": 199, "ok": True},
        {"op": "fetch", "url": "https://guess.example/also-wrong", "chars": 203, "ok": True},
        {"op": "fetch", "url": "https://dead.example/x", "chars": 0, "ok": False},
    ]
    text = build_trail(rows, "https://good.example/real").render()

    assert "https://good.example/real" in text
    assert "guess.example" not in text, "a 199-char stub is not a page that was read"
    assert "2 fetch(es) returned almost nothing" in text
    assert "1 page(s) could not be retrieved" in text


# --------------------------------------------------------------------------- #
# dr@3.0: the check's scope once a conversation can have more than one turn    #
# --------------------------------------------------------------------------- #


def test_a_page_an_earlier_turn_opened_is_not_a_fabricated_citation():
    """The defect this parameter exists to fix, reproduced from the run that found it.

    On the first three-turn conversation, turn three answered a follow-up, cited two
    URLs the research memo had listed - both opened in turn one - and the appendix
    reported both as never opened. The numerator had become per-conversation the
    moment the memo started handing later turns their own earlier sources, while the
    denominator was still per-turn. The direction is fixed and the trigger is the
    well-behaved case: an answer that credits where a fact came from.
    """
    answer = "per https://a.example/one and https://earlier.example/p2 it holds"
    without = build_trail(LEDGER, answer)
    assert without.cited_not_opened == ["https://earlier.example/p2"]

    t = build_trail(LEDGER, answer, ["https://earlier.example/p2"])

    assert t.cited_not_opened == []
    assert t.counters()["citation_grounding_rate"] == 1.0
    assert t.counters()["opened_earlier"] == 1
    assert "never opened" not in t.render()


def test_the_rendered_trail_still_lists_only_what_this_turn_opened():
    """Widening the check must not let a turn claim credit for earlier reading.

    ``pages`` drives the "N pages read" headline, and a turn that read one page while
    the conversation had read five must still say one - otherwise every later turn of
    a long conversation looks like a deeper piece of research than it was.
    """
    t = build_trail(LEDGER, "https://earlier.example/p2", ["https://earlier.example/p2"])
    assert len(t.pages) == len(build_trail(LEDGER, "").pages)
    assert "https://earlier.example/p2" not in t.render()


def test_an_unopened_url_is_still_caught_when_earlier_sources_are_supplied():
    """The reverse direction: the parameter widens the denominator, it does not
    disable the check. A fix that made the accusation unreachable would be worth
    less than the false positive it replaced."""
    t = build_trail(
        LEDGER,
        "see https://never.example/x",
        ["https://earlier.example/p2"],
    )
    assert t.cited_not_opened == ["https://never.example/x"]
    assert t.counters()["citation_grounding_rate"] == 0.0


def test_earlier_sources_are_normalised_the_same_way_as_fetched_ones():
    """A memo stores whatever the reader handed back, so the two sides can differ in
    canonical form. Comparing them raw would reinstate the false positive for exactly
    the URLs a reader service rewrote."""
    t = build_trail(LEDGER, "see https://earlier.example/p2/", ["http://earlier.example/p2"])
    assert t.cited_not_opened == []


def test_the_scope_is_reported_so_two_runs_are_comparable():
    """``opened_earlier`` is 0 on every single-turn run, which is every measured arm -
    so a non-zero value is the signal that the rate beside it was computed over a
    conversation rather than a turn."""
    assert build_trail(LEDGER, "x").counters()["opened_earlier"] == 0
    assert build_trail(LEDGER, "x", []).counters()["opened_earlier"] == 0


# --------------------------------------------------------------------------- #
# dr@3.0: the extractor's stop set, and the ASCII behaviour it must not change #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("依据 https://a.example/one（原文：xxx）", "https://a.example/one"),
        ("见 https://a.example/one）。但该页", "https://a.example/one"),
        ("参考 https://a.example/one；另见", "https://a.example/one"),
        ("文档 https://a.example/one，说明了这一点", "https://a.example/one"),
        ("引自 https://a.example/one`）；佐证", "https://a.example/one"),
        ("**https://a.example/one**，见上", "https://a.example/one"),
        ("详见 https://a.example/one和其它页", "https://a.example/one"),
    ],
)
def test_a_url_followed_by_chinese_punctuation_is_not_a_fabricated_citation(answer, expected):
    """The defect an eight-turn Chinese conversation surfaced, one form per row.

    The stop set was ASCII-only, and on Chinese output that is not a near-miss: nothing
    ended the match, so the citation swallowed the rest of the clause and matched no
    fetch record. A page the run really had opened was then named as invented - fixed
    direction, and only on Chinese output, which is this product's whole surface. Four
    of one eight-turn run's flagged citations were this and none were real.
    """
    t = build_trail(LEDGER, answer)
    assert t.cited == [expected]
    assert t.cited_not_opened == []
    assert t.counters()["citation_grounding_rate"] == 1.0


def test_the_ascii_forms_every_published_reading_was_measured_on_are_unchanged():
    """The regression risk that matters more than the fix.

    This counter has been reported since dr@2.8. Widening the stop set must not move a
    single ASCII extraction, or every historical value silently refers to a different
    quantity than the one recomputed from the same trajectory.
    """
    assert build_trail(LEDGER, "see https://a.example/one).").cited == ["https://a.example/one"]
    assert build_trail(LEDGER, "(https://a.example/one)").cited == ["https://a.example/one"]
    assert build_trail(LEDGER, "https://a.example/one, and more").cited == ["https://a.example/one"]
    assert build_trail(
        LEDGER, "per https://a.example/one and https://never.example/x it holds"
    ).cited == ["https://a.example/one", "https://never.example/x"]
    # Query strings and fragments are part of the link, not sentence punctuation.
    assert build_trail(LEDGER, "at https://a.example/one?q=1&b=2#frag.").cited == [
        "https://a.example/one?q=1&b=2#frag"
    ]


def test_a_chinese_answer_can_still_be_caught_fabricating():
    """The reverse direction. A stop set wide enough to stop catching anything would be
    worth less than the false positives it removed."""
    t = build_trail(LEDGER, "依据 https://never.example/x（原文：xxx）")
    assert t.cited_not_opened == ["https://never.example/x"]
    assert t.counters()["citation_grounding_rate"] == 0.0


# ── dr@3.3: the grounding line is rendered in every direction ─────────
#
# Before dr@3.3 only the failing direction reached the page. A check that is
# visible when it fails and invisible when it passes trains its reader to read
# silence as "not checked", which is the same failure shape as a gate that only
# prints on red. All three states are asserted here so none can be dropped as
# "the obvious case".


def test_a_clean_answer_says_so_instead_of_saying_nothing():
    t = build_trail(LEDGER, "see https://a.example/one")
    out = t.render()
    assert "All 1 cited link was opened" in out
    assert "⚠️" not in out


def test_an_answer_that_cites_nothing_is_not_rendered_as_perfect():
    """Undefined, not 1.0 - the ``counters()`` rule, now also on the page."""
    t = build_trail(LEDGER, "no links here at all")
    out = t.render()
    assert "nothing to check" in out
    assert "All 0" not in out
    assert t.counters()["citation_grounding_rate"] is None


def test_the_warning_carries_its_denominator():
    """The module rule is that the rate never appears without ``urls_cited``.
    Rendering "2 links were never opened" without the total states a numerator
    on its own, which is the same number saying two different things depending
    on whether the answer cited two links or forty."""
    t = build_trail(LEDGER, "see https://a.example/one and https://ghost.example/x")
    out = t.render()
    assert "of 2 link(s) cited" in out
    assert t.counters()["urls_cited"] == 2


def test_the_conversation_scope_travels_with_the_fraction():
    t = build_trail(LEDGER, "see https://a.example/one")
    t.opened_earlier = 1
    assert "opened on an earlier turn" in t.render()


def test_scope_note_is_absent_when_nothing_was_cited():
    t = build_trail(LEDGER, "no links")
    t.opened_earlier = 3
    assert "earlier turn" not in t.render()
