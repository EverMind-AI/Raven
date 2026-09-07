"""A rejection whose evidence was elided is not evidence of an unsupported claim.

dr@2.0's first measurement found this rule had shipped as prompt text only:
``verify.rejected_on_elided`` appeared nowhere in the batch, because nothing on the
code side read the elision count that ``_evidence_pack`` already returns. That is
the same inert-change class as dr@1.7, where three observer keys were written into
metadata and dropped by a persistence-layer whitelist, so a version was measured,
scored and reported while behaving like its predecessor.

The measured cost of leaving it to the prompt: the two recoverable items of the
previous batch were both correct drafts - one committing "Dr. Martens" (gold: Dr
Martens), one committing "Last Christmas" (gold: Last Christmas) - failed on every
bullet as unsupported while 87% and 100% of the tool results in their context had
already been replaced by the elision placeholder.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.verify import (  # noqa: E402
    _CONSTRAINT_RUBRIC,
    _ELISION_RUBRIC,
    _NUMERIC_RUBRIC,
    DraftReviewerGate,
)

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402


class _Reviewer:
    """A provider whose reviewer always rejects, naming a claim."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_retry(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            content='{"pass": false, "unresolved_claims": 1, '
            '"unsupported_claims": ["the deciding date"], "issues": ["no source"]}'
        )


def _ctx(*, elided: bool) -> AgentHookContext:
    ctx = AgentHookContext(session_key="cli:test")
    ctx.iteration = 3
    ctx.response = SimpleNamespace(has_tool_calls=False, content="reasoning</think>## Answer: Dr Martens")
    body = "[earlier tool output elided to fit the context window]" if elided else "docid=5 body text"
    ctx.messages = [
        {"role": "user", "content": "task"},
        {"role": "tool", "name": "web_fetch", "content": body},
    ]
    return ctx


@pytest.mark.asyncio
async def test_a_rejection_over_elided_evidence_is_degraded_and_counted():
    gate = DraftReviewerGate(_Reviewer())
    ctx = _ctx(elided=True)

    decision = await gate.after_iteration(ctx)

    state = ctx.metadata["verify_gate"]
    assert state["rejected_on_elided"] == 1
    assert state["evidence_elided_skipped"] >= 1
    # Degraded, not bounced: no revision was requested.
    assert state["revisions"] == 0
    assert state["rejects"] == 0
    assert "evidence elided" in " ".join(decision.notes)


@pytest.mark.asyncio
async def test_a_rejection_with_evidence_in_view_still_rejects():
    """The carve-out must not become a blanket disable of the gate."""
    gate = DraftReviewerGate(_Reviewer())
    ctx = _ctx(elided=False)

    await gate.after_iteration(ctx)

    state = ctx.metadata["verify_gate"]
    assert state.get("rejected_on_elided", 0) == 0
    assert state["rejects"] == 1


@pytest.mark.asyncio
async def test_the_carve_out_can_be_switched_off():
    gate = DraftReviewerGate(_Reviewer(), fail_open_on_elided_evidence=False)
    ctx = _ctx(elided=True)

    await gate.after_iteration(ctx)

    assert ctx.metadata["verify_gate"].get("rejected_on_elided", 0) == 0
    assert ctx.metadata["verify_gate"]["rejects"] == 1


def test_the_elision_carve_out_is_the_last_word_in_the_rubric():
    """Order is the whole defect. _CONSTRAINT_RUBRIC says any failed constraint is
    an unsupported claim; the carve-out says a claim whose evidence is gone is not.
    They collide exactly when a constraint fails because its evidence was elided,
    so the carve-out has to come last and say so explicitly."""
    gate = DraftReviewerGate(_Reviewer(), constraint_rubric=True)
    system = gate._reviewer_system

    assert system.endswith(_ELISION_RUBRIC)
    assert system.index(_CONSTRAINT_RUBRIC) < system.index(_ELISION_RUBRIC)
    assert "overrides every rule above" in _ELISION_RUBRIC


@pytest.mark.asyncio
async def test_a_previous_turns_persisted_placeholder_does_not_degrade_this_turns_reject():
    """dr@3.4. The elision snapshot scans ``messages[ctx.turn_base:]``, never 0.

    Elision placeholders are persisted, so one left on disk by ANY earlier turn
    of the session latched every later turn's reject into a pass for the rest
    of the conversation - the verify gate effectively off, with
    ``fail_open_on_elided_evidence`` defaulting true.
    """
    gate = DraftReviewerGate(_Reviewer())
    placeholder = "[earlier tool output elided to fit the context window]"
    history = [
        {"role": "user", "content": "turn one"},
        {"role": "tool", "name": "web_fetch", "content": placeholder},
        {"role": "assistant", "content": "turn one answer"},
    ]
    ctx = AgentHookContext(session_key="cli:test", turn_base=len(history))
    ctx.iteration = 3
    ctx.response = SimpleNamespace(has_tool_calls=False, content="reasoning</think>## Answer: Dr Martens")
    ctx.messages = history + [
        {"role": "user", "content": "turn two task"},
        {"role": "tool", "name": "web_fetch", "content": "docid=5 body text"},
    ]
    decision = await gate.after_iteration(ctx)
    state = ctx.metadata["verify_gate"]
    assert state["evidence_elided_in_context"] == 0
    assert state.get("rejected_on_elided", 0) == 0
    assert decision.rollback is True, "the reject was silently degraded by prior-turn elision"


@pytest.mark.asyncio
async def test_a_tagless_draft_with_oob_reasoning_is_still_reviewed():
    """Under ``closing_tag_required=True`` a response whose reasoning arrived
    out-of-band (``reasoning_content``) carries no think tag by construction.
    Without the waiver the gate extracted no draft and the reviewer was
    structurally silenced -- 4/4 turns on the OpenRouter live-web config."""
    reviewer = _Reviewer()
    gate = DraftReviewerGate(reviewer, closing_tag_required=True)
    ctx = _ctx(elided=False)
    ctx.response = SimpleNamespace(
        has_tool_calls=False,
        content="## Answer: Dr Martens",
        reasoning_content="chain of thought, delivered out-of-band",
    )

    await gate.after_iteration(ctx)

    assert reviewer.calls == 1
    assert ctx.metadata["verify_gate"]["rejects"] == 1


@pytest.mark.asyncio
async def test_a_tagless_draft_without_oob_reasoning_stays_unreviewed_under_the_strict_caliber():
    """The waiver must not become a blanket disable of the bar: a tagless
    response with no out-of-band reasoning is still indistinguishable from a
    generation cut mid-think, and reviewing it would re-open the dr@3.4 hole
    (a reject injecting raw reasoning into persisted history)."""
    reviewer = _Reviewer()
    gate = DraftReviewerGate(reviewer, closing_tag_required=True)
    ctx = _ctx(elided=False)
    ctx.response = SimpleNamespace(has_tool_calls=False, content="bare reasoning, cut before the tag")

    await gate.after_iteration(ctx)

    assert reviewer.calls == 0
    assert "verify_gate" not in ctx.metadata


def test_the_numeric_rubric_rides_every_reviewer_and_the_carve_out_cannot_reach_it():
    """The rubric that makes a self-declared estimate a claim rather than a gap.

    Unconditional, unlike the constraint and reject-only rubrics: their knobs exist
    because a bench arm runs the same reviewer and a measured distribution may not move
    underneath it, and this module serves one product and no arm.

    Placed before the elision carve-out so the carve-out stays the last word - the
    ordering the test above exists for - and worded so it survives being overridden: it
    reads the draft's own words, and a cell the draft calls an estimate is one whether or
    not the page behind it is still in the window.
    """
    for constraint, strict in ((False, False), (True, False), (False, True), (True, True)):
        gate = DraftReviewerGate(provider=None, constraint_rubric=constraint, strict_reject_only=strict)
        system = gate._reviewer_system
        assert _NUMERIC_RUBRIC in system
        assert system.endswith(_ELISION_RUBRIC)
        assert system.index(_NUMERIC_RUBRIC) < system.index(_ELISION_RUBRIC)

    assert "reads the draft's own words rather than the evidence" in _NUMERIC_RUBRIC
    assert "whether or not the evidence behind the cell is still in view" in _NUMERIC_RUBRIC
    # The estimate is named as the claim, in the field the gate already reads for a
    # reject: a rubric the code cannot act on is inert.
    assert "name that cell in unsupported_claims" in _NUMERIC_RUBRIC


class _NumericReviewer:
    """A reviewer that rejects for a cell the draft itself marks as an estimate."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_retry(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            content='{"pass": false, "unresolved_claims": 1, '
            '"unsupported_claims": ["CrossCodeEval size: 4 langs, ~2k (est.)"], '
            '"estimated_cells": ["CrossCodeEval size: 4 langs, ~2k (est.)"], '
            '"issues": ["the total was summed from an estimate"]}'
        )


@pytest.mark.asyncio
async def test_an_estimate_the_draft_names_survives_the_elision_carve_out():
    """The rubric promises this holds whether or not the cell's evidence is in view.

    Without this the promise was prompt text the code cancelled: a long research turn is
    exactly the one that elides, so the rubric was inert in the runs it was written for.
    The carve-out asks whether a rejection might be the pack failing to show evidence,
    and for a cell the draft itself marks `(est.)` the answer is no - the claim is made
    of the draft's own words.
    """
    gate = DraftReviewerGate(_NumericReviewer())
    ctx = _ctx(elided=True)

    await gate.after_iteration(ctx)

    state = ctx.metadata["verify_gate"]
    assert state["elided_kept_numeric"] == 1
    assert state.get("rejected_on_elided", 0) == 0
    # Bounced, not degraded: the revision is the point.
    assert state["rejects"] == 1
    assert state["revisions"] == 1


@pytest.mark.asyncio
async def test_an_evidence_claim_beside_no_estimate_is_still_degraded():
    """The carve-out keeps covering what it was built for; only the new class escapes it."""
    gate = DraftReviewerGate(_Reviewer())
    ctx = _ctx(elided=True)

    await gate.after_iteration(ctx)

    state = ctx.metadata["verify_gate"]
    assert state["rejected_on_elided"] == 1
    assert state.get("elided_kept_numeric", 0) == 0
    assert state["revisions"] == 0


@pytest.mark.asyncio
async def test_the_revision_is_told_the_cell_and_told_what_kind_of_problem_it_is():
    """A named estimate needs a different instruction from an unsupported claim: retrieve
    the number or write that it was not obtained, rather than find evidence for it."""
    gate = DraftReviewerGate(_NumericReviewer())
    verdict = {
        "unsupported_claims": ["CrossCodeEval size", "the deciding date"],
        "estimated_cells": ["CrossCodeEval size"],
        "issues": [],
    }

    feedback = gate._format_feedback(verdict)

    assert "- estimate in a scored column: CrossCodeEval size" in feedback
    # Named once, under the heading that says what to do about it.
    assert feedback.count("CrossCodeEval size") == 1
    assert "- unsupported claim: the deciding date" in feedback


def test_the_reviewer_is_asked_for_the_field_the_gate_reads():
    """A rubric the code cannot read is inert - the defect this module already carries a
    paragraph about. The field is in the reply shape as well as in the rubric."""
    gate = DraftReviewerGate(_Reviewer())
    system = gate._reviewer_system

    assert '"estimated_cells": ["..."]' in system
    assert "unsupported_claims AND in estimated_cells" in system


def test_a_count_in_the_estimated_cells_field_does_not_crash_the_gate():
    """Same normalisation the other list fields get: a reviewer answering with a number
    would otherwise make the gate raise mid-rejection, and a raised hook is a no-op - the
    draft ships and no counter moves."""
    verdict = DraftReviewerGate._parse_verdict('{"pass": false, "estimated_cells": 3}')

    assert verdict is not None
    assert verdict["estimated_cells"] == []
