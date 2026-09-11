"""The loop's own pre-request awaits are bounded, and a stall there is reported.

Every timeout a provider holds can only fire once the call has been entered.
The deck stall of 2026-09-10 never got that far: ``Iteration 1/600`` opened,
``memory.recall`` and ``skill.inject`` finished, and then fifteen minutes passed
with no ``llm.call`` span, no ``llm.input`` artefact, no retry line and no
established outbound socket -- the turn never issued its first request, so
nothing at the provider could have caught it. A retry of the identical turn was
healthy.

Covers:
- a hook that never returns does not hold the turn: the stage is abandoned at
  ``llmFirstByteTimeout`` and the model is asked anyway
- a strategy that never returns leaves the request un-hooked rather than unsent
- a ``TimeoutError`` the stage raised itself is not this guard's deadline and
  propagates, so a bad pre-process still cannot send an unprocessed request
- the record names the stage, the bound and how long it waited
- a budget of 0 switches the guard off, and a stage that answers in time is
  untouched
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, HostWiring, ToolWiring, TurnPolicy
from raven.agent.loop.first_call import FirstCallGuard
from raven.contracts.llm_provider import GenerationSettings
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision
from raven.contracts.token_strategy import TokenStrategy
from raven.providers.base import LLMProvider, LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.token_wise.registry import StrategyRegistry


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _Answers(LLMProvider):
    """Answers at once, and records what it was asked with."""

    def __init__(self, first_byte_timeout: float = 0.05):
        super().__init__(api_key="test")
        self.generation = GenerationSettings(timeout=600.0, first_byte_timeout=first_byte_timeout)
        self.calls = 0
        self.asked_tools: list[Any] = []

    def get_default_model(self) -> str:
        return "stub"

    _CHAT_RETRY_DELAYS = ()

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **_):
        self.calls += 1
        self.asked_tools.append(tools)
        return LLMResponse(content="real answer", finish_reason="stop")

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover - non-stream path under test
        raise NotImplementedError


class _StallingHook(AgentHook):
    """A ``before_iteration`` that never comes back."""

    def __init__(self) -> None:
        self.entered = 0

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        self.entered += 1
        await asyncio.sleep(30)
        return HookDecision(short_circuit_result="the hook answered")  # pragma: no cover


class _StallingStrategy(TokenStrategy):
    """A ``before_llm_call`` that never comes back."""

    name = "stalls"

    def __init__(self) -> None:
        self.entered = 0

    async def before_llm_call(self, messages, tools, model):
        self.entered += 1
        await asyncio.sleep(30)
        return [], None, model  # pragma: no cover


def _agent(workspace: Path, provider: LLMProvider, *, hooks=None, strategies=None) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=4),
        tools=ToolWiring(restrict_to_workspace=True),
        host=HostWiring(hooks=hooks or []),
        engine=EngineWiring(strategies=StrategyRegistry(strategies or [])),
    )


async def _turn(agent: AgentLoop):
    return await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="build the deck",
        ),
        session_key="s1",
    )


@pytest.mark.asyncio
async def test_a_hook_that_never_returns_does_not_hold_the_turn(workspace):
    hook = _StallingHook()
    provider = _Answers()
    agent = _agent(workspace, provider, hooks=[hook])

    out = await asyncio.wait_for(_turn(agent), 20)

    assert hook.entered >= 1, "the stalling hook really was entered"
    assert provider.calls >= 1, "the model was asked even though the hook never answered"
    assert out is not None and out[0] == "real answer"


@pytest.mark.asyncio
async def test_a_stalled_strategy_leaves_the_request_unhooked(workspace):
    strategy = _StallingStrategy()
    provider = _Answers()
    agent = _agent(workspace, provider, strategies=[strategy])

    out = await asyncio.wait_for(_turn(agent), 20)

    assert strategy.entered >= 1
    assert provider.calls >= 1
    assert provider.asked_tools[0], "asked with the loop's own tool schemas, not the strategy's empty result"
    assert out is not None and out[0] == "real answer"


@pytest.mark.asyncio
async def test_a_stages_own_timeout_error_is_not_this_guards_deadline():
    """``asyncio.wait_for`` re-raises a child's ``TimeoutError`` as its own.

    So an ``except TimeoutError`` around it read a strategy's immediate failure
    as a 0.0-second first-call stall and answered the fallback -- which around
    ``StrategyRegistry.before_llm_call`` means sending the original messages,
    tools and model. The TokenStrategy contract has before-call errors propagate
    exactly so a bad pre-process cannot send an unprocessed request.
    """
    raised: list[str] = []

    async def refuses():
        raised.append("ran")
        raise TimeoutError("the strategy's own bound, not the guard's")

    guard = FirstCallGuard(30.0, model="stub")
    with pytest.raises(TimeoutError, match="the strategy's own bound"):
        await guard.stage("the before_llm_call strategies", refuses(), iteration=1, fallback=("fallback",))
    assert raised == ["ran"]


@pytest.mark.asyncio
async def test_a_stages_own_timeout_error_is_not_recorded_as_a_stall():
    """And it leaves no first-call-guard record: nothing stalled."""
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m), level="ERROR")

    async def refuses():
        raise TimeoutError("mine, not yours")

    try:
        guard = FirstCallGuard(30.0, model="stub")
        with pytest.raises(TimeoutError):
            await guard.stage("the before_iteration hooks", refuses(), iteration=1, fallback=None)
    finally:
        logger.remove(sink)

    assert not [ln for ln in lines if "First call guard" in ln]


@pytest.mark.asyncio
async def test_the_record_names_the_stage_and_the_wait(workspace):
    """Today's failure left nothing; a stall must say which bound was hit and
    how long it waited."""
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m), level="ERROR")
    try:
        agent = _agent(workspace, _Answers(), hooks=[_StallingHook()])
        await asyncio.wait_for(_turn(agent), 20)
    finally:
        logger.remove(sink)

    stalls = [ln for ln in lines if "First call guard" in ln]
    assert stalls, f"no first-call-guard record in {lines}"
    record = stalls[0]
    assert "the before_iteration hooks" in record
    assert "llmFirstByteTimeout" in record
    assert "quiet for 0.1s" in record or "quiet for 0.0s" in record


@pytest.mark.asyncio
async def test_a_stage_that_answers_in_time_is_untouched():
    guard = FirstCallGuard(5.0, model="stub")
    assert await guard.stage("x", asyncio.sleep(0, result="value"), iteration=1, fallback="fallback") == "value"


@pytest.mark.asyncio
async def test_a_zero_budget_switches_the_guard_off():
    """0 restores what the loop did before: no bound at all on these awaits."""

    async def slow():
        await asyncio.sleep(0.15)
        return "late but delivered"

    guard = FirstCallGuard(0, model="stub")
    assert guard.budget == 0.0
    assert await guard.stage("x", slow(), iteration=1, fallback="fallback") == "late but delivered"


@pytest.mark.asyncio
async def test_the_stalled_awaitable_is_cancelled_not_left_running():
    """A guard that abandoned a stage must not leave its coroutine alive to
    finish later and mutate the turn behind the loop's back."""
    finished: list[str] = []

    async def slow():
        await asyncio.sleep(0.3)
        finished.append("ran on")  # pragma: no cover

    guard = FirstCallGuard(0.05, model="stub")
    assert await guard.stage("x", slow(), iteration=1, fallback=None) is None
    await asyncio.sleep(0.4)
    assert finished == []
