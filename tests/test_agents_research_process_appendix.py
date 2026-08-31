"""dr@2.8 research trail: computed from the record, never asked of the model.

The appendix exists because the alternative - a contract clause asking the model
to describe its own search strategy - produces an unverifiable self-report. So
every test here is about the properties that make it *not* a self-report: it can
only say what the ledger says, it says nothing when the ledger is absent rather
than rendering zeroes, and it never reaches the model.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.support.process_appendix import build_appendix, build_trail  # noqa: E402

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
    from research_flow.config import FlowConfig

    on = FlowConfig(enabled=True)
    assert on.final_shape.process_appendix is True
    off = FlowConfig(enabled=True, final_shape={"process_appendix": False})
    assert off.final_shape.process_appendix is False


def test_the_appendix_never_reaches_the_model():
    """Stated as a source-level assertion because it cannot be observed from output.

    The seam appends to ``final_content`` after ``messages`` already holds the raw
    answer, so the persisted trajectory and the next turn's history are unchanged.
    If that ordering is ever inverted, the model starts reading our commentary on
    its own work as if it had written it, and the ledger stops being an instrument
    that cannot steer what it measures.
    """
    import inspect

    from research_flow import flow

    src = inspect.getsource(flow.TurnFrame)
    # Anchored on the render call, not on the first mention of the knob: dr@2.9 added an
    # earlier reference (the turn-scoped ledger is opened at the top of the loop), and a
    # "first occurrence" anchor silently retargets to whichever line mentions it soonest.
    seam = src.index("appendix, trail = build_appendix")
    tail = src[seam : seam + 1200]
    assert 'final_content = final_content.rstrip() + "\\n" + appendix' in tail
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
        ("\u4f9d\u636e https://a.example/one\uff08\u539f\u6587\uff1axxx\uff09", "https://a.example/one"),
        ("\u89c1 https://a.example/one\uff09\u3002\u4f46\u8be5\u9875", "https://a.example/one"),
        ("\u53c2\u8003 https://a.example/one\uff1b\u53e6\u89c1", "https://a.example/one"),
        ("\u6587\u6863 https://a.example/one\uff0c\u8bf4\u660e\u4e86\u8fd9\u4e00\u70b9", "https://a.example/one"),
        ("\u5f15\u81ea https://a.example/one`\uff09\uff1b\u4f50\u8bc1", "https://a.example/one"),
        ("**https://a.example/one**\uff0c\u89c1\u4e0a", "https://a.example/one"),
        ("\u8be6\u89c1 https://a.example/one\u548c\u5176\u5b83\u9875", "https://a.example/one"),
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
    assert build_trail(LEDGER, "per https://a.example/one and https://never.example/x it holds").cited == [
        "https://a.example/one",
        "https://never.example/x",
    ]
    # Query strings and fragments are part of the link, not sentence punctuation.
    assert build_trail(LEDGER, "at https://a.example/one?q=1&b=2#frag.").cited == ["https://a.example/one?q=1&b=2#frag"]


def test_a_chinese_answer_can_still_be_caught_fabricating():
    """The reverse direction. A stop set wide enough to stop catching anything would be
    worth less than the false positives it removed."""
    t = build_trail(LEDGER, "\u4f9d\u636e https://never.example/x\uff08\u539f\u6587\uff1axxx\uff09")
    assert t.cited_not_opened == ["https://never.example/x"]
    assert t.counters()["citation_grounding_rate"] == 0.0


# ── decoded CJK paths, and the prose they drag in ──────────────────────
#
# A page is fetched percent-encoded and cited decoded. The extractor used to end
# a URL at the first ideograph, so every such citation was reported as never
# opened - and two different wiki pages truncated to the SAME prefix, multiplying
# the accusation. Observed on every Chinese-subject turn of the first product
# sessions that cited zh.wikipedia paths.

CJK_LEDGER = [
    {"op": "search", "query": "\u674e\u5f66\u5b8f", "replay": False, "zero_hit": False},
    {
        "op": "fetch",
        "url": "https://zh.wikipedia.org/wiki/%E6%9D%8E%E5%BD%A6%E5%AE%8F",
        "chars": 1711,
        "ok": True,
    },
]


def test_a_decoded_cjk_citation_matches_its_percent_encoded_fetch():
    t = build_trail(
        CJK_LEDGER, "\u89c1 https://zh.wikipedia.org/wiki/\u674e\u5f66\u5b8f \uff1b\u53e6\u89c1\u7ef4\u57fa"
    )
    assert t.cited == ["https://zh.wikipedia.org/wiki/\u674e\u5f66\u5b8f"]
    assert t.cited_not_opened == []
    assert t.counters()["citation_grounding_rate"] == 1.0


def test_prose_glued_onto_a_cjk_path_resolves_to_the_fetched_page():
    """Keeping ideographs in the extractor swallows glued prose into the match;
    whether the tail is path or prose is decidable only against the fetch record,
    so the trimmed form wins exactly when a record exists for it."""
    t = build_trail(
        CJK_LEDGER,
        "\u8be6\u89c1 https://zh.wikipedia.org/wiki/\u674e\u5f66\u5b8f\u8bcd\u6761\u4e2d\u7684\u8bf4\u660e",
    )
    assert t.cited == ["https://zh.wikipedia.org/wiki/\u674e\u5f66\u5b8f"]
    assert t.cited_not_opened == []


def test_a_fabricated_cjk_path_is_still_caught():
    """Trimming never invents a match: a CJK path with no fetch record behind any
    trailing-ideograph form stays an accusation, reported as written."""
    t = build_trail(CJK_LEDGER, "\u89c1 https://zh.wikipedia.org/zh-hans/\u6587\u5fc3\u4e00\u8a00 \u3002")
    assert t.cited_not_opened == ["https://zh.wikipedia.org/zh-hans/\u6587\u5fc3\u4e00\u8a00"]
    assert t.counters()["citation_grounding_rate"] == 0.0


def test_memo_pages_nobody_cited_do_not_inflate_the_scope_count():
    """The live defect: a warning listed one link while the scope line said
    "(2 of those...)" - the 2 was every memo page, cited or not, so the sentence
    named a number with no relation to the links beside it."""
    t = build_trail(
        LEDGER,
        "see https://never.example/x",
        ["https://memo.example/a", "https://memo.example/b"],
    )
    assert t.opened_earlier == 0
    assert t.cited_not_opened == ["https://never.example/x"]
    assert "earlier turn" not in t.render()


def test_the_scope_sentence_counts_cited_links_and_reads_coherently():
    answer = "per https://earlier.example/p2 and https://never.example/x"
    t = build_trail(LEDGER, answer, ["https://earlier.example/p2", "https://memo.example/unused"])
    assert t.opened_earlier == 1
    assert "1 of the cited links was opened on an earlier turn" in t.render()


# ── the salvage seam ships unreviewed, and the trail must say so ───────


def test_a_salvaged_answer_cannot_wear_a_reviewed_trail():
    """A salvaged answer never reaches the reviewer (observer order, ``dr.py``),
    and its trail used to simply omit the reviewer segment - silence a reader
    cannot tell apart from "not configured". The ledger row renders as an
    explicit skip, and the counter makes the salvage share of an arm measurable
    next to ``verify`` outcomes."""
    rows = [r for r in LEDGER if r["op"] != "verify"] + [
        {"op": "force_finalize", "event": "salvage", "seam": "iteration"}
    ]
    t = build_trail(rows, "x")
    assert t.salvaged is True
    assert t.counters()["salvaged"] is True
    assert "reviewer: skipped (salvaged answer)" in t.render()


def test_a_reviewed_turn_does_not_say_skipped():
    t = build_trail(LEDGER, "x")
    assert t.counters()["salvaged"] is False
    assert "skipped" not in t.render()


def test_a_salvage_after_a_rejection_does_not_hide_behind_the_verdict():
    """The common salvage path runs THROUGH a verdict: reject -> revision ->
    empty visible answer -> salvage. The verdict was about a draft that never
    shipped, so rendered alone it dresses the salvage as a reviewed turn - the
    exact trail this seam's visibility fix exists to prevent."""
    rows = list(LEDGER) + [
        {"op": "verify", "outcome": "rejected", "unsupported_claims": ["c1"]},
        {"op": "force_finalize", "event": "salvage", "seam": "terminal"},
    ]
    t = build_trail(rows, "x")
    rendered = t.render()
    assert "reviewer: rejected (1 open point)" in rendered
    assert "shipped answer: salvaged (not reviewed)" in rendered


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


# ── citation-tail and truncated-citation recoveries (both set-aware) ──────


DATE_TAIL_LEDGER = [
    {"op": "fetch", "url": "https://finance.sina.com.cn/roll/doc-abc.shtml", "chars": 900, "ok": True},
    {"op": "fetch", "url": "https://news.qq.com/rain/a/20260109A024R500", "chars": 900, "ok": True},
]


def test_a_comma_date_citation_tail_resolves_to_the_fetched_page():
    """Observed live: the model cites ``(url,2026-01-09)`` and the extractor
    swallows the comma and date into the URL. The tail comes off only because a
    fetch record exists for the clipped form."""
    t = build_trail(
        DATE_TAIL_LEDGER,
        "\u6765\u6e90\uff1ahttps://finance.sina.com.cn/roll/doc-abc.shtml,2026-01-09 \u62a5\u9053",
    )
    assert t.cited == ["https://finance.sina.com.cn/roll/doc-abc.shtml"]
    assert t.cited_not_opened == []


def test_two_citations_glued_by_a_semicolon_recover_the_first():
    """The extractor hands back one string; clipping at the first separator
    recovers the first citation. The second is lost to the glue, which
    understates ``cited`` rather than accusing anything - the safe direction."""
    t = build_trail(
        DATE_TAIL_LEDGER,
        "\u89c1 https://finance.sina.com.cn/roll/doc-abc.shtml,2026-01-09;"
        "https://news.qq.com/rain/a/20260109A024R500,2026-01-09 \u4e24\u5904",
    )
    assert t.cited == ["https://finance.sina.com.cn/roll/doc-abc.shtml"]
    assert t.cited_not_opened == []


def test_a_comma_tail_with_no_fetch_record_stays_an_accusation():
    """Reported AS WRITTEN, tail included - clipping is set-aware, so a miss
    rewrites nothing, same as the fabricated-CJK-path precedent."""
    t = build_trail(DATE_TAIL_LEDGER, "\u89c1 https://finance.sina.com.cn/roll/doc-xyz.shtml,2026-01-09 \u3002")
    assert t.cited_not_opened == ["https://finance.sina.com.cn/roll/doc-xyz.shtml,2026-01-09"]


PREFIX_LEDGER = [
    {
        "op": "fetch",
        "url": (
            "https://www.idc.com/resource-center/blog/%E6%99%BA%E8%83%BD%E4%BD%93token"
            "%E6%B6%88%E8%80%97%E5%B9%B4%E5%9D%87%E5%A2%9E%E8%B6%8530%E5%80%8D%E4%B8%AD%E5%9B%BD/"
        ),
        "chars": 5000,
        "ok": True,
    },
]


def test_a_truncated_citation_that_prefixes_one_opened_page_matches_it():
    """Observed live: the model truncated a long ideograph path when citing. The
    cited form is a strict prefix of exactly one opened page, so it matches -
    the SHALLOWER direction, which invents nothing deeper than what was read."""
    t = build_trail(
        PREFIX_LEDGER,
        "\u6765\u6e90\uff1ahttps://www.idc.com/resource-center/blog/"
        "\u667a\u80fd\u4f53token\u6d88\u8017\u5e74\u5747\u589e\u8d8530\u500d \u4e00\u6587",
    )
    assert t.cited_not_opened == []
    assert t.counters()["citation_grounding_rate"] == 1.0


def test_a_short_prefix_does_not_absolve_itself_against_a_deeper_page():
    """A string prefix is not a path prefix: ``/a`` must not match ``/about``.
    The path floor keeps short-segment citations in accusation territory."""
    rows = [{"op": "fetch", "url": "https://ex.example/about-the-project", "chars": 10, "ok": True}]
    t = build_trail(rows, "\u89c1 https://ex.example/a \u9875\u9762")
    assert t.cited_not_opened == ["https://ex.example/a"]


def test_an_ambiguous_prefix_stays_an_accusation():
    """Two opened pages share the cited prefix: the citation is ambiguous, and
    the check accuses rather than guesses."""
    rows = [
        {"op": "fetch", "url": "https://ex.example/reports/2026-industry-alpha", "chars": 10, "ok": True},
        {"op": "fetch", "url": "https://ex.example/reports/2026-industry-beta", "chars": 10, "ok": True},
    ]
    t = build_trail(rows, "\u89c1 https://ex.example/reports/2026-industry \u62a5\u544a")
    assert t.cited_not_opened == ["https://ex.example/reports/2026-industry"]


# --------------------------------------------------------------------------- #
# Where the rendered trail is parked                                          #
# --------------------------------------------------------------------------- #


def test_the_rendered_trail_is_kept_where_a_host_can_read_it(tmp_path, monkeypatch):
    """The appendix rides on the reply; the trail has to survive somewhere else.

    Riding on the outbound string alone is the whole of the appendix's
    distribution neutrality - the persisted transcript never carries it, so no
    model reads it this turn or as history next turn. That also means the only
    machine-readable copy is the one parked beside the counters, and the turn's
    ledger file is deleted moments later. Measured once on a live turn: 17 pages
    read, 8 cited, and the record of the other 9 built and then dropped.

    Kept OUT of ``process_appendix``: those are counters a reader parses as a
    measurement payload, and a multi-kilobyte string does not belong beside them.
    """
    import asyncio

    from research_flow.config import FlowConfig
    from research_flow.flow import ResearchFlowHook, ToolHandles
    from research_flow.state import SessionStore
    from research_flow.support import ledger as ledger_mod

    from raven.contracts.loop_hooks import AgentHookContext

    store = SessionStore(tmp_path)
    monkeypatch.setattr(ledger_mod, "_ledger_dir", store.ledger_dir)
    cfg = FlowConfig.from_slice({"enabled": True, "finalShape": {"processAppendix": True}})
    hook = ResearchFlowHook(cfg=cfg, provider=None, tools=ToolHandles(), store=store)

    async def turn():
        ctx = AgentHookContext(
            session_key="s",
            iteration=1,
            messages=[{"role": "user", "content": "q"}],
            metadata={"mode": "", "mode_overlay": {"drFlow": {}}},
        )
        await hook.before_iteration(ctx)
        path = ledger_mod.ledger_path()
        Path(path).write_text("\n".join(json.dumps(r) for r in LEDGER) + "\n", encoding="utf-8")
        return await hook.after_send(
            AgentHookContext(session_key="s", outbound_content="the answer cites https://a.example/one")
        )

    decision = asyncio.run(turn())
    observers = store.load("s").observers

    assert "Research trail" in (decision.modified_content or ""), "the appendix still rides on the reply"
    assert "Research trail" in observers["research_trail"]
    assert observers["research_trail"] in (decision.modified_content or "")
    assert observers["process_appendix"]["emitted"] is True
    assert "research_trail" not in observers["process_appendix"]


def test_turn_observers_reach_the_trunk_stamp_seam(tmp_path):
    """[B-1] The trunk stamps ``ctx.metadata["observers"]`` onto the turn's last
    substantive assistant message at persist -- the seam the fork's loop had
    natively. The flow must feed it at turn end, or only the record store's
    latest-turn copy survives and the per-turn history the fork kept is gone.
    The send context shares the turn's metadata dict in production, so the
    test hands it the same object the inbound phase saw.
    """
    import asyncio

    from research_flow.config import FlowConfig
    from research_flow.flow import ResearchFlowHook, ToolHandles
    from research_flow.state import SessionStore

    from raven.contracts.loop_hooks import AgentHookContext

    store = SessionStore(tmp_path)
    cfg = FlowConfig.from_slice({"enabled": True})
    hook = ResearchFlowHook(cfg=cfg, provider=None, tools=ToolHandles(), store=store)

    async def turn():
        first = AgentHookContext(
            session_key="s",
            iteration=1,
            messages=[{"role": "user", "content": "q"}],
            metadata={"mode": "", "mode_overlay": {"drFlow": {}}},
        )
        await hook.before_iteration(first)
        send_ctx = AgentHookContext(session_key="s", outbound_content="the answer", metadata=first.metadata)
        await hook.after_send(send_ctx)
        return send_ctx

    send_ctx = asyncio.run(turn())
    stamped = send_ctx.metadata.get("observers")
    assert isinstance(stamped, dict) and stamped, "after_send must feed the stamp seam"
    assert stamped == store.load("s").observers, "the stamp and the record read the same counters"
