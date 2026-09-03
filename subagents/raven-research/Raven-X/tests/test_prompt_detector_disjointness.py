"""The prompt vocabulary and the detector vocabulary must not overlap.

A detector that matches substrings in the model's own output is defeated by
putting its trigger phrase in the model's instructions, because the model
echoes what it is told. dr@1.9's headline defect was exactly this: the identity
segment said "retry with a different approach" every turn, the restart detector
listed the bare substring "different approach", and research was force-terminated
on 57-58 of 120 treated questions while the same phrase sat on 75 anchor
questions carrying no signal at all.

Polarity does not save you. "Do not go in circles" teaches the model to write
"I am going in circles", which fires the same detector. So the rule is
containment-free: a trigger phrase may not appear in model-visible prompt text
in any polarity.

This test is the guard. It fails on the phrase, not on the intent, which is the
point - the previous defect survived several careful reviews.
"""

from __future__ import annotations

import pytest

from raven.agent import harness_text
from raven.agent.flow import (
    ask_user,
    ask_user_tool,
    budget_note,
    dr,
    fetch_floor,
    finalize,
    report_shape,
    spin_breaker,
    sufficiency,
    verify,
)
from raven.agent.loop import main as loop_main
from raven.agent.tools import registry as tool_registry
from raven.agent.tools import web
from raven.context_engine.segments import render

# Modules whose module-level string constants reach the model.
#
# 20260813 added ``tool_registry`` and ``render``:
#   · ``tool_registry._TOOL_ERROR_HINT`` is appended to four tool-error return paths
#     and reaches EVERY arm including DR. It carried "a different approach" - the
#     idiom dr@1.9 had to delete from ``_RESTART_MARKERS`` - while sitting outside
#     this guard. ⚠️ It was a FUNCTION-LOCAL until 20260813, so listing the module
#     here would not have found it: ``vars(module)`` only sees module level. The
#     hoist in ``registry.py`` is what makes this entry mean anything.
#   · ``render.RAVEN_GUIDELINES`` is the identity guideline block, now defined once
#     and imported by ``ContextBuilder`` (it used to exist twice, byte-identical).
#
# 20260819 added ``report_shape`` (dr@3.4). Its reminder rides the END of the current
# user message, which makes it the highest-recency prompt text this build emits - the
# position a model is likeliest to echo, and echoed text is exactly what a detector
# then matches. Clean today; in scope from now on.
# 20260820 added ``ask_user_tool`` (dr@3.4-askuser). Its ``description`` and
# ``parameters`` are prompt text - they enter the tool schema the model reads - and
# both are PROPERTIES, so listing the module only catches its two fallback strings.
# The rendered pair is appended in ``_prompt_texts`` for that reason.
# ``ask_user`` (dr@3.4-askuser) alongside it: the handoff lead-in becomes the
# turn's reply and then this session's HISTORY, and history is the strongest thing
# a model echoes - which is what a detector then matches.
# 20260826 added ``sufficiency``. Its judge system prompt is model-visible text, and
# its release note lands in history appended to a tool result - the same channel the
# budget line uses, i.e. persisted and echoed back on every later iteration.
_PROMPT_MODULES = (
    dr, finalize, verify, fetch_floor, budget_note, web, tool_registry, render, report_shape,
    ask_user_tool, ask_user, sufficiency,
)

# Detector vocabularies: every list of literals matched against text the model
# produced. Add new ones here the moment a detector is written.
_DETECTOR_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("spin_breaker._RESTART_MARKERS", spin_breaker._RESTART_MARKERS),
    ("loop.main._TRANSIENT_FAILURE_MARKERS", loop_main._TRANSIENT_FAILURE_MARKERS),
    ("loop.main._EMPTY_SUCCESS_MARKERS", loop_main._EMPTY_SUCCESS_MARKERS),
)


def _prompt_texts() -> list[tuple[str, str]]:
    """Every model-visible string this build can put in a prompt.

    Module-level ``str`` constants plus the nudges that are built by a function,
    which a constant scan would miss - and the loop-break nudge was one of the
    two places the removed idiom lived.
    """
    out: list[tuple[str, str]] = []
    for module in _PROMPT_MODULES:
        for name, value in vars(module).items():
            if isinstance(value, str) and len(value) >= 40 and not name.startswith("__"):
                out.append((f"{module.__name__}.{name}", value))
    # Both branches: the DR one is what the treated arm sees, the product one is
    # frozen for the anchor and must not drift into a marker either.
    out.append(("loop.main._loop_break_nudge(dr)", loop_main._loop_break_nudge("web_search", 3, dr_mode=True)))
    out.append(("loop.main._loop_break_nudge(product)", loop_main._loop_break_nudge("web_search", 3, dr_mode=False)))
    # Assembled from a body constant plus delimiters, so the constant scan sees the
    # body but not the block the model actually reads.
    out.append(("flow.report_shape.render_reminder()", report_shape.render_reminder()))
    # ★ 20260826: the harness's own sentences. All three are model-visible - two ride
    # on a tool result, one is a tool return value - and all three were built by a
    # function in a module nothing here scanned, so a marker idiom in any of them was
    # outside this guard. ``search_closed_notice`` takes ``k``; one value is enough,
    # since the interpolation is a number.
    out.append(("harness_text.search_closed_notice()", harness_text.search_closed_notice(10)))
    out.append(("harness_text.fetch_gate_notice()", harness_text.fetch_gate_notice()))
    out.append(("harness_text.sufficiency_notice()", harness_text.sufficiency_notice()))
    out.append(("segments.render.identity_text()", render.identity_text.__doc__ or ""))
    # The anchor still renders the product identity; a regression there would
    # re-bias the control arm, so it is in scope even though DR mode drops it.
    import pathlib

    out.append(("segments.render.identity_text", render.identity_text(pathlib.Path("/tmp/ws"))))
    # ★ 20260813: the SECOND live identity path. ``context_engine.segments.identity``
    # renders the one above; ``ContextBuilder`` is what ``AgentLoop`` actually imports
    # (``loop/main.py:19``), and it assembles its own. Both were in scope all along and
    # only one was being scanned - so a marker added to the builder copy would have
    # passed this gate. They render byte-identical today; that is a fact to verify,
    # not to assume (see ``test_the_two_identity_paths_agree`` below).
    from raven.agent.context.builder import ContextBuilder

    out.append(("agent.context.ContextBuilder._get_identity",
                ContextBuilder(workspace=pathlib.Path("/tmp/ws"),
                               start_watcher=False)._get_identity()))
    # dr@3.4-askuser. Three surfaces a module-constant scan cannot see:
    #   · the on-state segment, whose identity is REWRITTEN rather than appended to,
    #     so the substituted sentences exist in no module-level string;
    #   · ``_ASK_USER_IDENTITY_SUBS``, a tuple of tuples - ``vars()`` yields the
    #     tuple, and ``isinstance(value, str)`` skips it;
    #   · the tool's ``description`` / ``parameters``, both properties.
    # Every one of them is text the model reads, and none of them was in scope.
    import asyncio
    import json

    # The rendered handoff and brief, not just their constants: both are assembled
    # from a lead-in plus model text, so a constant scan sees the lead-in and not
    # the block that reaches the model.
    pending = ask_user.PendingClarify(
        original_question="the original question",
        questions=[{"question": "which entity?", "options": ["a", "b"]}],
        outline=[{"goal": "g", "evidence": "e", "why": "w"}],
    )
    out.append(("flow.ask_user.render_handoff()", ask_user.render_handoff(pending)))
    out.append(("flow.ask_user.render_brief()", ask_user.render_brief(pending, "a")))
    for outline in (True, False):
        seg = asyncio.run(
            dr.DRModeSegmentBuilder(ask_user=True, ask_user_outline=outline).build(None)
        )
        out.append((f"flow.dr on-state segment (outline={outline})", seg.text))
        tool = ask_user_tool.DRAskUserTool(outline=outline)
        out.append((f"flow.ask_user_tool.description (outline={outline})", tool.description))
        out.append((f"flow.ask_user_tool.parameters (outline={outline})",
                    json.dumps(tool.parameters, ensure_ascii=False)))
    return out


@pytest.mark.parametrize("origin,text", _prompt_texts(), ids=lambda v: v if isinstance(v, str) and len(v) < 60 else "")
def test_no_prompt_text_contains_a_detector_marker(origin: str, text: str) -> None:
    lowered = text.lower()
    hits = [
        f"{marker!r} (from {source})"
        for source, markers in _DETECTOR_MARKERS
        for marker in markers
        if marker.lower() in lowered
    ]
    assert not hits, (
        f"{origin} contains detector trigger phrase(s): {', '.join(hits)}.\n"
        "The model echoes its instructions, so this phrase will fire the detector on "
        "the model's own text and the detector will measure our vocabulary, not the "
        "model's behaviour. Reword the prompt - do not widen the detector."
    )


def test_the_guideline_block_exists_exactly_once_in_the_tree() -> None:
    """One definition, not two copies that drift.

    The block lived in ``context_engine/segments/render.py`` AND in
    ``agent/context/builder.py`` byte-identically (1,040 chars), in two
    independently live prompt paths. Two copies of model-visible prompt text is a
    shape this project has paid for: the copy nobody edits keeps shipping, and an
    audit that greps one file passes while the other still carries the phrase.

    This test fails on a THIRD copy appearing, which is the realistic regression -
    someone pastes the block into a new assembler rather than importing it.
    """
    import pathlib as _p

    root = _p.Path(render.__file__).resolve().parents[2]        # …/raven
    needle = "- State intent before tool calls, but NEVER predict or claim results"
    carriers = sorted(
        str(f.relative_to(root))
        for f in root.rglob("*.py")
        if "__pycache__" not in str(f) and needle in f.read_text(encoding="utf-8", errors="replace")
    )
    assert carriers == ["context_engine/segments/render.py"], (
        f"the guideline block appears as a literal in {carriers}. It must be defined "
        "once (render.RAVEN_GUIDELINES) and imported. If you added an assembler, "
        "import the constant instead of pasting the text."
    )


def test_the_two_identity_paths_agree() -> None:
    """``ContextBuilder._get_identity()`` and ``render.identity_text()`` must render
    the same bytes while no language directive is configured.

    Why assert it rather than merge the two functions: they are NOT the same prompt.
    ``identity_text`` carries a ``_language_directive()`` slot the builder lacks, so
    merging them would add that directive to the builder path the moment a language
    is set - a prompt change on whichever arm uses it, dressed up as a refactor.
    Sharing the identical block and pinning the equality is the honest version: the
    day they legitimately diverge, this test says so instead of a batch saying it.
    """
    import pathlib as _p

    from raven.agent.context.builder import ContextBuilder

    ws = _p.Path("/tmp/ws")
    assert (ContextBuilder(workspace=ws, start_watcher=False)._get_identity()
            == render.identity_text(ws))


def test_the_dr_prompt_replaces_the_instructions_it_drops() -> None:
    """DR mode drops the identity segment, so its replacement must carry the
    parts that were load-bearing and none of the parts that named absent tools.

    Two states since dr@3.4-askuser. The intent of this test is "the prompt must
    not name a tool this arm does not have", and ``ask_user`` became a tool an arm
    CAN have. Asserting only the module constants would keep passing literally
    while measuring nothing: the clause and the identity rewrite both happen at
    build time, so the constants stay clean either way. The on state therefore
    asserts the pairing directly - the rendered segment names the tool AND the
    assembly admits it to the allowlist.
    """
    import asyncio

    from raven.agent.flow.dr import DRModeSegmentBuilder, build_dr_flow
    from raven.config.raven import DRFlowConfig

    dr_text = dr._DR_IDENTITY + "\n" + dr._DR_PROMPT_SECTION

    # Load-bearing, must survive the swap.
    assert "[BEGIN UNTRUSTED" in dr_text, "untrusted-content boundary rule was lost"
    assert "never write a result you have not" in dr_text.lower()

    # Named a tool no DR arm has; must not come back.
    for absent in ("ask_user", "## Workspace", "Platform Policy", "'message' tool", "user_memory"):
        assert absent not in dr_text, f"DR prompt names {absent!r}, which no DR arm provides"

    # Off state: the rendered segment, not just the constants.
    off = asyncio.run(DRModeSegmentBuilder(ask_user=False).build(None))
    assert "ask_user" not in off.text
    assert "nobody to consult" in off.text

    # On state: named in the prompt and present in the tool surface, together.
    on = asyncio.run(DRModeSegmentBuilder(ask_user=True).build(None))
    assert "`ask_user`" in on.text
    assert "nobody to consult" not in on.text
    asm = build_dr_flow(
        DRFlowConfig(
            enabled=True,
            ask_user={"enabled": True},
            conversation={"enabled": True},
        ),
        None, 20, 200_000,
    )
    assert "ask_user" in asm.tools_allowlist
    assert asm.ask_user_tool is not None and asm.ask_user_tool.name == "ask_user"

    assert "identity" in dr._MINIMAL_CONTEXT_DROPPED_SEGMENTS


def test_salvage_prompt_does_not_invite_the_hedge_it_scores_zero_on() -> None:
    """11 of 25 committed salvages hedged and 0 of those 11 were judged correct,
    against 9 of 14 unhedged. The prompt used to ask for the hedge by name."""
    system = finalize._SALVAGE_SYSTEM.lower()
    assert "clearly labelled as unconfirmed" not in system
    assert "do not label the answer unconfirmed" in system
    assert "never emit a list of searches" in system


def test_reviewer_is_told_elided_evidence_is_not_absent_evidence() -> None:
    rubric = verify._ELISION_RUBRIC.lower()
    assert "elided to fit the context window" in rubric
    assert "not that the claim is unsupported" in rubric
    assert "unverifiable_elided" in verify._ELISION_RUBRIC


def test_the_anchor_wording_of_the_loop_nudge_is_frozen() -> None:
    """The flow-off nudge is deliberately NOT improved: the anchor has to stay
    byte-comparable with earlier batches, whose deltas are already published."""
    product = loop_main._loop_break_nudge("web_search", 3, dr_mode=False)
    assert "change approach: a different tool" in product
    assert "re-examine the EXACT path" in product
    dr_text = loop_main._loop_break_nudge("web_search", 3, dr_mode=True)
    assert dr_text != product
    for absent in ("file or path", "offline from local data", "change approach"):
        assert absent not in dr_text, f"DR nudge still names {absent!r}, unreachable in a DR arm"


def test_the_dr_prompt_bytes_match_the_batch_that_measured_them() -> None:
    """The DR segment sha is the acceptance artifact for every A/B-class prompt change,
    because the system message is not stored in the trajectory.

    This pin exists because a refactor that only gave the guidance block an ablation
    switch also moved it to the end of the identity. The segment stayed 3,485 chars, so
    a length check confirmed it; the bytes had changed, and the label dr@2.0 would have
    described a prompt that never produced its headline. Pinned to the sha stamped in
    that batch's arm_env.json.
    """
    import asyncio
    import hashlib

    from raven.agent.flow.dr import DRModeSegmentBuilder

    def sha(s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

    # The measured batch ran without the answer-marker clause. That prompt has to
    # stay reachable and byte-identical forever, or no later run can be compared
    # against the readings it produced. dr@2.6 turned the clause on by default, so
    # this state is now the explicit one - and pinning only the default would have
    # quietly retired the artifact that makes the published numbers reproducible.
    # Every optional clause is named explicitly, never left to a default. dr@2.8 added
    # a second one defaulting on, and this line - which pinned only the marker - began
    # describing a prompt no batch has ever run. The rule the failure teaches: an
    # artifact that reproduces a published reading must state the whole switch state,
    # because "the rest are off" is a fact about today's defaults, not about the batch.
    # dr@3.4-askuser added a third; ``ask_user=False`` is written out here for the
    # same reason, even though its default is off today.
    measured = asyncio.run(
        DRModeSegmentBuilder(
            require_answer_marker=False, report_structure=False, ask_user=False
        ).build(None)
    )
    assert sha(measured.text) == "593c46c416c3f4cf"
    assert len(measured.text) == 3485

    # The dr@2.6 product surface: the same bytes plus one appended clause.
    marker_only = asyncio.run(
        DRModeSegmentBuilder(
            require_answer_marker=True, report_structure=False, ask_user=False
        ).build(None)
    )
    assert sha(marker_only.text) == "7ad4b42cc78aec62"
    assert marker_only.text.startswith(measured.text.rstrip())

    # The product surface: both clauses, still purely appended. This pin moves
    # with every report-clause rewrite: the fold replaced dr@2.8's ordered-prose
    # value (41d7d4d2b582bd55, the dr@2.8-3.4 live-web batches) with the fixed
    # template (878aa5a6e11d81a1), and the same label added the format-override rule.
    # Superseded readings reproduce from their own arm_env.json stamps, not from
    # this build.
    seg = asyncio.run(DRModeSegmentBuilder(ask_user=False).build(None))
    assert sha(seg.text) == "c5335e1d1b33870d"
    assert seg.text.startswith(marker_only.text.rstrip())

    # The pre-override surface stays reachable: the override passage rides its own
    # switch, and the off state must reproduce the pre-override bytes -
    # this pin is what makes pre-override runs comparable against a same-batch arm
    # cut from this build rather than only against their own _src_snapshot.
    no_override = asyncio.run(
        DRModeSegmentBuilder(report_format_override=False, ask_user=False).build(None)
    )
    assert sha(no_override.text) == "878aa5a6e11d81a1"
    assert no_override.text.startswith(marker_only.text.rstrip())

    # Guidance ablation, held at its measured length by pinning the other knob too:
    # a length that moved because an unrelated default flipped would read as the
    # ablation having changed size.
    off = asyncio.run(
        DRModeSegmentBuilder(
            measured_guidance=False, require_answer_marker=False,
            report_structure=False, ask_user=False,
        ).build(None)
    )
    assert len(off.text) == 2622
    assert off.meta["dr_measured_guidance"] is False
