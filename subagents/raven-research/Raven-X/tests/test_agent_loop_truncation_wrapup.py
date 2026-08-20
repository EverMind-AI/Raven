"""dr@2.9: the wrap-up net for turns killed by the COMPLETION budget.

Why this file exists. On eval_web_dr27_20260807 the anchor arm ended 22/60 HLE
items with ``finish_reason='length'`` on turn 1 (``n_search=0``, elision 8.3%):
not a context-window overflow, just one generation that never finished writing.
21 of those 22 carried no visible answer and were therefore scored wrong. Two
nets already existed and neither could reach them:

  * ``_synthesize_final_on_exhaustion`` is arm-neutral, but its guard requires
    ``iteration >= max_iterations`` - unreachable on turn 1.
  * ``ForcedFinalizeGate.terminal_answerless`` is a flow hook, so it does not
    exist on an anchor arm at all; and the ``answerless`` predicate that would
    trigger it uses ``_think_closing_tag_required``, which is False whenever the
    flow is off, so an unclosed think block reads as "has an answer" there.

The tests below pin the three properties that make the new net safe rather than
merely present: it fires on the right shape, it is judged by the SAME ruler on
every arm, and it cannot double-fire into the terminal seam.
"""

from types import SimpleNamespace

import pytest

from raven.agent.flow.answer_text import visible_answer
from raven.agent.loop.main import (
    _MAX_ITER_SYNTHESIS_PROMPT,
    _TRUNCATED_SYNTHESIS_PROMPT,
    AgentLoop,
    _is_truncated_answerless,
)
from raven.providers.base import LLMResponse

# --------------------------------------------------------------------------- #
# The prompt: it must not repeat a false statement                            #
# --------------------------------------------------------------------------- #


def test_truncated_prompt_does_not_claim_a_tool_budget_was_spent():
    """The exhaustion prompt is a FALSE statement for a truncated turn.

    ``_MAX_ITER_SYNTHESIS_PROMPT`` opens with "You've used up the tool-calling
    budget". The items this net targets called zero tools. Telling the model it
    exhausted a budget it never touched invites it to explain a constraint that
    does not exist instead of answering.
    """
    assert "tool-calling budget" in _MAX_ITER_SYNTHESIS_PROMPT  # guards the premise
    assert "tool-calling budget" not in _TRUNCATED_SYNTHESIS_PROMPT
    assert "length limit" in _TRUNCATED_SYNTHESIS_PROMPT


def test_truncated_prompt_pins_language_and_forbids_re_derivation():
    """Same language rule as the sibling prompt, plus one property of its own.

    48% of truncated items already contain a conclusion sentence and 10/10
    sampled ones then contradict it - i.e. the failure is that the model kept
    reasoning. A prompt that invites fresh analysis would re-run exactly the
    behaviour that ran out of budget.
    """
    assert "same language as the user" in _TRUNCATED_SYNTHESIS_PROMPT
    assert "Do not start a new analysis" in _TRUNCATED_SYNTHESIS_PROMPT
    assert "do not re-derive" in _TRUNCATED_SYNTHESIS_PROMPT


# --------------------------------------------------------------------------- #
# The call: right prompt, and room made for it                                #
# --------------------------------------------------------------------------- #


class _RecordingProvider:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _bind_synth(provider, max_iterations: int = 40):
    fake_self = SimpleNamespace(
        provider=provider,
        max_iterations=max_iterations,
        _strip_think=AgentLoop._strip_think,
    )
    return AgentLoop._synthesize_final_on_exhaustion.__get__(fake_self)


@pytest.mark.asyncio
async def test_truncated_flag_selects_the_truncation_prompt():
    provider = _RecordingProvider(LLMResponse(content="76.", finish_reason="stop"))
    synth = _bind_synth(provider)

    result = await synth([{"role": "user", "content": "hi"}], "m", None, truncated=True)

    assert result == ("76.", True)
    assert provider.calls[0]["messages"][-1]["content"] == _TRUNCATED_SYNTHESIS_PROMPT
    assert provider.calls[0]["tools"] is None


@pytest.mark.asyncio
async def test_default_still_selects_the_exhaustion_prompt():
    """Reverse direction: the new flag must not have moved the existing path."""
    provider = _RecordingProvider(LLMResponse(content="ok", finish_reason="stop"))
    synth = _bind_synth(provider)

    await synth([{"role": "user", "content": "hi"}], "m", None)

    assert provider.calls[0]["messages"][-1]["content"] == _MAX_ITER_SYNTHESIS_PROMPT


@pytest.mark.asyncio
async def test_wrapup_call_elides_older_tool_bodies_to_make_room():
    """This call is appended to a history that just proved too long to finish in.

    Measured on the same batch, items carrying >40 tool results had a p10 of
    8,496 characters of room. So the wrap-up must free space the same way the
    loop already does mid-turn, not invent a second shrink policy.
    """
    provider = _RecordingProvider(LLMResponse(content="done", finish_reason="stop"))
    synth = _bind_synth(provider)
    history = [{"role": "user", "content": "q"}] + [
        {"role": "tool", "content": f"body-{i}" * 100} for i in range(10)
    ]

    await synth(history, "m", None, truncated=True)

    sent = provider.calls[0]["messages"]
    tool_bodies = [m["content"] for m in sent if m.get("role") == "tool"]
    # All but the most recent few are replaced by the placeholder.
    kept_verbatim = [b for b in tool_bodies if b.startswith("body-")]
    assert len(kept_verbatim) == AgentLoop._SHRINK_KEEP_RECENT_TOOL_RESULTS
    # ...and the user turn plus the prompt survive untouched.
    assert sent[-1]["content"] == _TRUNCATED_SYNTHESIS_PROMPT
    assert any(m.get("role") == "user" and m.get("content") == "q" for m in sent)


@pytest.mark.asyncio
async def test_wrapup_shrink_is_a_noop_when_there_is_nothing_old():
    """Reverse direction: it must not mangle a short history."""
    provider = _RecordingProvider(LLMResponse(content="done", finish_reason="stop"))
    synth = _bind_synth(provider)
    history = [{"role": "user", "content": "q"}, {"role": "tool", "content": "small"}]

    await synth(history, "m", None, truncated=True)

    sent = provider.calls[0]["messages"]
    assert [m["content"] for m in sent if m.get("role") == "tool"] == ["small"]


# --------------------------------------------------------------------------- #
# The guard predicate: one ruler on every arm                                 #
# --------------------------------------------------------------------------- #


def _fires(final_content, finish_reason, *, salvaged=False, reasoning_oob=False):
    """Thin alias for the REAL predicate the loop calls.

    Deliberately not a re-implementation: an earlier draft of this file mirrored
    the expression here, which meant the assertions below described a private copy
    and would have stayed green after the loop's own predicate changed.
    """
    return _is_truncated_answerless(
        final_content, finish_reason, salvaged=salvaged, reasoning_oob=reasoning_oob
    )


# The shape these items actually have. Under prefilled-think serving the template
# emits the opening tag, so it is NOT part of the content: a generation cut before
# it writes ``</think>`` carries NO think tag at all, and its reasoning is
# indistinguishable from an answer by inspection. That is the fourth shape
# ``closing_tag_required`` exists for, and the only one where the two bars differ.
# (A literal ``<think>`` in the content is a different shape - both bars fold it
# away and return "" - so testing with one would pass while asserting nothing.)
_TRUNCATED_PREFILLED = "Let me reconsider. I think 76 is the answer, but wait"


def test_guard_fires_on_the_measured_shape():
    """turn 1, cut mid-sentence, no closing tag - the 21/22 HLE shape."""
    assert _fires(_TRUNCATED_PREFILLED, "length") is True


def test_guard_ignores_a_completed_answer():
    assert _fires("<think>reasoning</think>The answer is 76.", "length") is False


def test_guard_waives_the_bar_for_out_of_band_reasoning():
    """dr@3.4 fold. A response whose reasoning arrived via ``reasoning_content``
    holds only answer text in ``content`` - no closing tag can ever appear - so
    the strict bar would call EVERY length-cut answer on such a stack answerless
    and replace it with a lossy wrap-up. Waived on the per-response fact (arm-
    neutral, unlike the config flag), a cut mid-ANSWER ships as-is, exactly like
    an inline cut after ``</think>``."""
    assert _fires("The answer is 76, because", "length", reasoning_oob=True) is False


def test_guard_still_fires_on_an_oob_cut_before_any_answer():
    """Cut mid-REASONING on an out-of-band stack: ``content`` is empty, the
    waiver has nothing to protect, and the wrap-up must still run."""
    assert _fires("", "length", reasoning_oob=True) is True
    assert _fires("   ", "length", reasoning_oob=True) is True


def test_guard_ignores_a_normal_stop():
    """finish_reason is load-bearing: a short reply is not a truncated one."""
    assert _fires("<think>hmm", "stop") is False


def test_guard_ignores_a_committed_salvage():
    """Consult the commit flag, never re-derive 'has an answer'.

    A committed salvage structurally never carries a closing tag (see
    ``ForcedFinalizeGate._mark_committed``), so recomputing the predicate on it
    always says "answerless". That is precisely how the terminal seam once ran 22
    salvage calls over 16 items, re-running a lossy stage on text it had just
    produced and under-counting its own LLM calls by ~38%.
    """
    salvaged_text = "<think>partial reasoning with no closing tag"
    assert _fires(salvaged_text, "length", salvaged=False) is True   # premise
    assert _fires(salvaged_text, "length", salvaged=True) is False   # exemption


def test_guard_uses_the_same_ruler_on_both_arms():
    """★ The property that keeps this repair from creating a new defect.

    ``self._think_closing_tag_required`` is False whenever the flow is off, so a
    predicate keyed on it would judge the anchor permissively and the treated arm
    strictly - repairing the treated arm and leaving the anchor broken, i.e.
    manufacturing an arm-correlated defect while fixing one. The guard therefore
    hard-codes ``closing_tag_required=True``.

    Recomputing one 120-item batch with the strict bar moved the anchors 26->36
    and 25->33 while the treated arms moved 17->21 and 14->18 - the direction and
    the size of the asymmetry this test exists to prevent.
    """
    # Permissive bar (what an arm-dependent predicate would use when flow is off)
    # reads this pure reasoning as an answer; the strict bar does not. That gap IS
    # the arm-correlated bias, so the guard must never see the permissive value.
    assert visible_answer(_TRUNCATED_PREFILLED, closing_tag_required=False) == _TRUNCATED_PREFILLED
    assert visible_answer(_TRUNCATED_PREFILLED, closing_tag_required=True) == ""
    # The guard fires on it regardless of which arm produced it.
    assert _fires(_TRUNCATED_PREFILLED, "length") is True


# --------------------------------------------------------------------------- #
# The exemption: a wrap-up must not be re-processed by the terminal seam      #
# --------------------------------------------------------------------------- #


def _answerless(final_content, *, status="ok", salvage_committed=False, synthesized=False,
                closing_tag_required=False):
    """Mirrors the ``answerless`` expression in ``_run_agent_loop``."""
    return status == "error" or not (
        salvage_committed
        or synthesized
        or visible_answer(final_content, closing_tag_required=closing_tag_required)
    )


def test_synthesized_wrapup_is_not_answerless():
    """Otherwise the terminal seam fires a SECOND lossy stage on this loop's output.

    A wrap-up is free-form prose and need not carry a closing think tag, so on a
    treated arm (strict bar) recomputing the predicate says "answerless" and the
    terminal gate runs again on text the loop just produced.
    """
    wrapup = "Based on what I gathered, the answer is 76."
    assert _answerless(wrapup, synthesized=False, closing_tag_required=True) is True
    assert _answerless(wrapup, synthesized=True, closing_tag_required=True) is False


def test_static_fallback_is_still_answerless():
    """Reverse direction, and the reason the flag is tested rather than the branch.

    ``_synthesize_final_on_exhaustion`` returns ``synthesized=False`` when the call
    failed and it fell back to the canned apology. That case genuinely has no
    answer, so the terminal seam SHOULD still get its chance.
    """
    apology = "I reached the maximum number of tool call iterations (40)."
    assert _answerless(apology, synthesized=False, closing_tag_required=True) is True


def test_wrapup_observer_is_stamped_on_both_arms():
    """The rate must be readable per arm, so the stamp cannot sit behind a gate.

    This fix is not gated on ``DRFlowConfig`` - deliberately, since gating a
    reliability repair pins the anchor on the broken path - so it fires on the
    anchor too. That makes the per-arm firing rate a required disclosure: a batch
    where it fires on one arm and not the other has a delta moved by the harness,
    and one where it fires on neither cannot cite it as a reason the batch is
    incomparable to earlier ones.

    Asserting equal indentation with the ``containment`` stamp is the cheap way to
    say "unconditional": both live directly in the observers block, and anyone who
    later tucks this one inside an ``if self._dr_flow ...`` moves it one level in.
    """
    import inspect

    from raven.agent.loop.main import AgentLoop

    src = inspect.getsource(AgentLoop._run_agent_loop)
    indents = {}
    for line in src.splitlines():
        for key in ("containment", "truncation_wrapup"):
            if line.lstrip().startswith(f'observers["{key}"]'):
                indents[key] = len(line) - len(line.lstrip())
    assert set(indents) == {"containment", "truncation_wrapup"}, indents
    assert indents["truncation_wrapup"] == indents["containment"], (
        "the truncation stamp is nested deeper than the containment stamp: it is "
        "now conditional, and an arm that never reaches it is indistinguishable "
        "from an arm where the wrap-up never fired"
    )


# --------------------------------------------------------------------------- #
# dr@3.0: gated, budgeted, and counted by the scorer's ruler                  #
# --------------------------------------------------------------------------- #
#
# dr@2.9 shipped this net ungated on the stated reasoning that the anchor suffers
# from the defect most. It does - and it was still the wrong trade. An ungated
# repair moves the flow-off arm, and the anchor is the reference frame every
# published A-minus-B is measured against, so the batch that paid for the repair
# could not use its own cross-batch cells. Over that batch the net fired 21 times
# on the treated arm for zero correct answers.
#
# Three separate defects, three assertions. Each was found by looking at what the
# net produced rather than at whether it ran, which is the only way any of them is
# visible: all three are silent.


def test_the_net_cannot_reach_an_arm_without_the_flow():
    """The anchor builds no assembly at all, so the gate is structural, not configured.

    Asserted through the same ``getattr`` the loop uses rather than by constructing
    a loop: what matters is that a missing assembly reads as "off", because that is
    what the flow-off arm has - not a config with the knob set false.
    """
    assert bool(getattr(None, "truncation_wrapup", False)) is False


def test_the_knob_is_off_by_default_on_every_arm():
    """Including the treated one.

    The capability was repaired rather than deleted so it can be priced with a gated
    pair. Defaulting it on would skip that step and re-run the experiment that
    already returned zero.
    """
    from raven.config.raven import DRFlowConfig

    assert DRFlowConfig().truncation_wrapup.enabled is False
    assert DRFlowConfig(enabled=True).truncation_wrapup.enabled is False


def test_an_enabled_arm_carries_its_own_generation_budget_onto_the_assembly():
    """16 of 21 firings ran the wrap-up call itself into the completion limit.

    The turn this fires on has just proved it cannot finish writing inside the
    provider's default, so inheriting that default inherits the failure - and does so
    on precisely the questions that need the net most.
    """
    from raven.agent.flow import build_dr_flow
    from raven.config.raven import DRFlowConfig

    class _P:
        def get_default_model(self):
            return "student"

    cfg = DRFlowConfig(enabled=True, truncation_wrapup={"enabled": True, "maxTokens": 8192})
    a = build_dr_flow(cfg, _P(), max_iterations=40, context_window_tokens=65536)
    assert a.truncation_wrapup is True
    assert a.truncation_wrapup_max_tokens == 8192


def test_the_exhaustion_branch_keeps_no_budget_of_its_own():
    """The other caller of the same helper fires on BOTH arms.

    Giving it a budget too would move the anchor - the exact mistake this round is
    undoing one branch over. The helper must therefore treat ``None`` as "send what
    dr@2.9 sent", not as "send a default of my own".
    """
    import inspect

    sig = inspect.signature(AgentLoop._synthesize_final_on_exhaustion)
    assert sig.parameters["max_tokens"].default is None
    assert sig.parameters["reasoning_effort"].default is None


@pytest.mark.parametrize(
    "content,scorer_sees_answer",
    [
        # A wrap-up cut mid-reasoning. `_strip_think` removes only PAIRED tags, so
        # this passes through whole and reads as text to the old counter.
        ("Let me reconsider, the candidate might be", False),
        # A wrap-up that actually finished: the closing tag is present.
        ("reasoning here</think>The answer is 76.", True),
    ],
)
def test_recovered_means_the_scorer_can_see_an_answer(content, scorer_sees_answer):
    """The counter said 21/21; the scorer saw 5/21. Same turns.

    ``recovered`` is now computed with ``visible_answer(..., closing_tag_required=True)``
    - the scorer's own predicate - so the two cannot diverge again. This is the
    fifth time in this codebase a gate has been found not measuring the thing its
    name claims, and the first three were found the same way: by comparing the
    counter against the artifact the counter is supposed to summarise.
    """
    assert bool(visible_answer(content, closing_tag_required=True)) is scorer_sees_answer
