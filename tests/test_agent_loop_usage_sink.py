"""Tests for the usage_sink populated by AgentLoop and surfaced to the TUI.

Pins the wire shape that ``turn.send`` relays as ``message.complete.payload.usage``:
per-turn token counts plus the live context-window gauge (used / max / percent)
and the estimated cost. Before this, only the token counts were populated, so the
TUI context bar stayed frozen at 0% and never showed cost.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx
import pytest

from raven.agent.loop import AgentLoop
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.providers.base import LLMProvider, LLMResponse
from raven.token_wise import pricing

# The real fetch, captured before conftest's autouse guard stubs it to {}.
_REAL_FETCH = pricing._fetch_openrouter_models


class UsageProvider(LLMProvider):
    """Returns a fixed reply with a known usage snapshot. No tool calls."""

    def __init__(self, model: str, prompt_tokens: int, completion_tokens: int):
        super().__init__(api_key="test")
        self._model = model
        self._usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                   temperature=0.7, reasoning_effort=None, tool_choice=None):
        return LLMResponse(content="ok", finish_reason="stop", usage=self._usage)

    def get_default_model(self) -> str:
        return self._model


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture(autouse=True)
def _reset_openrouter_cache():
    pricing._OPENROUTER_CACHE.clear()
    yield
    pricing._OPENROUTER_CACHE.clear()


def _make_agent(workspace: Path, provider: LLMProvider, model: str, window: int) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model=model,
        max_iterations=2,
        context_window_tokens=window,
        restrict_to_workspace=True,
    )


@pytest.mark.asyncio
async def test_usage_sink_carries_context_gauge_and_cost(workspace):
    """A non-openrouter model fills used/percent against the configured window."""
    provider = UsageProvider("stub", prompt_tokens=6000, completion_tokens=2000)
    agent = _make_agent(workspace, provider, model="stub", window=40000)
    sink: dict = {}

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
        usage_sink=sink,
    )

    assert sink["context_max"] == 40000
    assert sink["context_used"] == 8000
    assert sink["context_percent"] == 20
    assert "cost_usd" in sink


@pytest.mark.asyncio
async def test_usage_sink_context_max_from_live_openrouter(workspace, monkeypatch):
    """An OpenRouter model LiteLLM lags on gets its real window from /models."""
    models = [{
        "id": "deepseek/deepseek-v4-pro",
        "context_length": 163840,
        "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
    }]

    def handler(_req):
        return httpx.Response(200, content=json.dumps({"data": models}))

    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(pricing, "_fetch_openrouter_models", _REAL_FETCH)
    monkeypatch.setattr(pricing.httpx, "Client", client_factory)
    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE_TIME", 0.0)

    provider = UsageProvider("openrouter/deepseek/deepseek-v4-pro", 1000, 500)
    agent = _make_agent(
        workspace, provider, model="openrouter/deepseek/deepseek-v4-pro", window=8192,
    )
    sink: dict = {}

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        session_key="s1",
        usage_sink=sink,
    )

    assert sink["context_max"] == 163840
    assert sink["context_used"] == 1500
