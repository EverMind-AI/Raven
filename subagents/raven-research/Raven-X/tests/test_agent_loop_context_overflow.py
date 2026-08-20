"""Context-overflow recovery: shrink the request and retry, never die on it.

The structured classifier flags ``should_compress`` on a context-window
overflow. The loop then has two moves, cheapest first: clamp the completion
reserve (lossless — the reserve is part of the request) and, failing that,
elide older tool-result bodies. Either way it retries the iteration rather
than ending the turn with an error.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.utils.helpers import estimate_prompt_tokens

_PLACEHOLDER = "[earlier tool output elided to fit the context window]"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# --------------------------------------------------------------------------- #
# unit: _emergency_shrink                                                      #
# --------------------------------------------------------------------------- #


def test_emergency_shrink_elides_all_but_recent_tool_results():
    msgs: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}"}]})
        msgs.append({"role": "tool", "content": f"result {i}"})

    shrunk, elided = AgentLoop._emergency_shrink(msgs)

    assert elided == 3  # 6 tool results, keep most-recent 3
    tool_contents = [m["content"] for m in shrunk if m["role"] == "tool"]
    assert tool_contents == [_PLACEHOLDER] * 3 + ["result 3", "result 4", "result 5"]
    # non-tool messages untouched
    assert shrunk[0]["content"] == "sys" and shrunk[1]["content"] == "q"


def test_emergency_shrink_noop_when_few_tool_results():
    msgs = [{"role": "system", "content": "s"}, {"role": "tool", "content": "r0"}]
    shrunk, elided = AgentLoop._emergency_shrink(msgs)
    assert elided == 0 and shrunk is msgs


# --------------------------------------------------------------------------- #
# loop level: overflow -> shrink -> recover                                    #
# --------------------------------------------------------------------------- #


class _OverflowThenAnswerProvider(LLMProvider):
    """Accumulates tool results, overflows once, then answers after the shrink."""

    def __init__(self, tool_rounds: int = 5):
        super().__init__(api_key="test")
        self._tool_rounds = tool_rounds
        self._overflowed = False
        self.seen_messages: list[list[dict]] = []

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
        self.seen_messages.append([dict(m) for m in messages])
        n_tool = sum(1 for m in messages if m.get("role") == "tool")
        if n_tool < self._tool_rounds:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id=f"t{n_tool}", name="no_such_tool", arguments={})],
                finish_reason="tool_calls",
            )
        if not self._overflowed:
            self._overflowed = True
            return LLMResponse(
                content="This model's maximum context length (8192 tokens) was exceeded",
                finish_reason="error",
            )
        return LLMResponse(content="answer after compaction", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_overflow_shrinks_and_recovers(workspace):
    provider = _OverflowThenAnswerProvider(tool_rounds=5)
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=12,
        restrict_to_workspace=True,
    )

    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )

    assert out is not None
    assert out[0] == "answer after compaction"  # recovered, not the error
    assert provider._overflowed is True
    # the post-overflow (recovery) call saw elided placeholders, not 5 full results
    recovery_call = provider.seen_messages[-1]
    assert sum(1 for m in recovery_call if m.get("content") == _PLACEHOLDER) == 2  # 5 - keep 3


# --------------------------------------------------------------------------- #
# unit: _completion_clamp                                                      #
# --------------------------------------------------------------------------- #


class _IdleProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, **kwargs):
        return LLMResponse(content="idle", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


def _clamp_agent(workspace, window, *, clamp_enabled=True, factor=0.5):
    agent = AgentLoop(
        provider=_IdleProvider(api_key="test"),
        workspace=workspace,
        model="stub",
        context_window_tokens=window,
        restrict_to_workspace=True,
    )
    # The reactive clamp is carried on the DR assembly and off by default, so a bare
    # AgentLoop cannot reach it. These unit tests are about the arithmetic, so they
    # switch it on explicitly rather than asserting against the disabled path -
    # ``test_the_reactive_clamp_is_unreachable_by_default`` covers that half.
    agent._dr_flow = SimpleNamespace(
        reactive_clamp=clamp_enabled, reactive_clamp_factor=factor
    )
    return agent


def test_the_reactive_clamp_is_unreachable_by_default(workspace):
    """No DR assembly -> no reactive clamp, whatever the numbers say.

    This is the half that matters for every published reading: the anchor builds no
    assembly at all, so the capability cannot move it.
    """
    agent = AgentLoop(
        provider=_IdleProvider(api_key="test"),
        workspace=workspace,
        model="stub",
        context_window_tokens=5_000,
        restrict_to_workspace=True,
    )
    assert agent._dr_flow is None
    assert agent._completion_clamp(4096) is None


def test_completion_clamp_shrinks_the_cap_that_was_in_force(workspace):
    """It shrinks the reserve by a factor, and does NOT consult the token estimator.

    That is the whole repair. The old version recomputed ``room`` from
    ``estimate_prompt_tokens`` and compared it against ``cap`` - the same predicate
    ``_fit_request`` had already applied pre-call - so it returned ``None`` on every
    path in production. A ``should_compress`` rejection means the server's accounting
    disagreed with ours, so re-deriving our own number cannot inform the retry.
    """
    agent = _clamp_agent(workspace, window=65_536)
    assert agent._completion_clamp(8192) == 4096
    # Independent of the window, because it no longer measures the prompt at all.
    narrow = _clamp_agent(workspace, window=5_000)
    assert narrow._completion_clamp(8192) == 4096


def test_completion_clamp_declines_below_the_usable_floor(workspace):
    """Going under the floor trades an overflow for an empty answer - same score,
    harder to detect."""
    agent = _clamp_agent(workspace, window=65_536)
    assert agent._completion_clamp(4096) == 2048            # exactly at the floor
    assert agent._completion_clamp(4095) is None            # would land under it


def test_completion_clamp_declines_when_the_factor_would_not_shrink(workspace):
    agent = _clamp_agent(workspace, window=65_536, factor=1.0)
    assert agent._completion_clamp(8192) is None


def test_completion_clamp_compounds_across_attempts(workspace):
    """``attempt`` is not a parameter: the caller assigns the result back to
    ``completion_cap``, so the factor compounds by itself."""
    agent = _clamp_agent(workspace, window=65_536)
    first = agent._completion_clamp(16_384)
    assert first == 8192
    assert agent._completion_clamp(first) == 4096


# --------------------------------------------------------------------------- #
# loop level: overflow -> clamp the reserve -> recover (no evidence lost)       #
# --------------------------------------------------------------------------- #


class _OverflowOnceProvider(LLMProvider):
    """Overflows the first call, then answers; records the reserve each call."""

    def __init__(self):
        super().__init__(api_key="test")
        self.max_tokens_seen: list[int] = []

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
        self.max_tokens_seen.append(max_tokens)
        if len(self.max_tokens_seen) == 1:
            return LLMResponse(
                content="Requested token count exceeds the model's maximum context length of 5000 tokens",
                finish_reason="error",
            )
        return LLMResponse(content="answer after clamping", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_reactive_clamp_is_the_net_when_the_pre_call_estimate_was_wrong(workspace):
    """With the pre-call fit in place the reactive clamp only fires on estimate
    error (the server counts more tokens than we do), so that is what this
    simulates: the fit is stubbed out and the 400 has to be recovered."""
    provider = _OverflowOnceProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=6,
        restrict_to_workspace=True,
    )
    # Size the window so the 4096-token reserve is exactly what does not fit —
    # derived from the live tool surface, so registering a tool can't silently
    # turn this into a no-room case where eliding is the only option.
    first_call = [{"role": "user", "content": "go"}]
    used = estimate_prompt_tokens(list(first_call), agent.tools.get_definitions())
    agent.context_window_tokens = used + 1_500 + 3_596
    agent._fit_request = lambda msgs, tools, model, cap: (msgs, cap, 0)
    # 20260813: the capability is gated off by default, so the loop-level test has to
    # ask for it. Before the gate existed this test passed while the production path
    # was dead - it stubs ``_fit_request`` (see the docstring above), and that stub is
    # exactly the condition that does not hold in a real run. A unit test that removes
    # the interaction it depends on asserts its own copy of the predicate.
    agent._dr_flow = SimpleNamespace(reactive_clamp=True, reactive_clamp_factor=0.5)

    final, _, messages, _ = await agent._run_agent_loop(first_call)

    assert final == "answer after clamping"
    # No pin on the first call: generation.max_tokens defaults to None, which
    # chat_with_retry forwards as "the model's own ceiling applies".
    assert provider.max_tokens_seen[0] is None
    # The clamp retry pins a real number below the 4096 the clamp assumed.
    assert AgentLoop._MIN_CLAMPED_COMPLETION_TOKENS <= provider.max_tokens_seen[1] < 4096
    assert not any(m.get("content") == _PLACEHOLDER for m in messages)


# --------------------------------------------------------------------------- #
# unit: _fit_request — shrink before sending, not after the 400                #
# --------------------------------------------------------------------------- #


def test_fit_request_noop_when_the_request_already_fits(workspace):
    agent = _clamp_agent(workspace, window=65_536)
    msgs = [{"role": "user", "content": "q"}]
    out, cap, elided = agent._fit_request(msgs, [], "stub", None)
    assert out is msgs and cap is None and elided == 0


def test_fit_request_lowers_the_reserve_before_eliding(workspace):
    """Lossless first: the reserve shrinks and the evidence stays intact."""
    agent = _clamp_agent(workspace, window=5_000)
    msgs: list[dict] = [{"role": "user", "content": "q"}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}"}]})
        msgs.append({"role": "tool", "content": f"result {i}"})

    out, cap, elided = agent._fit_request(msgs, [], "stub", None)

    assert elided == 0
    assert AgentLoop._MIN_CLAMPED_COMPLETION_TOKENS <= cap < 4096
    assert not any(m.get("content") == _PLACEHOLDER for m in out)


def test_fit_request_elides_only_when_the_floor_will_not_fit(workspace):
    agent = _clamp_agent(workspace, window=6_000)
    msgs: list[dict] = [{"role": "user", "content": "q"}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}"}]})
        msgs.append({"role": "tool", "content": "x" * 4_000})

    out, cap, elided = agent._fit_request(msgs, [], "stub", None)

    assert elided == 3  # 6 tool results, keep the most recent 3
    assert sum(1 for m in out if m.get("content") == _PLACEHOLDER) == 3


def test_fit_request_never_raises_the_reserve_back(workspace):
    """History only grows, so a reserve that had to shrink must stay shrunk."""
    agent = _clamp_agent(workspace, window=65_536)
    out, cap, elided = agent._fit_request([{"role": "user", "content": "q"}], [], "stub", 3_000)
    assert cap == 3_000 and elided == 0


class _WindowAwareProvider(LLMProvider):
    """Rejects any request whose reserve does not fit — like a real server."""

    def __init__(self, max_reserve: int):
        super().__init__(api_key="test")
        self._max_reserve = max_reserve
        self.max_tokens_seen: list[int] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, **kwargs):
        self.max_tokens_seen.append(max_tokens)
        if max_tokens > self._max_reserve:
            return LLMResponse(
                content="Requested token count exceeds the model's maximum context length of 5000 tokens",
                finish_reason="error",
            )
        return LLMResponse(content="answer after clamping", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_pre_call_fit_keeps_the_turn_off_the_overflow_path(workspace):
    """The 400 round-trip should not happen at all when the fit is knowable."""
    provider = _WindowAwareProvider(max_reserve=3_600)
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=6,
        restrict_to_workspace=True,
    )
    first_call = [{"role": "user", "content": "go"}]
    used = estimate_prompt_tokens(list(first_call), agent.tools.get_definitions())
    agent.context_window_tokens = used + 1_500 + 3_596

    final, _, messages, _ = await agent._run_agent_loop(first_call)

    # The provider only ever sees a request whose reserve already fits, so its
    # scripted overflow (fired on the first call) never triggers.
    assert final == "answer after clamping"
    assert provider.max_tokens_seen[0] < 4096
    observers = [m.get("observers") for m in messages if m.get("observers")]
    assert observers and observers[-1]["turn_end"]["overflows"] == 0
    assert observers[-1]["turn_end"]["prefits"] == 1
