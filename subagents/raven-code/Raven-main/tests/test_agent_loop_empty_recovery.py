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

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.recovery import (
    RecoveryAction,
    RecoveryLimits,
    classify_empty_response,
    has_inline_thinking,
    has_thinking,
    limits_from_defaults,
)
from raven.providers.base import LLMProvider, LLMResponse
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
        max_iterations=10,
        restrict_to_workspace=True,
        empty_recovery=limits,
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
    length_nudges=0,
):
    return classify_empty_response(
        response,
        visible,
        prev_had_tool_calls=prev_had_tool_calls,
        nudges_done=nudges_done,
        prefill_retries=prefill_retries,
        empty_retries=empty_retries,
        limits=limits or RecoveryLimits(),
        length_nudges=length_nudges,
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
    assert _classify(resp, "", empty_retries=3) is RecoveryAction.COMPLETE


def test_classify_always_reasoning_model_still_retries_after_prefill():
    # Models that always populate a reasoning field must not be permanently
    # blocked from plain retry once prefill is exhausted (load-bearing clause).
    resp = LLMResponse(content=None, reasoning_content="hmm")
    assert _classify(resp, "", prefill_retries=2, empty_retries=0) is RecoveryAction.RETRY
    assert _classify(resp, "", prefill_retries=2, empty_retries=3) is RecoveryAction.COMPLETE


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
# loop: persistently empty is bounded then falls back                          #
# --------------------------------------------------------------------------- #


class _AlwaysEmptyProvider(LLMProvider):
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
        return LLMResponse(content="", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_persistently_empty_is_bounded_then_falls_back(workspace):
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
    # plain-empty budget -> 1 initial call + N retries, then give up.
    assert provider.calls == 1 + limits.empty_content_max_retries
    assert "no response" in out[0].lower()


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


def test_classify_length_capped_gets_act_now_nudge():
    r = LLMResponse(content=None, reasoning_content="x" * 100, finish_reason="length")
    assert _classify(r, None) is RecoveryAction.TRUNCATED
    assert _classify(r, None, length_nudges=1) is RecoveryAction.TRUNCATED
    assert _classify(r, None, length_nudges=2) is not RecoveryAction.TRUNCATED


def test_classify_length_with_visible_text_completes():
    r = LLMResponse(content="partial answer", finish_reason="length")
    assert _classify(r, "partial answer") is RecoveryAction.COMPLETE


# --------------------------------------------------------------------------- #
# loop: synthetics + mid-turn checkpoint must not lose trailing real messages  #
# --------------------------------------------------------------------------- #


class _NudgeThenLongToolRunProvider(LLMProvider):
    """Call 2 is empty (spawns nudge synthetics), then enough tool calls to
    cross the mid-turn checkpoint interval, then a real answer.

    Regression: checkpoint indices are taken against the in-loop message list
    including synthetics; stripping synthetics before the final save shifted
    the slice and silently dropped trailing real messages from the session.
    """

    def __init__(self, tool_iters: int):
        super().__init__(api_key="test")
        self.calls = 0
        self._tool_iters = tool_iters

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
        if self.calls == 2:
            return LLMResponse(content="", finish_reason="stop")
        if self.calls <= self._tool_iters:
            from raven.providers.base import ToolCallRequest

            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id=f"c{self.calls}", name="no_such_tool", arguments={"n": self.calls})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="real answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_mid_turn_checkpoint_with_synthetics_keeps_tail_messages(workspace):
    provider = _NudgeThenLongToolRunProvider(tool_iters=14)
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=30,
        restrict_to_workspace=True,
    )

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
    session = agent.sessions.get_or_create("s1")
    contents = [str(m.get("content", "")) for m in session.messages]
    # the final assistant reply survives persistence...
    assert any("real answer" in c for c in contents)
    # ...no synthetic scaffolding leaks...
    for m in session.messages:
        assert not m.get("_recovery_synthetic")
    # ...and every executed tool call's result is persisted exactly once.
    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == len({m.get("tool_call_id") for m in tool_msgs})
    assert len(tool_msgs) == 13  # calls 1, 3..14 → 13 tool iterations


# --------------------------------------------------------------------------- #
# unit: mid-stream kill detection (provider aborts generation mid-reasoning)   #
# --------------------------------------------------------------------------- #


def test_midstream_kill_retries_instead_of_prefill():
    r = LLMResponse(
        content="",
        reasoning_content="x" * 5000,
        finish_reason="stop",
        usage={"prompt_tokens": 90_000, "completion_tokens": 1},
    )
    assert _classify(r, "", empty_retries=0) is RecoveryAction.RETRY


def test_genuine_thinking_only_still_prefills():
    # A real thinking-only turn bills its reasoning as completion tokens.
    r = LLMResponse(
        content="",
        reasoning_content="x" * 5000,
        finish_reason="stop",
        usage={"prompt_tokens": 90_000, "completion_tokens": 1400},
    )
    assert _classify(r, "") is RecoveryAction.PREFILL


def test_missing_usage_keeps_prefill_path():
    r = LLMResponse(content="", reasoning_content="x" * 5000, finish_reason="stop")
    assert _classify(r, "") is RecoveryAction.PREFILL


def test_midstream_kill_with_small_reasoning_not_flagged():
    from raven.agent.loop.recovery import is_midstream_kill

    r = LLMResponse(content="", reasoning_content="short", finish_reason="stop",
                    usage={"completion_tokens": 1})
    assert is_midstream_kill(r) is False
