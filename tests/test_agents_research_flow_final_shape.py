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

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FinalShapeConfig, FlowConfig  # noqa: E402
from research_flow.gates.final_shape import (  # noqa: E402
    ANSWER_LINE_PREFIX,
    shape_final_answer,
)


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
    for cfg in (FinalShapeConfig(), FlowConfig().final_shape):
        assert cfg.record is True
        assert cfg.require_marker is True
        assert cfg.report_structure is True
    # Every boolean here is on except the ones named below, each of which owes a
    # reason. Narrowing this is the deliberate act the previous comment asked
    # for, not a way to let a default drift.
    #
    # ``report_bounce`` (dr@3.4) is off because it is the one knob here that
    # spends a whole extra generation, and its benefit is not yet priced: the
    # session record stamps ``flow_version`` but never stamped which finalShape
    # knobs were on, so the 43%-malformed baseline that motivated it is an upper
    # bound that cannot be attributed. dr@3.4 adds the ``report_shape`` observer
    # that closes that gap; the knob earns its default from those readings, not
    # from this file.
    # ``report_depth`` is off because it is an unmeasured prompt bundle: the
    # deep template changes what the model reads on any arm that opts in. It
    # earns a default flip from a bench round, not from this file; under the
    # launch convention the label is a batch's concern, not this knob's.
    off_by_design = {"report_bounce", "report_depth"}
    bools = {
        n: getattr(FinalShapeConfig(), n) for n, f in FinalShapeConfig.model_fields.items() if f.annotation is bool
    }
    assert all(v for n, v in bools.items() if n not in off_by_design), bools
    assert not any(bools[n] for n in off_by_design), bools


def test_the_default_cannot_reach_a_flow_off_arm(tmp_path):
    """Why the flip above is safe, stated as a test rather than a comment.

    Anchors set ``drFlow.enabled=false``; the hop is assembled inside the flow.
    Both defaults on must still leave a flow-off arm with nothing attached, or
    the reference frame every historical A-minus-B rests on has moved.

    The fork asserted ``build_dr_flow(...) is None``; the plugin's assembly
    seam is the ``make_hook`` factory, which contributes nothing on a flow-off
    slice.
    """
    from types import SimpleNamespace

    from research_flow import plugin as plugin_module
    from research_flow.support.ledger import set_ledger_dir

    workspace = tmp_path / "anchor"
    ctx = SimpleNamespace(
        config={"enabled": False},
        services=SimpleNamespace(workspace=workspace, provider=None),
    )
    try:
        assert plugin_module.make_hook(ctx) is None
    finally:
        plugin_module._SHARED.pop(str(workspace), None)
        set_ledger_dir(None)


def test_off_state_contract_is_byte_identical_to_pre_flip():
    """Turning ``require_marker`` off must restore the dr@2.5 prompt exactly.

    This is the escape hatch for measurement: a batch compared against a
    reading taken before the flip has to be able to reproduce that prompt, and
    "we set the flag back" is only true if the bytes agree.

    Asserted at the config layer, because that is the layer an operator flips;
    a test that only reached the builder would pass while the config stopped
    forwarding the flag.
    """
    from research_flow.prompts import (
        _DR_ANSWER_MARKER_CLAUSE,
        render_identity_and_contract,
        render_parts,
    )

    # Both layers must agree on the default, or "the default contract" means two
    # different things depending on the call path - which is what the flow/anchor
    # contract test caught when only the config default was flipped.
    assert render_parts()[1] == render_parts(require_answer_marker=FinalShapeConfig().require_marker)[1]
    # ``report_structure`` is held off on both sides: this test is about one knob,
    # and letting the other vary would make it pass for the wrong reason.
    marker = _DR_ANSWER_MARKER_CLAUSE.format(n=6).strip()
    pre_flip = render_parts(require_answer_marker=False, report_structure=False)[1]
    off = render_identity_and_contract(
        FlowConfig(enabled=True, final_shape={"require_marker": False, "report_structure": False})
    )[1]
    on = render_identity_and_contract(FlowConfig(enabled=True, final_shape={"report_structure": False}))[1]
    assert marker not in off
    assert marker in on
    assert pre_flip.rstrip() in off


def test_depth_off_is_byte_identical_to_the_shipped_template():
    """Turning ``report_depth`` off must reproduce the dr@3.4 contract exactly.

    Same escape hatch as ``require_marker`` above, and the same two layers: the
    builder default and the config default must agree, and the config layer is
    the one an operator flips. A cheap pin - the off branch selects the shipped
    constant directly - while the sha-level protection stays with
    ``test_the_dr_prompt_bytes_match_the_batch_that_measured_them``.
    """
    from research_flow.prompts import render_identity_and_contract, render_parts

    assert render_parts()[1] == render_parts(report_depth=FinalShapeConfig().report_depth)[1]
    off = render_identity_and_contract(FlowConfig(enabled=True, final_shape={"report_depth": False}))[1]
    default = render_identity_and_contract(FlowConfig(enabled=True))[1]
    assert off == default
    assert off == render_parts()[1]


def test_depth_on_swaps_the_clause_without_moving_its_number():
    """The deep template replaces the dr@3.4 clause in place: same slot handling,
    same clause number, no residual ``{format_override}`` / ``{n}`` in any state.

    The closing-sentence change ("no other headings" -> "no other ``##``
    headings") is asserted from both sides - the deep sentence present, the
    template sentence absent - because the two constants must never drift into
    rendering the same closing rule.
    """
    from research_flow.prompts import render_identity_and_contract, render_parts

    deep = render_identity_and_contract(FlowConfig(enabled=True, final_shape={"report_depth": True}))[1]
    shipped = render_parts()[1]

    assert "the full report that carries the answer" in deep
    assert "Add no other `##` headings" in deep
    assert "Add no other headings" not in deep
    assert "Add no other headings" in shipped
    assert "Add no other `##` headings" not in shipped
    # Same clause number in both states: the deep clause substitutes for the
    # template inside the same ``enumerate`` slot, after the marker clause.
    assert "7. Write the reply as a research report" in deep
    assert "7. Write the reply as a research report" in shipped
    for state in (
        deep,
        render_parts(report_depth=True, report_format_override=False)[1],
    ):
        assert "{format_override}" not in state
        assert "{n}" not in state
    # The override passage rides the same slot in both templates.
    assert "takes precedence over any formatting instructions" in deep
    assert (
        "takes precedence over any formatting instructions"
        not in render_parts(report_depth=True, report_format_override=False)[1]
    )


def test_the_live_label_is_never_also_a_retired_one():
    """Named for the invariant rather than the number.

    A test whose name carries the current version has to be renamed at every bump, and
    a rename is the step that gets skipped - leaving a green test called
    ``test_version_is_dr26`` on a build that ships something else.
    """
    current = FlowConfig.model_fields["version"].default
    retired = FlowConfig._SUPERSEDED_VERSIONS
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
    from research_flow.prompts import _DR_ANSWER_MARKER_CLAUSE, render_parts

    off = render_parts(require_answer_marker=False, report_structure=False)[1]
    on = render_parts(report_structure=False)[1]
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
    import hashlib

    from research_flow.prompts import render_parts

    def sha(**kw):
        identity, contract = render_parts(**kw)
        text = identity + "\n\n" + contract
        return len(text), hashlib.sha256(text.encode()).hexdigest()[:16]

    # Every bench arm: both clauses off. Its readings are the whole corpus axis.
    assert sha(require_answer_marker=False, report_structure=False) == (3485, "593c46c416c3f4cf")
    # The product surface through dr@2.7: marker only. Renumbering must not move it.
    assert sha(require_answer_marker=True, report_structure=False) == (3796, "7ad4b42cc78aec62")


def test_the_report_clause_is_product_only_and_purely_appended():
    """On the product surface it is added after the marker, and nothing moves.

    Also pins the numbering: the clauses continue the contract's list rather than
    restarting or colliding, and marker-off/report-on renumbers the report clause
    down to 6 instead of leaving a hole at 6 and a 7 with nothing before it.
    """
    from research_flow.prompts import (
        _DR_ANSWER_MARKER_CLAUSE,
        _DR_REPORT_FORMAT_OVERRIDE_PASSAGE,
        _DR_REPORT_STRUCTURE_CLAUSE,
        render_parts,
    )

    report_clause = _DR_REPORT_STRUCTURE_CLAUSE.replace("{format_override}", _DR_REPORT_FORMAT_OVERRIDE_PASSAGE)

    bench = render_parts(require_answer_marker=False, report_structure=False)[1]
    product = render_parts(require_answer_marker=True, report_structure=True)[1]

    assert product.startswith(bench.rstrip()), "appended only; no prior byte moved"
    assert _DR_ANSWER_MARKER_CLAUSE.format(n=6).strip() in product
    assert report_clause.format(n=7).strip() in product

    report_only = render_parts(require_answer_marker=False, report_structure=True)[1]
    assert report_clause.format(n=6).strip() in report_only
    assert "7." not in report_only.split("Contract", 1)[1]

    # The override rides its own switch inside the clause: off leaves the
    # pre-override template with no seam, and no other byte of the clause moves.
    no_override = render_parts(require_answer_marker=True, report_structure=True, report_format_override=False)[1]
    template_only = _DR_REPORT_STRUCTURE_CLAUSE.replace("{format_override}", "")
    assert template_only.format(n=7).strip() in no_override
    assert _DR_REPORT_FORMAT_OVERRIDE_PASSAGE.strip() not in no_override


def test_the_report_clause_never_asks_for_a_shorter_answer():
    """The one property that separates this from the failure class it imitates.

    An upstream framework's answer-extraction stage carried gold on 71/120
    questions into 58/120 boxed fields - it invented nothing and dropped 10.83pp -
    and dr@1.6's salvage seam failed the same way. A clause that asked for brevity,
    or for the answer alone, would make the model perform that truncation during
    generation, where no downstream transform can refuse it.
    """
    from research_flow.prompts import (
        _DR_REPORT_FORMAT_OVERRIDE_PASSAGE,
        _DR_REPORT_STRUCTURE_CLAUSE,
    )

    text = (
        _DR_REPORT_STRUCTURE_CLAUSE.replace("{format_override}", _DR_REPORT_FORMAT_OVERRIDE_PASSAGE).format(n=6).lower()
    )
    for banned in ("concise", "brief", "summarize", "only the answer", "keep it short"):
        assert banned not in text, f"the clause asks for {banned!r}"
    assert "never" in text and "drop evidence" in text


def test_a_literal_brace_in_a_clause_does_not_crash_the_assembly(monkeypatch):
    """dr@3.4. The clause text talks about JSON, so a brace must stay inert.

    It did not: the override passage was spliced with ``replace`` (safe) and the
    clause numbering right after it ran ``format`` (not), so one example object in
    prompt text raised ``KeyError`` while building the system prompt - a crash
    reachable only by editing a constant, which is exactly the kind nobody meets
    until they are mid-edit on something else. The comment above the passage
    promised the safety this test now enforces.
    """
    import research_flow.prompts as prompts_module

    monkeypatch.setattr(
        prompts_module,
        "_DR_REPORT_FORMAT_OVERRIDE_PASSAGE",
        prompts_module._DR_REPORT_FORMAT_OVERRIDE_PASSAGE.rstrip() + '\n   Example: {"k": "v"}.\n',
    )
    contract = prompts_module.render_parts()[1]
    assert '{"k": "v"}' in contract, "the brace must survive verbatim, not be consumed"


def test_a_contract_override_owns_the_contract_including_both_clauses():
    """A profile that replaces the contract gets neither clause appended.

    The override exists for graders that parse a token at the end of the output -
    two of them ship in ``configs/futurex_*.json`` - so appending anything after it
    relocates the one thing such a profile is written to control.
    """
    from research_flow.prompts import render_parts

    own = render_parts("# My Contract\n\n1. do the thing", require_answer_marker=True, report_structure=True)[1]
    assert own == "# My Contract\n\n1. do the thing"


def test_assembly_carries_the_record_flag_and_anchor_gets_nothing(tmp_path):
    """``make_hook`` returns None with the flow off, so the seam that reads
    ``final_shape.record`` has nothing to read on the anchor.

    The fork's assembly copied the flag onto ``record_final_shape``; the plugin
    threads the whole config to the recording seam (``TurnFrame.after_send``),
    so the flag is read from the built hook's ``cfg``. ``verify`` is turned off
    in the enabled slices because this factory - unlike the fork's builder -
    installs nothing at all when an LLM gate is on with no provider lent.
    """
    from types import SimpleNamespace

    from research_flow import plugin as plugin_module
    from research_flow.support.ledger import set_ledger_dir

    def _ctx(name, slice_):
        return SimpleNamespace(
            config=slice_,
            services=SimpleNamespace(workspace=tmp_path / name, provider=None),
        )

    try:
        assert plugin_module.make_hook(_ctx("off", {"enabled": False})) is None

        asm = plugin_module.make_hook(_ctx("on", {"enabled": True, "verify": {"enabled": False}}))
        assert asm is not None and asm.cfg.final_shape.record is True

        asm_off = plugin_module.make_hook(
            _ctx(
                "record-off",
                {"enabled": True, "verify": {"enabled": False}, "finalShape": {"record": False}},
            )
        )
        assert asm_off is not None and asm_off.cfg.final_shape.record is False
    finally:
        for name in ("off", "on", "record-off"):
            plugin_module._SHARED.pop(str(tmp_path / name), None)
        set_ledger_dir(None)


def test_counters_payload_is_wire_safe_scalars():
    r = shape_final_answer("Body.\n<answer>X</answer>")
    c = r.counters()
    assert set(c) == {
        "form",
        "marked",
        "shaped",
        "reason",
        "visible_chars",
        "shaped_chars",
        "span_chars",
    }
    for v in c.values():
        assert isinstance(v, (str, int, bool)), v


# dr@3.4. Table-driven pin of the marker grammar - a pure function is the
# cheapest thing in the package to pin exhaustively, and two of these shapes
# were extracted wrong for two versions: ``**Final Answer:** 42`` yielded
# ``** 42`` (the regex allowed ``**`` before the colon but not after), and
# ``\boxed{\frac{1}{2}}`` was cut to ``\frac{1`` by a lazy group, on exactly
# the nested-brace inputs boxed exists for.
_MARKER_FORMS = [
    ("<answer>Berlin</answer>", "answer_tag", "Berlin"),
    ("prose\n<answer>a\nb</answer>", "answer_tag", "a\nb"),
    (r"\boxed{42}", "boxed", "42"),
    (r"so \boxed{\frac{1}{2}} holds", "boxed", r"\frac{1}{2}"),
    (r"\boxed{a_{1}b_{2}}", "boxed", r"a_{1}b_{2}"),
    ("Answer: Paris", "labeled", "Paris"),
    ("Final Answer: Paris", "labeled", "Paris"),
    ("**Final Answer:** 42", "labeled", "42"),
    ("**Final Answer**: 42", "labeled", "42"),
    ("- Answer: Paris", "labeled", "Paris"),
    ("> Answer: Paris", "labeled", "Paris"),
    # The CJK "answer" label row from the fork, spelled as escapes so this file
    # carries no CJK characters (same convention as the module under test).
    ("\u7b54\u6848\uff1a\u5317\u4eac", "labeled", "\u5317\u4eac"),
    ("Answer: line one\nline two", "labeled", "line one\nline two"),
    ("Answer: **Paris**", "labeled", "Paris"),
    ("Answer: the **bold** middle", "labeled", "the **bold** middle"),
]


@pytest.mark.parametrize("text,form,span", _MARKER_FORMS)
def test_marker_form_table(text, form, span):
    r = shape_final_answer(text)
    assert r.form == form
    assert r.span == span


def test_unbalanced_boxed_yields_nothing_rather_than_a_guess():
    r = shape_final_answer(r"\boxed{\frac{1}{2}")
    assert r.form == "unmarked" and r.span is None


def test_mid_sentence_answer_colon_still_does_not_match():
    r = shape_final_answer("the answer: it depends on context")
    assert r.form == "unmarked"
