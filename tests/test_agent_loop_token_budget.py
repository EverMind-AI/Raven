"""How much of the context window a turn holds back for the answer.

The reservation used to be a configured constant (8192) and became the model's
whole resolved output ceiling when that setting was retired. For a model whose
ceiling equals its window -- which the catalogue reports for more rows than
not -- that leaves nothing for history at all, and even an honest 131000
ceiling on a 202800 window spends two thirds of it on an answer the turn
probably will not produce.

So the reservation is bounded by a share of the window as well as by the
ceiling. The share is LiteLLM's own: `trim_messages` gives a prompt 75% of the
window and never consults an output ceiling.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import raven.agent.loop.main as agent_main
from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse


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
    agent.context_window_tokens = window
    return agent


def test_a_ceiling_as_large_as_the_window_still_leaves_room_for_history(workspace, monkeypatch) -> None:
    """Measured before this bound: `openrouter/anthropic/claude-haiku-4.5`
    reports a 200000 ceiling on a 200000 window, so `available_history` was
    exactly 0 -- no history fits in the budget, on every turn.
    """
    budget = _loop(workspace, window=200_000, ceiling=200_000, monkeypatch=monkeypatch)._make_token_budget()

    assert budget.reserved_output == 50_000
    assert budget.available_history > 100_000


def test_an_honest_but_large_ceiling_does_not_eat_the_window(workspace, monkeypatch) -> None:
    """`z-ai/glm-4.6` really does allow 131000 output tokens on a 202800 window.

    Nothing about that number is wrong; reserving all of it is. The turn that
    spends its whole ceiling on one answer is rare, and paying for it on every
    other turn costs two thirds of the history.
    """
    budget = _loop(workspace, window=202_800, ceiling=131_000, monkeypatch=monkeypatch)._make_token_budget()

    assert budget.reserved_output == 50_700
    assert budget.available_history > 140_000


def test_a_ceiling_below_the_share_is_reserved_in_full(workspace, monkeypatch) -> None:
    """The bound is a cap, not a target. gpt-4o's 16384 on a 128000 window is
    well under the share, and reserving the share instead would hold back
    15616 tokens the model cannot use.
    """
    budget = _loop(workspace, window=128_000, ceiling=16_384, monkeypatch=monkeypatch)._make_token_budget()

    assert budget.reserved_output == 16_384


def test_the_share_is_named_rather_than_spelled_at_the_call_site(workspace, monkeypatch) -> None:
    """It is an inherited judgement, not a derived quantity -- the next reader
    has to be able to find where it came from and what it trades off."""
    assert 0 < agent_main._OUTPUT_RESERVATION_SHARE < 1

    monkeypatch.setattr(agent_main, "_OUTPUT_RESERVATION_SHARE", 0.5)
    budget = _loop(workspace, window=200_000, ceiling=200_000, monkeypatch=monkeypatch)._make_token_budget()

    assert budget.reserved_output == 100_000
