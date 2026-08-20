"""ForcedFinalizeGate: answerless terminal turns are nudged, then salvaged.

The gate must judge the *visible* answer (the serving stack prefills the
opening think tag, so content arrives as ``reasoning</think>answer``),
prefer a training-clean commit nudge, salvage through an independent
context only after the nudge budget is spent, and fail open when the
salvage call dies.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven.agent.context import TOOL_OUTPUT_ELIDED
from raven.agent.flow import ForcedFinalizeGate, closing_tag_bar, visible_answer
from raven.agent.hook import AgentHookContext
from raven.providers.base import LLMResponse


class _SalvageProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.kwargs = []

    async def chat_with_retry(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, LLMResponse):
            return response
        return LLMResponse(content=response, finish_reason="stop")


def _ctx(content, messages=None):
    ctx = AgentHookContext(session_key="cli:test")
    ctx.iteration = 30
    ctx.messages = messages or [
        {"role": "user", "content": "who founded X?"},
        {"role": "tool", "name": "web_fetch", "content": "evidence: X was founded by Ada in 1992"},
    ]
    ctx.response = SimpleNamespace(has_tool_calls=False, content=content)
    return ctx


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<think>hm</think>The answer is X.", "The answer is X."),
        ("reasoning without opener</think>The answer is X.", "The answer is X."),
        ("reasoning that never closes and just goes on</think>", ""),
        ("<think>generation cut mid-think", ""),
        ("plain answer, no think at all", "plain answer, no think at all"),
        (None, ""),
    ],
)
def test_visible_answer_shapes(text, expected):
    assert visible_answer(text) == expected


@pytest.mark.asyncio
async def test_normal_answer_passes_through():
    provider = _SalvageProvider([])
    gate = ForcedFinalizeGate(provider)

    decision = await gate.after_iteration(_ctx("I checked both sources.</think>Ada founded X in 1992."))

    assert not decision.rollback and decision.short_circuit_result is None
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_tool_call_iterations_ignored():
    gate = ForcedFinalizeGate(_SalvageProvider([]))
    ctx = _ctx("planning next search</think>")
    ctx.response.has_tool_calls = True

    decision = await gate.after_iteration(ctx)

    assert not decision.rollback and decision.short_circuit_result is None


@pytest.mark.asyncio
async def test_all_think_terminal_gets_commit_nudge():
    gate = ForcedFinalizeGate(_SalvageProvider([]))
    ctx = _ctx("Let me search for the founder next.</think>  ")

    decision = await gate.after_iteration(ctx)

    assert decision.rollback is True
    assert decision.rollback_inject[0]["role"] == "user"
    assert decision.rollback_inject[0]["content"].startswith("[finalize]")
    assert ctx.metadata["force_finalize"] == {
        "empty_hits": 1,
        "nudges": 1,
        "synth_failed": 0,
        "last_reason": "empty_visible_answer",
    }


@pytest.mark.asyncio
async def test_second_empty_terminal_salvages_via_short_circuit():
    provider = _SalvageProvider(["salvage reasoning</think>Ada founded X in 1992, per the fetched page."])
    gate = ForcedFinalizeGate(provider)
    ctx = _ctx("Still no answer, I should look further.</think>")

    first = await gate.after_iteration(ctx)
    assert first.rollback is True

    ctx.response = SimpleNamespace(has_tool_calls=False, content="More reasoning, still no commit.</think> ")
    second = await gate.after_iteration(ctx)

    assert second.short_circuit_result == "Ada founded X in 1992, per the fetched page."
    assert ctx.metadata["force_finalize"]["synthesized"] is True
    salvage_user = provider.kwargs[0]["messages"][1]["content"]
    assert "Task:\nwho founded X?" in salvage_user
    assert "founded by Ada in 1992" in salvage_user
    assert "Researcher notes" in salvage_user


@pytest.mark.asyncio
async def test_salvage_failure_fails_open():
    provider = _SalvageProvider([RuntimeError("endpoint down")])
    gate = ForcedFinalizeGate(provider, max_nudges=0)
    ctx = _ctx("reasoning only</think>")

    decision = await gate.after_iteration(ctx)

    assert not decision.rollback and decision.short_circuit_result is None
    assert ctx.metadata["force_finalize"]["synth_failed"] == 1


@pytest.mark.asyncio
async def test_salvage_output_is_think_folded():
    provider = _SalvageProvider(
        [LLMResponse(content="only reasoning, salvage also failed to commit</think>  ", finish_reason="stop")]
    )
    gate = ForcedFinalizeGate(provider, max_nudges=0)

    decision = await gate.after_iteration(_ctx("reasoning only</think>"))

    assert decision.short_circuit_result is None
    assert decision.notes == ["force_finalize_failopen"]


@pytest.mark.asyncio
async def test_salvage_cut_mid_think_is_rejected_under_the_strict_caliber():
    """A tagless salvage reply is a chain of thought, not an answer.

    The served template prefills the opening tag, so a salvage call that never
    emits the closing one carries no tag at all -- and committing it persists
    reasoning as the final answer, the exact shape the closing-tag rule rejects
    one seam earlier."""
    provider = _SalvageProvider(
        [LLMResponse(content="The user wants to identify a brand. Candidates: A? B?", finish_reason="stop")]
    )
    gate = ForcedFinalizeGate(provider, max_nudges=0, closing_tag_required=True)

    decision = await gate.after_iteration(_ctx("reasoning only</think>"))

    assert decision.short_circuit_result is None
    assert decision.notes == ["force_finalize_failopen"]
    assert provider.kwargs, "the salvage call must still have been attempted"


@pytest.mark.asyncio
async def test_tagless_salvage_still_commits_when_the_stack_emits_no_think_tags():
    provider = _SalvageProvider([LLMResponse(content="Ada founded X in 1992.", finish_reason="stop")])
    gate = ForcedFinalizeGate(provider, max_nudges=0, closing_tag_required=False)

    decision = await gate.after_iteration(_ctx("reasoning only</think>"))

    assert decision.short_circuit_result == "Ada founded X in 1992."


@pytest.mark.asyncio
async def test_committed_salvage_is_counted_on_the_trajectory():
    """A committed salvage carries no closing tag once the reasoning is folded
    away, so a consumer applying the closing-tag rule cannot tell it from a turn
    that never reached its answer. Only this counter separates them."""
    provider = _SalvageProvider(["salvage reasoning</think>Ada founded X in 1992."])
    gate = ForcedFinalizeGate(provider, max_nudges=0, closing_tag_required=True)
    ctx = _ctx("reasoning only</think>")

    decision = await gate.after_iteration(ctx)

    assert decision.short_circuit_result == "Ada founded X in 1992."
    state = ctx.metadata["force_finalize"]
    assert state["salvage_committed"] == 1
    assert state["salvage_seam"] == "iteration"
    assert state["salvage_answer_chars"] == len("Ada founded X in 1992.")


@pytest.mark.asyncio
async def test_spin_answer_triggers_gate():
    gate = ForcedFinalizeGate(_SalvageProvider([]))
    spin = "The founder might be Ada, or possibly not Ada, let me reconsider the founder again. " * 30

    decision = await gate.after_iteration(_ctx(f"done</think>{spin}"))

    assert decision.rollback is True
    assert ctx_reason(decision) == "spin_answer"


def ctx_reason(decision):
    return decision.notes[0].split()[-1]


@pytest.mark.asyncio
async def test_terminal_seam_salvages_without_a_nudge():
    """The turn is already over at this seam, so there is no re-sample to nudge
    into — salvage runs immediately even with nudge budget left."""
    provider = _SalvageProvider(["Ada founded X in 1992. Source: https://example.org/x"])
    gate = ForcedFinalizeGate(provider, max_nudges=1)
    ctx = _ctx("reasoning that never committed</think>")
    ctx.response = None

    decision = await gate.terminal_answerless(ctx)

    assert decision.short_circuit_result.startswith("Ada founded X in 1992")
    assert decision.notes == ["force_finalize_terminal_salvage"]
    state = ctx.metadata["force_finalize"]
    assert state["terminal_hits"] == 1 and state["nudges"] == 0
    assert state["synthesized"] is True


@pytest.mark.asyncio
async def test_terminal_seam_reads_notes_from_the_last_assistant_turn():
    """``ctx.response`` is None here (the response that ended the turn may never
    have been persisted), so the reasoning excerpt comes from history."""
    provider = _SalvageProvider(["Ada."])
    gate = ForcedFinalizeGate(provider)
    ctx = _ctx("unused")
    ctx.response = None
    ctx.messages = [
        {"role": "user", "content": "who founded X?"},
        {"role": "assistant", "content": "candidate Ada keeps recurring</think>"},
        {"role": "tool", "name": "web_fetch", "content": "evidence: founded by Ada"},
    ]

    await gate.terminal_answerless(ctx)

    assert "candidate Ada keeps recurring" in provider.kwargs[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_terminal_seam_fails_open():
    provider = _SalvageProvider([RuntimeError("endpoint down")])
    gate = ForcedFinalizeGate(provider)
    ctx = _ctx("reasoning only</think>")
    ctx.response = None

    decision = await gate.terminal_answerless(ctx)

    assert decision.short_circuit_result is None
    assert decision.notes == ["force_finalize_terminal_failopen"]
    assert ctx.metadata["force_finalize"]["synth_failed"] == 1


@pytest.mark.asyncio
async def test_salvage_does_not_restart_a_stall_it_cannot_afford():
    """A restart discards a generation that was only slow, and the fresh call is
    no faster — measured, every stalled salvage burned its whole budget in
    identical stalls. One attempt per full budget window, then fail open."""
    import asyncio

    class _StallProvider:
        def __init__(self):
            self.calls = 0

        async def chat_with_retry(self, **kwargs):
            self.calls += 1
            await asyncio.sleep(5)
            return LLMResponse(content="too late", finish_reason="stop")

    provider = _StallProvider()
    gate = ForcedFinalizeGate(provider, max_nudges=0, timeout_seconds=0.2, attempt_timeout_seconds=0.1)
    ctx = _ctx("reasoning only</think>")

    decision = await gate.after_iteration(ctx)

    assert decision.short_circuit_result is None
    assert provider.calls == 1  # not three doomed restarts
    assert ctx.metadata["force_finalize"]["synth_fail_reason"] == "stalled"


@pytest.mark.asyncio
async def test_salvage_records_why_it_produced_nothing():
    provider = _SalvageProvider([LLMResponse(content="still thinking</think>  ", finish_reason="stop")])
    gate = ForcedFinalizeGate(provider, max_nudges=0)
    ctx = _ctx("reasoning only</think>")

    await gate.after_iteration(ctx)

    assert ctx.metadata["force_finalize"]["synth_fail_reason"] == "no_visible_answer"


@pytest.mark.asyncio
async def test_salvage_evidence_pack_skips_elided_bodies():
    """This gate fires on exactly the overflowing turns where older tool bodies
    have already been replaced by the elision placeholder."""
    provider = _SalvageProvider(["Paris."])
    gate = ForcedFinalizeGate(provider, max_nudges=0, evidence_items=2)
    messages = [
        {"role": "user", "content": "what is the capital?"},
        {"role": "tool", "name": "web_fetch", "content": "the capital is Paris"},
        {"role": "tool", "name": "web_fetch", "content": TOOL_OUTPUT_ELIDED},
        {"role": "tool", "name": "web_search", "content": "later result"},
    ]

    pack = gate._evidence_pack(messages)

    assert "the capital is Paris" in pack
    assert "later result" in pack
    assert TOOL_OUTPUT_ELIDED not in pack


@pytest.mark.asyncio
async def test_salvage_that_echoes_the_harness_refusal_is_rejected():
    """dr@3.2. The harness must not be able to answer its own question.

    Reproduces ``hle-256`` from the dr@3.0 live-web batch: the turn overflowed,
    39 of 43 tool bodies were elided, the four survivors were all the
    search-closed notice, and the salvage model returned it verbatim. It scored
    zero AND counted as ``closed_with_answer`` on the answer-rate endpoint --
    a false positive on a co-primary endpoint, not merely a lost point.
    """
    from raven.agent.harness_text import search_closed_notice

    notice = search_closed_notice(10)
    provider = _SalvageProvider([LLMResponse(content=notice, finish_reason="stop")])
    gate = ForcedFinalizeGate(provider, max_nudges=0, closing_tag_required=False)

    decision = await gate.after_iteration(_ctx("reasoning only</think>"))

    assert decision.short_circuit_result is None, "the harness sentence must not ship"
    assert decision.notes == ["force_finalize_failopen"]


@pytest.mark.asyncio
async def test_salvage_that_merely_quotes_the_refusal_still_commits():
    """The opposite direction, and the one that matters more.

    An answer ABOUT the refusal is a real answer. Blanking it would recreate the
    failure this gate exists to prevent -- MiroFlow's boxed extraction dropped
    10.83% of its own correct answers that way, dr@1.6's salvage seam blanked 15.
    Shaping may improve an answer; it may never empty one.
    """
    from raven.agent.harness_text import SEARCH_CLOSED_PREFIX

    answer = f"{SEARCH_CLOSED_PREFIX} so I answered from the pages already open: Ada, 1992."
    provider = _SalvageProvider([LLMResponse(content=answer, finish_reason="stop")])
    gate = ForcedFinalizeGate(provider, max_nudges=0, closing_tag_required=False)

    decision = await gate.after_iteration(_ctx("reasoning only</think>"))

    assert decision.short_circuit_result is not None
    assert "Ada, 1992" in decision.short_circuit_result


@pytest.mark.parametrize(
    ("configured", "reasoning", "expected"),
    [
        (True, None, True),
        (True, "", True),
        (True, "chain of thought, delivered out-of-band", False),
        (False, None, False),
        (False, "chain of thought", False),
    ],
)
def test_closing_tag_bar_waives_only_for_oob_reasoning(configured, reasoning, expected):
    assert closing_tag_bar(configured, reasoning) == expected


@pytest.mark.asyncio
async def test_tagless_answer_with_oob_reasoning_passes_under_the_strict_caliber():
    """A stack that returns reasoning on its own channel (``reasoning_content``)
    leaves only answer text in ``content``, so the closing-tag bar's premise is
    void for that response -- holding it anyway erased every complete answer
    (4/4 turns on the OpenRouter live-web config) and burned a commit nudge each
    time."""
    provider = _SalvageProvider([])
    gate = ForcedFinalizeGate(provider, closing_tag_required=True)
    ctx = _ctx("Ada founded X in 1992.")
    ctx.response.reasoning_content = "chain of thought, delivered out-of-band"

    decision = await gate.after_iteration(ctx)

    assert not decision.rollback and decision.short_circuit_result is None
    assert provider.calls == 0
    assert "force_finalize" not in ctx.metadata


@pytest.mark.asyncio
async def test_tagless_salvage_with_oob_reasoning_commits_under_the_strict_caliber():
    provider = _SalvageProvider(
        [
            LLMResponse(
                content="Ada founded X in 1992.",
                reasoning_content="salvage thinking, delivered out-of-band",
                finish_reason="stop",
            )
        ]
    )
    gate = ForcedFinalizeGate(provider, max_nudges=0, closing_tag_required=True)

    decision = await gate.after_iteration(_ctx("reasoning only</think>"))

    assert decision.short_circuit_result == "Ada founded X in 1992."
