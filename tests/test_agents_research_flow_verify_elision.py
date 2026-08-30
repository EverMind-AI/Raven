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

from research_flow.gates.verify import _CONSTRAINT_RUBRIC, _ELISION_RUBRIC, DraftReviewerGate  # noqa: E402

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
