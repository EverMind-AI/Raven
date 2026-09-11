"""How much of the context window a turn holds back for the answer.

A request names an output ceiling, so the reservation is that exact number: the
prompt and that reply have to fit the window together, and handing out more
means the sum is refused at request time -- a refusal whose recovery only
elides tool bodies, so a history grown on conversation dies there.

Which makes the *spelling* load-bearing, not only the arithmetic. The
reservation and the request have to resolve from the same model id, and the id
a request goes out under is not always the one it was configured with (see
`_wire_output_ceiling`). What the catalogue's implausible rows used to make of
this -- a ceiling equal to the window, leaving nothing for history -- is
handled upstream, where such a row is distrusted (see `providers.rates`).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import raven.agent.harness.memory as budget_owner
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.providers.base import LLMProvider, LLMResponse, send_max_tokens
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
    monkeypatch.setattr(budget_owner, "send_max_tokens", lambda *a, **k: ceiling)
    agent = AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    # The window is the binding's, so the fixture sets it where it lives
    # rather than on the loop -- which no longer has one of its own.
    agent._default_binding = ModelBinding(agent._default_binding.provider, agent._default_binding.model, window)
    return agent


def test_the_reservation_reads_the_id_the_request_goes_out_under(workspace, monkeypatch) -> None:
    """The reservation and the request have to resolve from one spelling.

    Measured on the deck agent's own model: configured `z-ai/glm-5.3-flash` has
    no catalogue row under that name, so the reservation fell to half the
    default window (32768) while the request -- built from the wire spelling
    `openrouter/z-ai/glm-5.3-flash`, which does have one -- carried 64000. The
    loop then handed out a prompt sized against a reply twice as large as it
    had reserved for.
    """
    import litellm

    from raven.providers import rates

    # Nothing in LiteLLM under either spelling, which is the deck model's real
    # situation: the declaration lives only in OpenRouter's catalogue, and that
    # tier answers for an id naming OpenRouter and for no other.
    monkeypatch.setattr(litellm, "model_cost", {})
    monkeypatch.setattr(litellm, "get_model_info", lambda *a, **k: {})
    rates.reset_openrouter_cache()
    monkeypatch.setattr(
        rates,
        "_OPENROUTER_CACHE",
        {"vendor/thing": {"context_length": 400_000, "max_completion_tokens": 100_000}},
        raising=False,
    )
    monkeypatch.setattr(rates, "_OPENROUTER_CACHE_TIME", 0.0, raising=False)

    class _PrefixingProvider(_StubProvider):
        def wire_model_id(self, model: str) -> str:
            return model if model.startswith("openrouter/") else f"openrouter/{model}"

    agent = AgentLoop(
        provider=_PrefixingProvider(),
        workspace=workspace,
        model="vendor/thing",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    assert agent._wire_output_ceiling() == 100_000, "the wire spelling reaches the catalogue, so it answers"
    assert send_max_tokens(None, "vendor/thing", allow_fetch=False) == 49_152, (
        "the configured spelling reaches nothing and falls back to the default window's reserve"
    )


def test_a_ceiling_as_large_as_the_window_still_leaves_room_for_history(workspace, monkeypatch) -> None:
    """Measured before this bound: `openrouter/anthropic/claude-haiku-4.5`
    reports a 200000 ceiling on a 200000 window, so `available_history` was
    exactly 0 -- no history fits in the budget, on every turn.
    """
    budget = _loop(workspace, window=200_000, ceiling=64_000, monkeypatch=monkeypatch)._make_token_budget()

    assert budget.reserved_output == 64_000, "what the reply may actually use"
    # Two bounds, because two different things were being guarded through one
    # number and the tighter of them rode on where the repo is checked out.
    #
    # `available_history` is the docstring's subject: the measured 0 that
    # started this, history squeezed to nothing by an honest-but-huge ceiling.
    # A loose bound is the right shape for it -- `reserved_system` embeds the
    # workspace path, so this figure moves with the length of a temp directory:
    # measured 129_090 / 129_060 / 129_035 at path lengths 22 / 62 / 121.
    assert budget.available_history > 128_000, "an honest ceiling must not squeeze history toward zero"
    # `reserved_tools` is what the old 129_000 bound was really watching, and it
    # is invariant across those same three paths: 5773 tokens, paid on every
    # turn of every conversation. Asserted directly so a grown tool description
    # trips it for that reason rather than because a worktree sits deeper than
    # the one this was measured on. Trim somewhere before raising it, and say
    # what was traded.
    assert budget.reserved_tools < 6_000, f"tool surface grew: {budget.reserved_tools} tokens reserved"


def test_an_honest_but_large_ceiling_does_not_eat_the_window(workspace, monkeypatch) -> None:
    """`z-ai/glm-4.6` really does allow 131000 output tokens on a 202800 window.

    Nothing about that number is wrong; reserving all of it is. The turn that
    spends its whole ceiling on one answer is rare, and paying for it on every
    other turn costs two thirds of the history.
    """
    budget = _loop(workspace, window=202_800, ceiling=131_000, monkeypatch=monkeypatch)._make_token_budget()

    assert budget.reserved_output == 131_000, "the model really can emit this much"
    # 60_000 rather than 65_000: the system prompt embeds the workspace path,
    # so the measured figure rides a few dozen tokens with tmp-path length.
    # The bound guards history keeping most of the leftover window, not an
    # exact split, so it stands clear of the boundary instead of on it.
    assert budget.available_history > 60_000


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
