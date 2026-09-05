"""SpinEntryBreaker: intercept restart language, spare legitimate re-anchors.

Joint criteria under test: the first restart phrase of a turn passes,
early-budget restarts pass, a restart that introduces new entities
passes (re-anchor), and only a repeated same-ground restart late in the
budget is popped and redirected to the report.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.spin_breaker import SpinEntryBreaker  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402


def _ctx(text, iteration=8, has_tool_calls=False):
    ctx = AgentHookContext(session_key="cli:test")
    ctx.iteration = iteration
    ctx.messages = [{"role": "user", "content": "task"}]
    ctx.response = SimpleNamespace(
        has_tool_calls=has_tool_calls,
        content=text,
        reasoning_content=None,
        usage={},
    )
    return ctx


# The fixtures deliberately no longer say "different approach": the identity
# segment instructs the model to use that exact phrase every turn, so it appeared
# on 75 of 120 anchor questions and carried no restart signal. Testing against it
# would test a marker the detector no longer watches.
_SAME_GROUND_1 = "I keep confusing Murmansk and Arkhangelsk near Severodvinsk. Let me start over on this."
_SAME_GROUND_2 = "This is going in circles - Murmansk, Arkhangelsk, Severodvinsk again. I will start from scratch."
_NEW_GROUND = "Starting over: instead of ports, check the Kandalaksha, Onega and Belomorsk shipyard records."


@pytest.mark.asyncio
async def test_first_restart_phrase_passes():
    breaker = SpinEntryBreaker(max_iterations=10)
    decision = await breaker.after_iteration(_ctx(_SAME_GROUND_1))

    assert decision.rollback is False


@pytest.mark.asyncio
async def test_repeated_same_ground_restart_triggers():
    breaker = SpinEntryBreaker(max_iterations=10)
    ctx = _ctx(_SAME_GROUND_1)
    await breaker.after_iteration(ctx)

    ctx.response = _ctx(_SAME_GROUND_2).response
    ctx.iteration = 9
    decision = await breaker.after_iteration(ctx)

    assert decision.rollback is True
    assert decision.rollback_inject[0]["role"] == "user"
    assert decision.rollback_inject[0]["content"].startswith("[research checkpoint]")
    assert ctx.metadata["spin_breaker"]["triggers"] == 1


@pytest.mark.asyncio
async def test_early_budget_restart_passes():
    breaker = SpinEntryBreaker(max_iterations=100)
    ctx = _ctx(_SAME_GROUND_1, iteration=2)
    await breaker.after_iteration(ctx)
    ctx.response = _ctx(_SAME_GROUND_2).response
    ctx.iteration = 3

    decision = await breaker.after_iteration(ctx)

    assert decision.rollback is False


@pytest.mark.asyncio
async def test_new_entity_reanchor_passes():
    breaker = SpinEntryBreaker(max_iterations=10)
    ctx = _ctx(_SAME_GROUND_1)
    await breaker.after_iteration(ctx)
    ctx.response = _ctx(_NEW_GROUND).response
    ctx.iteration = 9

    decision = await breaker.after_iteration(ctx)

    assert decision.rollback is False
    assert "spin_breaker_pass_new_entities" in decision.notes


@pytest.mark.asyncio
async def test_trigger_budget_is_bounded():
    breaker = SpinEntryBreaker(max_iterations=10, max_triggers=1)
    ctx = _ctx(_SAME_GROUND_1)
    await breaker.after_iteration(ctx)
    ctx.response = _ctx(_SAME_GROUND_2).response
    assert (await breaker.after_iteration(ctx)).rollback is True

    ctx.response = _ctx(_SAME_GROUND_2).response
    decision = await breaker.after_iteration(ctx)

    assert decision.rollback is False
    assert ctx.metadata["spin_breaker"]["triggers"] == 1


@pytest.mark.asyncio
async def test_tool_call_restart_intercepted_before_execution():
    breaker = SpinEntryBreaker(max_iterations=10)
    ctx = _ctx(_SAME_GROUND_1, has_tool_calls=True)
    await breaker.before_execute_tools(ctx)
    ctx.response = _ctx(_SAME_GROUND_2, has_tool_calls=True).response
    ctx.iteration = 9

    decision = await breaker.before_execute_tools(ctx)

    assert decision.rollback is True


@pytest.mark.asyncio
async def test_plain_research_text_never_hits():
    breaker = SpinEntryBreaker(max_iterations=10)
    ctx = _ctx("The 1992 charter names Ada as founder; cross-checking the registry next.")

    decision = await breaker.after_iteration(ctx)

    assert decision.rollback is False
    assert "spin_breaker" not in ctx.metadata
