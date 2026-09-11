"""Empty / thinking-only response recovery.

A turn that ends with no visible text is usually a weak-model dud, not a real
"done". The loop recovers the turn (bounded per turn) before falling back to the
canned reply, and the synthetic scaffolding never reaches persisted history.

Decision logic lives in ``raven.agent.loop.recovery`` as a pure function and
is unit-tested in isolation; the loop tests cover the side effects (re-feeding
reasoning, injecting nudges, stripping synthetic messages).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.agent.loop.recovery import (
    OUTPUT_LIMIT_NUDGE,
    POST_TOOL_NUDGE,
    RecoveryAction,
    RecoveryLimits,
    classify_empty_response,
    has_inline_thinking,
    has_thinking,
    limits_from_defaults,
    lower_reasoning_effort,
)
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.providers.litellm_provider import LiteLLMProvider
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _make_agent(workspace: Path, provider: LLMProvider, limits: RecoveryLimits | None = None) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=10, empty_recovery=limits),
        tools=ToolWiring(restrict_to_workspace=True),
    )


def _classify(
    response,
    visible,
    *,
    prev_had_tool_calls=False,
    nudges_done=0,
    prefill_retries=0,
    empty_retries=0,
    limits=None,
    prefill_supported=True,
):
    return classify_empty_response(
        response,
        visible,
        prev_had_tool_calls=prev_had_tool_calls,
        nudges_done=nudges_done,
        prefill_retries=prefill_retries,
        empty_retries=empty_retries,
        limits=limits or RecoveryLimits(),
        prefill_supported=prefill_supported,
    )


# --------------------------------------------------------------------------- #
# unit: thinking detection                                                     #
# --------------------------------------------------------------------------- #


def test_has_inline_thinking():
    assert has_inline_thinking("<think>...</think>") is True
    assert has_inline_thinking("<THINKING>x") is True
    assert has_inline_thinking("plain text") is False
    assert has_inline_thinking(None) is False
    assert has_inline_thinking("") is False


def test_has_thinking():
    assert has_thinking(LLMResponse(content=None, reasoning_content="hmm")) is True
    assert has_thinking(LLMResponse(content=None, thinking_blocks=[{"x": 1}])) is True
    assert has_thinking(LLMResponse(content="<think>...</think>")) is True
    assert has_thinking(LLMResponse(content="real answer")) is False
    assert has_thinking(LLMResponse(content=None)) is False


# --------------------------------------------------------------------------- #
# unit: classify_empty_response                                                #
# --------------------------------------------------------------------------- #


def test_classify_visible_text_completes():
    assert _classify(LLMResponse(content="answer"), "answer") is RecoveryAction.COMPLETE


def test_classify_disabled_completes():
    """Recovery switched off is not an exhausted ladder.

    No budget was spent, so there is nothing to have run out of, and the turn
    ends the way it always has. FAIL is reserved for a ladder that was climbed.
    """
    limits = RecoveryLimits(enabled=False)
    assert _classify(LLMResponse(content=None), "", limits=limits) is RecoveryAction.COMPLETE


def test_classify_thinking_only_prefills_until_budget():
    resp = LLMResponse(content=None, reasoning_content="hmm")
    assert _classify(resp, "", prefill_retries=0) is RecoveryAction.PREFILL
    assert _classify(resp, "", prefill_retries=1) is RecoveryAction.PREFILL
    # budget spent → falls through to plain retry (prefill_exhausted clause)
    assert _classify(resp, "", prefill_retries=2) is RecoveryAction.RETRY


def test_classify_post_tool_empty_nudges():
    resp = LLMResponse(content=None)  # no thinking
    assert _classify(resp, "", prev_had_tool_calls=True, nudges_done=0) is RecoveryAction.NUDGE
    # nudge budget (default 1) spent → plain retry
    assert _classify(resp, "", prev_had_tool_calls=True, nudges_done=1) is RecoveryAction.RETRY


def test_classify_thinking_takes_priority_over_nudge():
    # thinking + post-tool → PREFILL, not NUDGE (they are mutually exclusive)
    resp = LLMResponse(content=None, reasoning_content="hmm")
    assert _classify(resp, "", prev_had_tool_calls=True) is RecoveryAction.PREFILL


def test_classify_plain_empty_retries_until_budget():
    resp = LLMResponse(content=None)
    assert _classify(resp, "", empty_retries=2) is RecoveryAction.RETRY
    # Budget spent with nothing ever returned: FAIL, not COMPLETE. A turn that
    # never produced a token must not be reported as one that finished.
    assert _classify(resp, "", empty_retries=3) is RecoveryAction.FAIL


def test_classify_always_reasoning_model_still_retries_after_prefill():
    # Models that always populate a reasoning field must not be permanently
    # blocked from plain retry once prefill is exhausted (load-bearing clause).
    resp = LLMResponse(content=None, reasoning_content="hmm")
    assert _classify(resp, "", prefill_retries=2, empty_retries=0) is RecoveryAction.RETRY
    assert _classify(resp, "", prefill_retries=2, empty_retries=3) is RecoveryAction.FAIL


def test_limits_from_defaults_maps_fields():
    class _D:
        empty_recovery_enabled = False
        post_tool_empty_max_nudges = 5
        thinking_prefill_max_retries = 6
        empty_content_max_retries = 7

    limits = limits_from_defaults(_D())
    assert limits == RecoveryLimits(
        enabled=False,
        post_tool_empty_max_nudges=5,
        thinking_prefill_max_retries=6,
        empty_content_max_retries=7,
    )


def test_limits_from_defaults_uses_defaults_for_missing_attrs():
    assert limits_from_defaults(object()) == RecoveryLimits()


# --------------------------------------------------------------------------- #
# loop: plain empty -> retry -> recover                                        #
# --------------------------------------------------------------------------- #


class _EmptyThenAnswerProvider(LLMProvider):
    def __init__(self, empties: int = 2):
        super().__init__(api_key="test")
        self._empties = empties
        self.calls = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        if self.calls <= self._empties:
            return LLMResponse(content="", finish_reason="stop")
        return LLMResponse(content="real answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_empty_then_recovers_and_does_not_persist_scaffolding(workspace):
    provider = _EmptyThenAnswerProvider(empties=2)
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "real answer"  # recovered, not the canned dud reply
    assert provider.calls == 3  # 2 empty retries + the recovered call
    # synthetic scaffolding must not be persisted into session history
    session = agent.sessions.get_or_create("s1")
    for m in session.messages:
        assert not m.get("_recovery_synthetic")


# --------------------------------------------------------------------------- #
# loop: thinking-only -> prefill -> recover                                    #
# --------------------------------------------------------------------------- #


class _ThinkingThenAnswerProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0
        self.saw_reasoning_replay = False

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
        # the prefill re-feeds the prior reasoning as a synthetic assistant turn
        if any(m.get("_recovery_synthetic") for m in messages):
            self.saw_reasoning_replay = True
        return LLMResponse(content="final answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_thinking_only_recovers_via_prefill(workspace):
    provider = _ThinkingThenAnswerProvider()
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "final answer"
    assert provider.saw_reasoning_replay is True
    session = agent.sessions.get_or_create("s1")
    for m in session.messages:
        assert not m.get("_recovery_synthetic")


# --------------------------------------------------------------------------- #
# loop: reasoning cut at the ceiling -> prefill -> the continuation's head      #
# --------------------------------------------------------------------------- #


def test_a_mid_sentence_head_is_cut_and_an_answer_opening_is_kept():
    from raven.agent.loop.recovery import cut_reasoning_head

    junk = " Lägg in an token. Let me start researching.\n\n## Answer\nfinal"
    assert cut_reasoning_head(junk) == "Let me start researching.\n\n## Answer\nfinal"
    assert cut_reasoning_head("so the answer is\n## Answer\nfinal") == "## Answer\nfinal"
    # Openings that read as content are left whole: a heading, a list, a sentence.
    for whole in ("## Answer\nfinal", "- first\n- second", "Yes. It does.", "1. step one\n2. step two"):
        assert cut_reasoning_head(whole) == whole
    # A fragment with nothing after it, or one longer than a fragment could be, stays.
    assert cut_reasoning_head(" trailing thought.") == " trailing thought."
    assert cut_reasoning_head(" " + "x" * 300 + ". rest") == " " + "x" * 300 + ". rest"
    assert cut_reasoning_head("") == "" and cut_reasoning_head(None) is None
    # CJK has no case, so a continuation opening directly on it is left whole; the
    # CJK stops end a fragment the gate did admit (leading whitespace, or a lowercase
    # Latin word running into CJK text) instead of letting it run to a Latin period.
    han_answer = "\u597d\u7684\u3002\u6211\u5f00\u59cb\u7814\u7a76\u3002"
    assert cut_reasoning_head(han_answer) == han_answer
    assert cut_reasoning_head(" " + han_answer) == han_answer[3:]
    assert cut_reasoning_head("so \u7b54\u6848\u662f\u3002\n## \u7b54\u6848") == "## \u7b54\u6848"


@pytest.mark.asyncio
async def test_the_gate_streams_what_the_stored_content_keeps():
    """The reader must see exactly what the session keeps: deltas are held until
    the head rule can be decided, then released with the fragment removed."""
    from raven.agent.loop.recovery import ContinuationGate, cut_reasoning_head

    async def run(deltas: list[str]) -> str:
        seen: list[str] = []

        async def deliver(text: str) -> None:
            seen.append(text)

        gate = ContinuationGate(deliver)
        for d in deltas:
            await gate(d)
        await gate.finish()
        return "".join(seen)

    deltas = [" Lägg in", " an token.", " Let me", " start.\n\n## Answer\nfinal"]
    assert await run(deltas) == cut_reasoning_head("".join(deltas)) == "Let me start.\n\n## Answer\nfinal"
    # Nothing is released before the rule can be decided.
    seen: list[str] = []

    async def deliver(text: str) -> None:
        seen.append(text)

    gate = ContinuationGate(deliver)
    await gate(" Lägg in")
    await gate(" an token.")
    assert seen == [], "a boundary with nothing after it is not yet decidable"
    await gate(" Let")
    assert seen == ["Let"]
    await gate(" me")
    assert seen == ["Let", " me"]
    # A continuation that never reaches a boundary is delivered whole at the end.
    assert await run([" a fragment with no end"]) == " a fragment with no end"
    assert await run(["## Answer\n", "final"]) == "## Answer\nfinal"


class _CutThinkingThenAnswerProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="", reasoning_content="let me think about the tok", finish_reason="length")
        return LLMResponse(
            content=" Lägg in an token. Let me start researching.\n\n## Answer\nfinal", finish_reason="stop"
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_continuation_after_cut_reasoning_loses_its_mid_thought_head(workspace):
    """2026-09-08: a 131072-token reasoning run hit the ceiling, the prefill fed
    it back, and the continuation's first clause reached the reader as the head
    of the answer."""
    provider = _CutThinkingThenAnswerProvider()
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "Let me start researching.\n\n## Answer\nfinal"
    session = agent.sessions.get_or_create("s1")
    stored = [m for m in session.messages if m.get("role") == "assistant"]
    assert stored and stored[-1]["content"] == "Let me start researching.\n\n## Answer\nfinal"


@pytest.mark.asyncio
async def test_a_continuation_after_complete_reasoning_is_left_whole(workspace):
    """Reasoning that ended on its own (``stop``) was not cut, so the continuation
    opens where the model meant it to; the head rule is for the ceiling case."""

    class _Whole(_CutThinkingThenAnswerProvider):
        async def chat(
            self,
            messages,
            tools=None,
            model=None,
            max_tokens=4096,
            temperature=0.7,
            reasoning_effort=None,
            tool_choice=None,
        ):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
            return LLMResponse(content=" lowercase start. Then the rest.", finish_reason="stop")

    agent = _make_agent(workspace, _Whole())
    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )
    assert out is not None and out[0] == "lowercase start. Then the rest."


class _AlwaysCutThinkingProvider(LLMProvider):
    """Every call spends the whole output budget inside the reasoning block.

    The shape a reasoning model takes at its own ceiling: no visible text, no
    tool call, ``finish_reason="length"``.
    """

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        return LLMResponse(
            content="",
            reasoning_content="deciding how to lay out the file",
            finish_reason="length",
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_an_empty_turn_cut_at_the_output_limit_is_told_so(workspace):
    """The ladder's retries carried no reason, so a model that had spent its
    whole budget thinking was asked again under the conditions that had just
    failed -- measured on one run as six generations and no tool call.

    ``write_file``'s ``truncation_hint`` covers the other shape, a call present
    and cut mid-arguments. A turn cut before any call has no tool result to
    carry advice on, so the ladder has to say it itself.
    """
    provider = _AlwaysCutThinkingProvider()
    agent = _make_agent(workspace, provider)

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="write eight files",
        ),
        session_key="s1",
    )

    assert provider.calls > 1, "the ladder did not ask again at all"
    assert any(any(m.get("content") == OUTPUT_LIMIT_NUDGE for m in seen) for seen in provider.seen), (
        "no request in the ladder told the model it had been cut at the output limit"
    )
    # scaffolding, so it must not reach persisted history either
    session = agent.sessions.get_or_create("s1")
    for m in session.messages:
        assert not m.get("_recovery_synthetic")
        assert m.get("content") != OUTPUT_LIMIT_NUDGE


@pytest.mark.asyncio
async def test_the_output_limit_nudge_does_not_presume_a_tool_call_was_owed(workspace):
    """The nudge fires on ``finish_reason`` "length" alone, and that carries no
    evidence about what the cut turn owed -- a plain question whose reasoning ate
    the budget reaches it too. Wording that tells such a turn to emit a call
    points it at a tool the task never needed, which is a worse outcome than the
    silence it replaces.
    """
    provider = _AlwaysCutThinkingProvider()
    agent = _make_agent(workspace, provider)

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="which year did the timezone database begin",
        ),
        session_key="s1",
    )

    delivered = [m for seen in provider.seen for m in seen if m.get("content") == OUTPUT_LIMIT_NUDGE]
    assert delivered, "an answer-only turn never reached the nudge"
    text = str(delivered[0]["content"])
    assert "answer" in text, "the nudge offers the answer-only turn no valid recovery"
    assert "tool call" in text, "the nudge no longer names the call recovery"


@pytest.mark.asyncio
async def test_an_empty_turn_that_was_not_cut_gets_no_output_limit_nudge(workspace):
    """The trigger is the ceiling, not emptiness. An empty turn that stopped
    honestly was not cut, so every sentence the nudge opens with would be false
    about it -- and a message that says the wrong thing is worse than the silence
    it replaces. The fix's other tests all enter through a cut turn, so without
    this one the condition itself is unheld.
    """

    class _Recording(_AlwaysEmptyProvider):
        def __init__(self):
            super().__init__()
            self.seen: list[list[dict]] = []

        async def chat(
            self,
            messages,
            tools=None,
            model=None,
            max_tokens=4096,
            temperature=0.7,
            reasoning_effort=None,
            tool_choice=None,
        ):
            self.seen.append([dict(m) for m in messages])
            return await super().chat(
                messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
            )

    provider = _Recording()
    agent = _make_agent(workspace, provider)

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert provider.calls > 1, "the ladder never asked again, so this guard proves nothing"
    assert not any(m.get("content") == OUTPUT_LIMIT_NUDGE for seen in provider.seen for m in seen), (
        "an empty turn that was never cut was told it had hit the output limit"
    )


class _ToolThenAlwaysCutProvider(LLMProvider):
    """One tool call, then every later call ends empty on the ceiling.

    Built so the retry is reached with a ``tool`` message last: the post-tool
    nudge is switched off in the test's limits, so a post-tool empty turn falls
    straight to RETRY and nothing has been appended after the tool result.
    """

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="no_such_tool", arguments={})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="", finish_reason="length")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_the_output_limit_nudge_never_follows_a_tool_message(workspace):
    """A bare tool->user pair is a 400 on most APIs, which is why a synthetic
    assistant sits between them. That failure is at the provider boundary, so
    what a unit test can hold is the shape rather than the API.

    The scenario is the point: reached through a turn with no tool call, the
    message before the nudge is never a tool result, so the assertion passes
    whether or not the placeholder is written. Driven this way -- tool result
    last, post-tool nudge switched off so RETRY is reached directly -- removing
    the placeholder puts a `tool` immediately before the nudge and this fails.
    """
    provider = _ToolThenAlwaysCutProvider()
    agent = _make_agent(workspace, provider, limits=RecoveryLimits(post_tool_empty_max_nudges=0))

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="use the tool",
        ),
        session_key="s1",
    )

    checked = 0
    for seen in provider.seen:
        for i, m in enumerate(seen):
            if m.get("content") == OUTPUT_LIMIT_NUDGE:
                assert i > 0, "the nudge opened the request with no turn before it"
                assert seen[i - 1].get("role") != "tool", (
                    f"a bare tool->user pair reached the provider: {[x.get('role') for x in seen[max(0, i - 3) : i + 1]]}"
                )
                checked += 1
    assert checked, "the nudge was never delivered, so the shape was never checked"


@pytest.mark.asyncio
async def test_the_output_limit_nudge_is_said_once_not_once_per_retry(workspace):
    """Three retries share one explanation. Appending it again per retry fills
    the window the advice is asking the model to spend less of, and the
    escalation for advice that did not land is ``loop_break_nudge``.
    """
    provider = _AlwaysCutThinkingProvider()
    agent = _make_agent(workspace, provider)

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="write eight files",
        ),
        session_key="s1",
    )

    per_request = [sum(1 for m in seen if m.get("content") == OUTPUT_LIMIT_NUDGE) for seen in provider.seen]
    assert max(per_request) == 1, f"nudge accumulated across retries: {per_request}"


# --------------------------------------------------------------------------- #
# loop: persistently empty is bounded then falls back                          #
# --------------------------------------------------------------------------- #


class _AlwaysEmptyProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0
        self.efforts: list[str | None] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.efforts.append(reasoning_effort)
        return LLMResponse(content="", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_persistently_empty_is_bounded_then_reports_a_failed_turn(workspace):
    limits = RecoveryLimits()
    provider = _AlwaysEmptyProvider()
    agent = _make_agent(workspace, provider, limits=limits)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None
    # plain-empty budget -> 1 initial call + a retry per rung the effort descent
    # still has, then give up. From an unstated effort that is low and minimal,
    # so the budget's third retry is never sent: it could only repeat the second.
    assert provider.calls <= 1 + limits.empty_content_max_retries
    assert provider.calls == 3
    # Not the canned "no response to give" filler, which reads as an answer and
    # let a measured DAG node file a dead turn as a finished one. The reply names
    # the failure and how many attempts went into it -- three, the calls actually
    # sent, not the budget's full allowance.
    reply = out[0]
    assert "no answer" in reply.lower()
    assert "failed call" in reply.lower()
    assert f"{provider.calls} attempt" in reply


@pytest.mark.asyncio
async def test_an_exhausted_recovery_ends_the_turn_with_error_status(workspace):
    """The status the caller reads, which used to say ``completed``.

    ``LoopOutcome.status`` is the loop's own word on whether the turn produced
    anything; the neighbouring provider-error exit sets ``"error"`` and this one
    used to leave the initialised ``"completed"`` in place.
    """
    provider = _AlwaysEmptyProvider()
    agent = _make_agent(workspace, provider, limits=RecoveryLimits())

    _final, _used, messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "hi"}])

    assert outcome.status == "error"
    # Like the provider-error exit beside it, the failure text is not persisted
    # into history: an error reply in the transcript poisons the next request.
    assert not any(m.get("role") == "assistant" for m in messages)


def test_the_effort_ladder_descends_one_rung_and_stops(monkeypatch):
    """The descent, on its own. ``None`` in is a turn that named no effort and
    let the backend pick, so the first rung down is the lowest effort this repo
    documents in config -- a stated request, which an unstated one is not."""
    assert lower_reasoning_effort(None) == "low"
    assert lower_reasoning_effort("high") == "medium"
    assert lower_reasoning_effort("low") == "minimal"
    assert lower_reasoning_effort("minimal") is None, "the floor, so a later retry cannot invent a rung"
    assert lower_reasoning_effort("HIGH ") == "medium", "an effort is a config string, not a keyword"
    assert lower_reasoning_effort("enthusiastic") is None, "a value we cannot place has no rung below it"


def test_a_rung_the_wire_cannot_tell_apart_is_skipped_not_sent():
    """A retry only pays for itself if the request it re-sends differs.

    Reviewer, 2026-09-10: on the Anthropic Messages wire ``_ADAPTIVE_EFFORTS``
    maps both ``minimal`` and ``low`` onto ``low``, so descending by label alone
    produced ``output_config.effort: low`` twice for an adaptive model and
    ``reasoning.effort: low`` twice otherwise -- the second retry was the first
    failure again at full price, which is the defect the ladder exists to
    remove. Asked of the provider, the collapsed rung is skipped, and when every
    rung below collapses onto this one there is nothing left to change.
    """
    from raven.agent.loop.turn_path import _reasoning_wire_keys
    from raven.providers.anthropic_messages_provider import AnthropicMessagesProvider

    for model in ("claude-opus-4-5", "claude-opus-5"):
        provider = AnthropicMessagesProvider(api_key="k", default_model=model)
        wire = _reasoning_wire_keys(provider, model)
        assert wire is not None
        assert wire("low") == wire("minimal"), f"{model}: the two rungs really are one request"
        assert lower_reasoning_effort("low", wire) is None, "so there is no rung below low here"
        assert lower_reasoning_effort("medium", wire) == "low"
        assert lower_reasoning_effort(None, wire) == "low"
        # And the descent from the top still gets a distinct request each step.
        efforts, effort = [], "max"
        while effort is not None:
            efforts.append(effort)
            effort = lower_reasoning_effort(effort, wire)
        assert efforts == ["max", "xhigh", "high", "medium", "low"]
        assert len({wire(e) for e in efforts}) == len(efforts), "every rung sent is its own request"


def test_a_wire_that_sends_the_label_verbatim_keeps_every_rung():
    """The chat and responses wires send the effort as the caller named it, so
    the labels stand on their own and nothing is skipped."""
    from raven.providers.base import LLMProvider

    wire = LLMProvider.reasoning_wire_keys
    shape = lambda effort: repr(wire(None, "m", effort))  # noqa: E731 - one expression, read once

    assert shape("low") != shape("minimal")
    assert lower_reasoning_effort("low", shape) == "minimal"
    assert lower_reasoning_effort("minimal", shape) is None


def test_a_provider_that_cannot_answer_leaves_the_descent_on_the_labels():
    """A test double or an older adapter: no method, no shape, previous behaviour."""
    from raven.agent.loop.turn_path import _reasoning_wire_keys

    assert _reasoning_wire_keys(SimpleNamespace(), "m") is None
    assert lower_reasoning_effort("low", None) == "minimal"

    def raises(_effort):
        raise RuntimeError("no")

    broken = _reasoning_wire_keys(SimpleNamespace(reasoning_wire_keys=lambda *a: raises(a)), "m")
    assert broken is not None
    assert lower_reasoning_effort("low", broken) == "minimal", "an unanswerable question is not a same request"


@pytest.mark.asyncio
async def test_the_plain_empty_retry_asks_for_less_reasoning_than_the_call_that_failed(workspace):
    """A retry that resends the same bytes is not a retry.

    Measured: three ``plain empty retry`` steps, each behind a call truncated at
    exactly the output ceiling, each re-sent unchanged -- so three identical
    failures were guaranteed before the first one was sent. An empty body with
    reasoning behind it is a call whose thinking spent the ceiling before the
    answer began, so the request that has to change is the one saying how much
    of the ceiling thinking may take.

    The descent is sticky across the turn's retries: read afresh each time it
    would hand back the value that just came up empty.
    """
    limits = RecoveryLimits()
    provider = _AlwaysEmptyProvider()
    agent = _make_agent(workspace, provider, limits=limits)

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert provider.efforts == [None, "low", "minimal"]
    assert provider.calls == 3, "the fourth call had no rung left to change, so it was not paid for"
    retried = provider.efforts[1:]
    assert all(effort != provider.efforts[index] for index, effort in enumerate(retried)), (
        "every retry differs from the call it is retrying"
    )


@pytest.mark.asyncio
async def test_recovery_disabled_falls_back_immediately(workspace):
    provider = _AlwaysEmptyProvider()
    agent = _make_agent(workspace, provider, limits=RecoveryLimits(enabled=False))

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None
    assert provider.calls == 1  # no retries when disabled
    assert "no response" in out[0].lower()


# ---------------------------------------------------------------------------
# Tag debris counts as empty, so recovery still runs
# ---------------------------------------------------------------------------


def test_lone_closing_think_tag_reads_as_empty() -> None:
    """A cut-off inline reasoning block leaves an opener-less closing tag.

    The paired-block substitution finds nothing to remove, so an
    eleven-character string reaches the recovery check looking like a real
    answer -- recovery is skipped and the tag is what the user sees.
    """
    for debris in ("</mm:think>", "</think>", "</thinking>", "</mm:think></mm:think>"):
        assert AgentLoop._strip_think(debris) is None, debris


def test_paired_namespaced_block_reads_as_empty() -> None:
    """A complete but empty namespaced block is debris too."""
    assert AgentLoop._strip_think("<mm:think></mm:think>") is None


def test_text_mentioning_a_tag_is_left_alone() -> None:
    """Debris detection must not eat an answer that talks about tags.

    The check asks whether anything survives removing the tags, and does not
    rewrite the text it returns.
    """
    said = "The model emits </think> at the end of its reasoning."
    assert AgentLoop._strip_think(said) == said


def test_real_content_after_a_think_block_survives() -> None:
    """The existing paired-block behaviour is unchanged."""
    assert AgentLoop._strip_think("<think>reasoning</think>the answer") == "the answer"


# ---------------------------------------------------------------------------
# Inline-thinking detection: namespaced and orphan spellings
# ---------------------------------------------------------------------------


def test_inline_thinking_detects_namespaced_and_orphan_tags() -> None:
    """The scan decides whether a turn produced reasoning at all.

    An opener-only pattern misses both shapes a truncated turn actually
    produces: a vendor-namespaced tag, and a closing tag whose opener the
    backend swallowed. Missing them classifies a reasoning-only turn as a real
    answer, which is exactly the recovery this module exists to trigger.
    """
    from raven.agent.loop.recovery import has_inline_thinking

    assert has_inline_thinking("<mm:think>weighing options</mm:think>")
    assert has_inline_thinking("weighing options</think>")
    assert has_inline_thinking("<think>weighing options</think>")


def test_inline_thinking_ignores_prose_about_thinking() -> None:
    """No tag, no detection -- the word alone must not trigger recovery."""
    from raven.agent.loop.recovery import has_inline_thinking

    assert not has_inline_thinking("I was thinking about the reasoning behind it")
    assert not has_inline_thinking("")
    assert not has_inline_thinking(None)


def test_paired_namespaced_block_is_stripped_not_shown() -> None:
    """A complete block must not reach the user because of its prefix.

    Debris detection and the orphan split both match tags by shape, so a
    literal-only pattern here was the one place a vendor spelling still got
    through -- and it got through in the worst form: a whole reasoning block
    rendered as if it were the answer.
    """
    assert AgentLoop._strip_think("<mm:think>weighing options</mm:think>the answer") == "the answer"
    assert AgentLoop._strip_think("<thinking>weighing</thinking>the answer") == "the answer"


def test_a_mismatched_pair_is_not_treated_as_a_block() -> None:
    """Deleting between two unrelated tags would take real content with it."""
    text = "<think>weighing</thinking>the answer"

    assert AgentLoop._strip_think(text) == text


def test_one_end_namespaced_is_still_one_block() -> None:
    """A backend that stamps only one end still wrote a single block."""
    assert AgentLoop._strip_think("<mm:think>weighing</think>the answer") == "the answer"


# --------------------------------------------------------------------------- #
# the transport verdict must not pre-empt this recovery                        #
# --------------------------------------------------------------------------- #
#
# The loop breaks the turn on `finish_reason == "error"` *before*
# `classify_empty_response` runs, so anything that reports an empty response as
# an error takes every mode in this file out of service. The tests above stub
# `chat` directly and so never reach the provider's response exit, which is
# where such a verdict is reached -- these go through it on purpose.


class _SilentAfterToolProvider(LiteLLMProvider):
    """Calls a tool, comes back with nothing, and answers once nudged.

    The shape `recovery.py` documents as the common weak-model dud: honest
    usage, no text. Built through the real `_parse_response` so a verdict at
    that exit is in the path.
    """

    def __init__(self) -> None:
        with (
            patch("raven.providers.litellm_provider.litellm"),
            patch.object(LiteLLMProvider, "_setup_env"),
        ):
            super().__init__(api_key="sk-test", provider_name="openrouter")
        self.calls = 0
        self.nudged = False

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
                usage={"prompt_tokens": 900, "total_tokens": 910},
            )
        if any(m.get("content") == POST_TOOL_NUDGE for m in messages):
            self.nudged = True
            return LLMResponse(content="here are the files", finish_reason="stop")
        # Empty, and billed truthfully: a model that said nothing, not a
        # request that never arrived.
        return self._parse_response(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=None, tool_calls=None, reasoning_content=None),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=2900, completion_tokens=1, total_tokens=2901),
            ),
            sent_chars=12000,
        )


@pytest.mark.asyncio
async def test_a_silent_model_after_a_tool_still_reaches_the_nudge(workspace):
    """Honest usage means the loop keeps ownership of the silence.

    Reported as an error instead, this turn would end on the spot -- and the
    error would claim a transport failure, which for this response is simply
    untrue.
    """
    provider = _SilentAfterToolProvider()
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="list the files",
        ),
        session_key="s1",
    )

    assert out is not None
    assert provider.nudged is True, "the nudge never happened; the empty turn was taken as an error"
    assert out[0] == "here are the files"


# --------------------------------------------------------------------------- #
# unit: providers that reject a trailing assistant message (Anthropic family)  #
# --------------------------------------------------------------------------- #


def test_thinking_only_is_retried_instead_of_prefilled_when_prefill_is_refused():
    """Anthropic rejects an assistant prefill while thinking is on.

    Live incident (2026-09-01, a dispatched raven-code run, claude via
    OpenRouter): the thinking-only prefill produced a request the vendor
    considers invalid; routed through a gateway it neither errored nor
    answered, so the turn hung until the wall-clock cap. With prefill refused
    the recovery must pick an action that leaves the transcript ending on a
    user or tool message -- here, before any tool ran, the plain re-request.
    """
    r = LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
    assert _classify(r, "", prefill_supported=False) is RecoveryAction.RETRY


def test_thinking_only_after_a_tool_is_nudged_when_prefill_is_refused():
    """After a tool the nudge is the cheaper repair and it too ends on a user
    message: the synthetic assistant it inserts sits before the nudge."""
    r = LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
    assert _classify(r, "", prefill_supported=False, prev_had_tool_calls=True) is RecoveryAction.NUDGE


def test_prefill_refused_retries_once_the_nudge_budget_is_spent():
    limits = RecoveryLimits()
    r = LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
    action = _classify(
        r, "", prefill_supported=False, prev_had_tool_calls=True, nudges_done=limits.post_tool_empty_max_nudges
    )
    assert action is RecoveryAction.RETRY


def test_prefill_refused_fails_once_every_budget_is_spent():
    limits = RecoveryLimits()
    r = LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
    action = _classify(
        r,
        "",
        prefill_supported=False,
        prev_had_tool_calls=True,
        nudges_done=limits.post_tool_empty_max_nudges,
        empty_retries=limits.empty_content_max_retries,
    )
    assert action is RecoveryAction.FAIL


def test_prefill_refused_never_consumes_the_prefill_budget():
    """The refusal is decided before the prefill budget is consulted, so a
    fresh budget changes nothing: PREFILL is never the answer."""
    r = LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
    for retries in range(RecoveryLimits().thinking_prefill_max_retries + 1):
        assert _classify(r, "", prefill_supported=False, prefill_retries=retries) is not RecoveryAction.PREFILL


def test_prefill_refused_never_reaches_a_non_thinking_response():
    """The guard is scoped to the prefill path; a plain empty turn still retries."""
    r = LLMResponse(content="", finish_reason="stop")
    assert _classify(r, "", prefill_supported=False) is RecoveryAction.RETRY


def test_prefill_supported_is_the_default_so_other_providers_are_untouched():
    r = LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
    assert _classify(r, "") is RecoveryAction.PREFILL
    assert _classify(r, "", prefill_supported=True) is RecoveryAction.PREFILL


# --------------------------------------------------------------------------- #
# unit: which providers accept an assistant prefill                            #
# --------------------------------------------------------------------------- #


def test_base_provider_accepts_an_assistant_prefill_by_default():
    provider = _AlwaysEmptyProvider()
    assert provider.supports_assistant_prefill("some/model") is True
    assert provider.supports_assistant_prefill(None) is True


def test_litellm_provider_refuses_an_assistant_prefill_for_anthropic_models():
    """Same judgement that decides whether thinking_blocks go on the wire.

    The illegal request is exactly "trailing assistant message + thinking
    blocks", so the two must be decided by one rule or they drift apart.
    """
    provider = LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")
    assert provider.supports_assistant_prefill("anthropic/claude-opus-5") is False
    assert provider.supports_assistant_prefill("claude-opus-4-5") is False
    assert provider.supports_assistant_prefill("openrouter/anthropic/claude-opus-5") is False


def test_litellm_provider_accepts_an_assistant_prefill_for_other_models():
    provider = LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")
    assert provider.supports_assistant_prefill("openai/gpt-4o") is True
    assert provider.supports_assistant_prefill("deepseek/deepseek-chat") is True
    assert provider.supports_assistant_prefill(None) is True


def test_litellm_provider_answers_for_its_default_model_when_none_is_named():
    provider = LiteLLMProvider(api_key="test-key", default_model="anthropic/claude-opus-5")
    assert provider.supports_assistant_prefill() is False


def test_delegating_providers_answer_for_the_provider_they_wrap():
    """LazyProvider / PerModelProvider are the shapes the factory actually
    builds; answering from the base default would leave the guard dead in
    production."""
    from raven.providers.base import GenerationSettings
    from raven.providers.lazy import LazyProvider
    from raven.providers.per_model_provider import PerModelProvider

    inner = LiteLLMProvider(api_key="test-key", default_model="anthropic/claude-opus-5")
    lazy = LazyProvider(factory=lambda: inner, default_model="anthropic/claude-opus-5", generation=GenerationSettings())
    assert lazy.supports_assistant_prefill("anthropic/claude-opus-5") is False
    assert lazy.supports_assistant_prefill("openai/gpt-4o") is True

    routed = PerModelProvider(models=[], fallback=inner)
    assert routed.supports_assistant_prefill("anthropic/claude-opus-5") is False
    assert routed.supports_assistant_prefill("openai/gpt-4o") is True


# --------------------------------------------------------------------------- #
# loop: no request may end on an assistant message when prefill is refused     #
# --------------------------------------------------------------------------- #


class _PrefillRefusingThinkingProvider(LLMProvider):
    """Thinking-only once, then an answer; refuses assistant prefills."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0
        self.last_roles: list[str] = []

    def supports_assistant_prefill(self, model: str | None = None) -> bool:
        return False

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.last_roles.append(str(messages[-1].get("role")))
        if self.calls == 1:
            return LLMResponse(content="", reasoning_content="let me think", finish_reason="stop")
        return LLMResponse(content="real answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "anthropic/claude-opus-5"


@pytest.mark.asyncio
async def test_no_request_ends_on_an_assistant_message_when_prefill_is_refused(workspace):
    """Protocol invariant, not just a classification: whatever the recovery
    picks, the transcript handed to a prefill-refusing provider must never end
    with an assistant turn, and the turn must still recover its answer."""
    provider = _PrefillRefusingThinkingProvider()
    agent = _make_agent(workspace, provider)

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "real answer"
    assert provider.calls == 2
    assert "assistant" not in provider.last_roles, f"a prefill went to the provider: {provider.last_roles}"


class _AlwaysCutProvider(LLMProvider):
    """Every call ends on the output ceiling having produced nothing at all."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        return LLMResponse(content="", finish_reason="length")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_turn_cut_at_the_ceiling_records_it_on_the_session(workspace):
    """Recorded on the session, not on a message: this turn persists no
    assistant row at all, so a message-seated record has nowhere to land."""
    agent = _make_agent(workspace, _AlwaysCutProvider(), limits=RecoveryLimits())

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    session = agent.sessions.get_or_create("s1")
    assert session.metadata.get("output_limit_turn_at") == 0


@pytest.mark.asyncio
async def test_an_answerless_turn_that_was_not_cut_records_no_output_limit(workspace):
    agent = _make_agent(workspace, _AlwaysEmptyProvider(), limits=RecoveryLimits())

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert "output_limit_turn_at" not in agent.sessions.get_or_create("s1").metadata


class _CutThenAnswersProvider(LLMProvider):
    """Cut on the first call, whole on the second -- a turn that recovered."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="", finish_reason="length")
        return LLMResponse(content="the whole answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_turn_that_recovered_from_a_cut_records_no_output_limit(workspace):
    """The fact says the run's own last word was cut, so a turn that went on to
    deliver must not carry it -- a reader told otherwise would weigh a
    completed answer as a truncated one."""
    agent = _make_agent(workspace, _CutThenAnswersProvider(), limits=RecoveryLimits())

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert out is not None and out[0] == "the whole answer"
    assert "output_limit_turn_at" not in agent.sessions.get_or_create("s1").metadata


@pytest.mark.asyncio
async def test_a_turn_that_was_not_cut_clears_a_marker_left_by_an_earlier_one(workspace):
    """The slot has to be turn-scoped by being written OR cleared every turn.

    Scoping it by the message index alone assumes that index only grows, and
    `Session.clear()` (what `/new` calls) and `undo_last_turn` both move it
    back without touching metadata -- so an old marker can sit at an index a
    later turn's own start satisfies, and that turn gets reported as cut.
    """
    agent = _make_agent(workspace, _AlwaysEmptyProvider(), limits=RecoveryLimits())
    agent.sessions.get_or_create("s1").metadata["output_limit_turn_at"] = 0

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
    )

    assert "output_limit_turn_at" not in agent.sessions.get_or_create("s1").metadata
