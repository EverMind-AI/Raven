"""How much of the context window a turn holds back for the answer.

Requests do not name an output ceiling, so the one that applies is the model's
own -- whatever the vendor, or LiteLLM's transformation, fills in. The prompt
and that reply have to fit the window together, which fixes the reservation at
the ceiling itself: hand out more and the sum is refused at request time, and
the recovery from that refusal only elides tool bodies, so a history grown on
conversation dies there.

A share of the window would be enough only if the request carried that share as
its ceiling. It did, briefly, and stopping the request from naming one is what
put this back. What the catalogue's implausible rows used to make of this --
a ceiling equal to the window, leaving nothing for history -- is handled
upstream now, where such a row is distrusted (see `providers.rates`).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import raven.agent.loop.main as agent_main
from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse
from raven.providers.binding import ModelBinding


class _StubProvider(LLMProvider):
    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok", finish_reason="stop")


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _loop(workspace: Path, *, window: int, ceiling: int, monkeypatch) -> AgentLoop:
    monkeypatch.setattr(agent_main, "send_max_tokens", lambda *a, **k: ceiling)
    agent = AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
    )
    # The window is the binding's, so the fixture sets it where it lives
    # rather than on the loop -- which no longer has one of its own.
    agent._default_binding = ModelBinding(agent._default_binding.provider, agent._default_binding.model, window)
    return agent


def test_a_ceiling_as_large_as_the_window_still_leaves_room_for_history(workspace, monkeypatch) -> None:
    """Measured before this bound: `openrouter/anthropic/claude-haiku-4.5`
    reports a 200000 ceiling on a 200000 window, so `available_history` was
    exactly 0 -- no history fits in the budget, on every turn.
    """
    budget = _loop(workspace, window=200_000, ceiling=64_000, monkeypatch=monkeypatch)._make_token_budget()

    assert budget.reserved_output == 64_000, "what the reply may actually use"
    assert budget.available_history > 130_000


def test_an_honest_but_large_ceiling_does_not_eat_the_window(workspace, monkeypatch) -> None:
    """`z-ai/glm-4.6` really does allow 131000 output tokens on a 202800 window.

    Nothing about that number is wrong; reserving all of it is. The turn that
    spends its whole ceiling on one answer is rare, and paying for it on every
    other turn costs two thirds of the history.
    """
    budget = _loop(workspace, window=202_800, ceiling=131_000, monkeypatch=monkeypatch)._make_token_budget()

    assert budget.reserved_output == 131_000, "the model really can emit this much"
    assert budget.available_history > 65_000


def test_a_ceiling_below_the_share_is_reserved_in_full(workspace, monkeypatch) -> None:
    """The bound is a cap, not a target. gpt-4o's 16384 on a 128000 window is
    well under the share, and reserving the share instead would hold back
    15616 tokens the model cannot use.
    """
    budget = _loop(workspace, window=128_000, ceiling=16_384, monkeypatch=monkeypatch)._make_token_budget()

    assert budget.reserved_output == 16_384


def test_a_configured_window_smaller_than_the_model_s_still_leaves_a_budget(workspace, monkeypatch) -> None:
    """A user who pins a small context window must not end up with a negative
    one: the ceiling is the model's, and it can exceed a window chosen by hand.
    """
    budget = _loop(workspace, window=32_000, ceiling=64_000, monkeypatch=monkeypatch)._make_token_budget()

    assert budget.reserved_output == 32_000, "never more than the window it is carved from"
    assert budget.available_history == 0
