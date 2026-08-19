"""Terminal-answer shaping (dr@2.5; on by default since dr@2.6).

The tests that matter here are the negative ones. A shaper that only ever
improves things is easy to write and easy to break later; what has to be pinned
is that it *cannot* blank or shorten a record, because that is the failure class
it was built against (an upstream extraction stage carried gold on 71/120 into a
boxed field on 58/120 - net zero produced, 10.83pp dropped).

So every positive case below is paired with the corresponding refusal, and the
class defaults are asserted directly: the anchor arms run this same code path,
so a default flip would move the reference frame without any test failing.
"""

from __future__ import annotations

from raven.agent.flow.final_shape import (
    ANSWER_LINE_PREFIX,
    shape_final_answer,
)
from raven.config.raven import DRFlowConfig, DRFlowFinalShapeConfig


def _content(s: str) -> str:
    return "".join(s.split())


# --------------------------------------------------------------------------
# Class defaults — the anchor's reference frame
# --------------------------------------------------------------------------


def test_both_knobs_default_on():
    """The shipped defaults: a caller who configures nothing gets the product.

    Enumerated field by field rather than spot-checked. The knob added in dr@2.8
    went in under a test named "both knobs" that asserted the other two and passed
    while the new default was inverted - a name that stops describing its subject
    is how a default ships unguarded.
    """
    for cfg in (DRFlowFinalShapeConfig(), DRFlowConfig().final_shape):
        assert cfg.record is True
        assert cfg.require_marker is True
        assert cfg.report_structure is True
    # Every boolean here is on. If a future knob should default off, it needs its
    # own reason written down and this assertion narrowed deliberately.
    bools = {n: getattr(DRFlowFinalShapeConfig(), n)
             for n, f in DRFlowFinalShapeConfig.model_fields.items()
             if f.annotation is bool}
    assert all(bools.values()), bools


def test_the_default_cannot_reach_a_flow_off_arm():
    """Why the flip above is safe, stated as a test rather than a comment.

    Anchors set ``drFlow.enabled=false``; the hop is assembled inside the flow.
    Both defaults on must still leave a flow-off arm with nothing attached, or
    the reference frame every historical A-minus-B rests on has moved.
    """
    from raven.agent.flow.dr import build_dr_flow

    assert build_dr_flow(DRFlowConfig(enabled=False), None, 10, 1000) is None


def test_off_state_contract_is_byte_identical_to_pre_flip():
    """Turning ``require_marker`` off must restore the dr@2.5 prompt exactly.

    This is the escape hatch for measurement: a batch compared against a
    reading taken before the flip has to be able to reproduce that prompt, and
    "we set the flag back" is only true if the bytes agree.

    Asserted at the config layer, because that is the layer an operator flips;
    a test that only reached the builder would pass while the config stopped
    forwarding the flag.
    """
    from raven.agent.flow.dr import (
        _DR_ANSWER_MARKER_CLAUSE,
        DRModeSegmentBuilder,
        build_dr_flow,
    )

    # Both layers must agree on the default, or "the default contract" means two
    # different things depending on the call path - which is what the flow/anchor
    # contract test caught when only the config default was flipped.
    assert DRModeSegmentBuilder()._contract == DRModeSegmentBuilder(
        require_answer_marker=DRFlowFinalShapeConfig().require_marker
    )._contract
    # ``report_structure`` is held off on both sides: this test is about one knob,
    # and letting the other vary would make it pass for the wrong reason.
    marker = _DR_ANSWER_MARKER_CLAUSE.format(n=6).strip()
    pre_flip = DRModeSegmentBuilder(
        require_answer_marker=False, report_structure=False
    )._contract
    off = build_dr_flow(
        DRFlowConfig(
            enabled=True, final_shape={"require_marker": False, "report_structure": False}
        ), None, 10, 1000
    )
    on = build_dr_flow(
        DRFlowConfig(enabled=True, final_shape={"report_structure": False}), None, 10, 1000
    )
    assert marker not in off.segment_builder._contract
    assert marker in on.segment_builder._contract
    assert pre_flip.rstrip() in off.segment_builder._contract


def test_the_live_label_is_never_also_a_retired_one():
    """Named for the invariant rather than the number.

    A test whose name carries the current version has to be renamed at every bump, and
    a rename is the step that gets skipped - leaving a green test called
    ``test_version_is_dr26`` on a build that ships something else.
    """
    current = DRFlowConfig.model_fields["version"].default
    retired = DRFlowConfig._SUPERSEDED_VERSIONS.default
    assert current not in retired
    for label in ("dr@1.4", "dr@2.0", "dr@2.4", "dr@2.5", "dr@2.6"):
        assert label in retired, f"{label} shipped once; it must stay indexable"


# --------------------------------------------------------------------------
# The core guarantee: additive, never lossy
# --------------------------------------------------------------------------


def test_never_shortens_content_for_any_marker_form():
    bodies = [
        "Reasoning about the question.\n\n<answer>Cherokee Nations</answer>",
        "Working through it.\n\nThe result is \\boxed{1980}.",
        "Long analysis here.\n\n**Answer:** Marguerite Smith",
        "Analysis.\n\nFinal Answer: 10.5812/ijpbs.62774",
        "no marker at all, just prose about the topic",
        "",
        "   \n  ",
    ]
    for raw in bodies:
        r = shape_final_answer(raw)
        assert len(_content(r.text)) >= len(_content(r.visible)), raw[:40]
        assert r.reason != "refused_shorter", raw[:40]


def test_empty_input_stays_empty():
    """Cannot invent an answer. This is the same property as cannot lose one."""
    for raw in ("", None, "   ", "<think>only reasoning</think>"):
        r = shape_final_answer(raw)
        assert r.text == ""
        assert r.form == "empty"
        assert r.shaped is False
        assert r.marked is False


def test_unmarked_answer_passes_through_byte_identical():
    raw = "Apple acquired PrimeSense in 2013, per TechCrunch."
    r = shape_final_answer(raw)
    assert r.text == raw
    assert r.form == "unmarked"
    assert r.shaped is False
    assert r.reason == "no_marker"


# --------------------------------------------------------------------------
# Marker extraction
# --------------------------------------------------------------------------


def test_answer_tag_extracted_and_body_kept():
    raw = "Evidence: the DLC added four factions.\n<answer>Cherokee Nations</answer>"
    r = shape_final_answer(raw)
    assert r.form == "answer_tag"
    assert r.span == "Cherokee Nations"
    assert r.marked is True
    # Additive: the body survives and a canonical line is present.
    assert "Evidence: the DLC added four factions." in r.text
    assert r.text.rstrip().endswith(ANSWER_LINE_PREFIX + "Cherokee Nations")


def test_boxed_extracted():
    r = shape_final_answer("The station switched in \\boxed{1980} per the archive.")
    assert r.form == "boxed"
    assert r.span == "1980"
    assert r.text.rstrip().endswith(ANSWER_LINE_PREFIX + "1980")


def test_labeled_multiline_span_is_not_truncated_at_first_newline():
    raw = "Analysis.\n\n**Answer:** line one\nline two\nline three\n\nFootnote."
    r = shape_final_answer(raw)
    assert r.form == "labeled"
    assert "line one" in r.span and "line three" in r.span
    assert "Footnote." in r.text


def test_last_marker_wins_not_first():
    """A model that restates its answer has its final word at the end, and a
    prompt echo ("put it in \\boxed{}") must not beat the real answer."""
    raw = "I will put it in \\boxed{PLACEHOLDER} as asked.\n\nAfter research: \\boxed{1980}"
    assert shape_final_answer(raw).span == "1980"


def test_already_canonical_is_not_double_appended():
    raw = "Body text.\n\n" + ANSWER_LINE_PREFIX + "Verity"
    r = shape_final_answer(raw)
    assert r.text.count(ANSWER_LINE_PREFIX) == 1
    assert r.shaped is False
    assert r.reason == "already_canonical"


# --------------------------------------------------------------------------
# Refusals — the MiroFlow trap, both directions
# --------------------------------------------------------------------------


def test_empty_marker_refuses_and_keeps_the_original():
    """A marker with nothing in it is exactly the upstream failure: extraction
    turning an answer-bearing turn into an empty answer. Refuse, keep the text."""
    for raw in (
        "The evidence points there.\n<answer></answer>",
        "The evidence points there.\n<answer>   </answer>",
        "So we get \\boxed{}",
    ):
        r = shape_final_answer(raw)
        assert r.text.strip() != "", raw
        assert r.form == "unmarked", raw
        assert r.reason.startswith("refused_empty_marker:"), raw
        assert r.shaped is False, raw


def test_empty_strong_marker_does_not_fall_through_to_a_weaker_one():
    """An empty <answer> must not be rescued by a stray "Answer:" in the prose -
    falling through would silently swap which span is authoritative."""
    raw = "Answer: something in prose\n\n<answer></answer>"
    r = shape_final_answer(raw)
    assert r.reason == "refused_empty_marker:answer_tag"
    assert r.span is None


def test_closing_tag_required_is_forwarded_not_hardcoded():
    """The anchor runs with this False and the treated arm with it True; a hop
    that hardcoded either would report one arm's rate on the other's definition."""
    raw = "reasoning with no closing tag and an <answer>X</answer>"
    assert shape_final_answer(raw, closing_tag_required=False).marked is True
    assert shape_final_answer(raw, closing_tag_required=True).form == "empty"


# --------------------------------------------------------------------------
# The prompt clause — the one piece that IS a distribution change
# --------------------------------------------------------------------------


def test_prompt_clause_is_gated_and_appended_not_spliced():
    """Off-state contract must be byte-identical, and the clause appended.

    dr@2.0 lost a headline to a relocation that kept the char count and changed
    the sha, so the label described a prompt that never produced the number.
    Appending keeps every prior byte at its measured offset.
    """
    from raven.agent.flow.dr import _DR_ANSWER_MARKER_CLAUSE, DRModeSegmentBuilder

    off = DRModeSegmentBuilder(require_answer_marker=False, report_structure=False)._contract
    on = DRModeSegmentBuilder(report_structure=False)._contract
    assert off != on
    assert on.startswith(off.rstrip())
    assert _DR_ANSWER_MARKER_CLAUSE.format(n=6).strip() in on
    # Additive in both directions: nothing before the clause moved.
    assert len(on) > len(off)


def test_bench_and_pre_dr28_product_segments_are_byte_identical_to_their_stamps():
    """The two shas every landed reading was taken under, pinned as literals.

    dr@2.8 numbers the optional clauses at assembly instead of inside their
    constants, so that a second clause can be switched on independently without
    leaving a gap in the list. That refactor touches the marker clause's first
    byte, which is exactly the kind of "harmless" edit that cost dr@2.0 a
    headline - same char count, different sha, label describing a prompt that
    never produced the number. These literals are the only thing standing between
    that and a silent re-labelling of every published corpus and web reading.
    """
    import asyncio
    import hashlib

    from raven.agent.flow.dr import DRModeSegmentBuilder

    def sha(**kw):
        text = asyncio.run(DRModeSegmentBuilder(**kw).build(None)).text
        return len(text), hashlib.sha256(text.encode()).hexdigest()[:16]

    # Every bench arm: both clauses off. Its readings are the whole corpus axis.
    assert sha(require_answer_marker=False, report_structure=False) == (
        3485, "593c46c416c3f4cf")
    # The product surface through dr@2.7: marker only. Renumbering must not move it.
    assert sha(require_answer_marker=True, report_structure=False) == (
        3796, "7ad4b42cc78aec62")


def test_the_report_clause_is_product_only_and_purely_appended():
    """On the product surface it is added after the marker, and nothing moves.

    Also pins the numbering: the clauses continue the contract's list rather than
    restarting or colliding, and marker-off/report-on renumbers the report clause
    down to 6 instead of leaving a hole at 6 and a 7 with nothing before it.
    """
    from raven.agent.flow.dr import (
        _DR_ANSWER_MARKER_CLAUSE,
        _DR_REPORT_STRUCTURE_CLAUSE,
        DRModeSegmentBuilder,
    )

    bench = DRModeSegmentBuilder(require_answer_marker=False, report_structure=False)._contract
    product = DRModeSegmentBuilder(require_answer_marker=True, report_structure=True)._contract

    assert product.startswith(bench.rstrip()), "appended only; no prior byte moved"
    assert _DR_ANSWER_MARKER_CLAUSE.format(n=6).strip() in product
    assert _DR_REPORT_STRUCTURE_CLAUSE.format(n=7).strip() in product

    report_only = DRModeSegmentBuilder(
        require_answer_marker=False, report_structure=True
    )._contract
    assert _DR_REPORT_STRUCTURE_CLAUSE.format(n=6).strip() in report_only
    assert "7." not in report_only.split("Contract", 1)[1]


def test_the_report_clause_never_asks_for_a_shorter_answer():
    """The one property that separates this from the failure class it imitates.

    An upstream framework's answer-extraction stage carried gold on 71/120
    questions into 58/120 boxed fields - it invented nothing and dropped 10.83pp -
    and dr@1.6's salvage seam failed the same way. A clause that asked for brevity,
    or for the answer alone, would make the model perform that truncation during
    generation, where no downstream transform can refuse it.
    """
    from raven.agent.flow.dr import _DR_REPORT_STRUCTURE_CLAUSE

    text = _DR_REPORT_STRUCTURE_CLAUSE.format(n=6).lower()
    for banned in ("concise", "brief", "summarize", "only the answer", "keep it short"):
        assert banned not in text, f"the clause asks for {banned!r}"
    assert "never" in text and "drop evidence" in text


def test_a_contract_override_owns_the_contract_including_both_clauses():
    """A profile that replaces the contract gets neither clause appended.

    The override exists for graders that parse a token at the end of the output -
    two of them ship in ``configs/futurex_*.json`` - so appending anything after it
    relocates the one thing such a profile is written to control.
    """
    from raven.agent.flow.dr import DRModeSegmentBuilder

    own = DRModeSegmentBuilder(
        "# My Contract\n\n1. do the thing", require_answer_marker=True, report_structure=True
    )._contract
    assert own == "# My Contract\n\n1. do the thing"


def test_assembly_carries_the_record_flag_and_anchor_gets_nothing():
    """``build_dr_flow`` returns None with the flow off, so the seam that reads
    ``record_final_shape`` has nothing to read on the anchor."""
    from raven.agent.flow.dr import build_dr_flow

    assert build_dr_flow(DRFlowConfig(enabled=False), None, 10, 1000) is None

    asm = build_dr_flow(DRFlowConfig(enabled=True), None, 10, 1000)
    assert asm is not None and asm.record_final_shape is True

    cfg_off = DRFlowConfig(enabled=True, final_shape={"record": False})
    asm_off = build_dr_flow(cfg_off, None, 10, 1000)
    assert asm_off is not None and asm_off.record_final_shape is False


def test_counters_payload_is_wire_safe_scalars():
    r = shape_final_answer("Body.\n<answer>X</answer>")
    c = r.counters()
    assert set(c) == {
        "form", "marked", "shaped", "reason",
        "visible_chars", "shaped_chars", "span_chars",
    }
    for v in c.values():
        assert isinstance(v, (str, int, bool)), v
