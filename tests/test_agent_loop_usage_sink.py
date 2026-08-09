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
from raven.providers import rates
from raven.providers.base import LLMProvider, LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

# The real fetch, captured before conftest's autouse guard stubs it to {}.
_REAL_FETCH = rates._fetch_openrouter_models


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
        return LLMResponse(content="ok", finish_reason="stop", usage=self._usage)

    def get_default_model(self) -> str:
        return self._model


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture(autouse=True)
def _reset_openrouter_cache():
    rates._OPENROUTER_CACHE.clear()
    yield
    rates._OPENROUTER_CACHE.clear()


def _make_agent(workspace: Path, provider: LLMProvider, model: str, window: int | None) -> AgentLoop:
    kwargs: dict = {}
    if window is not None:
        kwargs["context_window_tokens"] = window
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model=model,
        max_iterations=2,
        restrict_to_workspace=True,
        **kwargs,
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


def _patch_live_openrouter_window(monkeypatch, window: int) -> None:
    """Route the OpenRouter models fetch to report ``window`` for deepseek-v4-pro."""
    models = [
        {
            "id": "deepseek/deepseek-v4-pro",
            "context_length": window,
            "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
        }
    ]

    def handler(_req):
        return httpx.Response(200, content=json.dumps({"data": models}))

    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(rates, "_fetch_openrouter_models", _REAL_FETCH)
    monkeypatch.setattr(rates.httpx, "Client", client_factory)
    monkeypatch.setattr(rates, "_OPENROUTER_CACHE_TIME", 0.0)


@pytest.mark.asyncio
async def test_usage_sink_context_max_from_live_openrouter(workspace, monkeypatch):
    """Unpinned, an OpenRouter model LiteLLM lags on gets its real window from /models."""
    _patch_live_openrouter_window(monkeypatch, 163840)

    provider = UsageProvider("openrouter/deepseek/deepseek-v4-pro", 1000, 500)
    agent = _make_agent(
        workspace,
        provider,
        model="openrouter/deepseek/deepseek-v4-pro",
        window=None,
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


@pytest.mark.asyncio
async def test_usage_sink_context_max_stays_pinned_over_live_openrouter(workspace, monkeypatch):
    """A pinned window wins even when the model's live window disagrees."""
    _patch_live_openrouter_window(monkeypatch, 163840)

    provider = UsageProvider("openrouter/deepseek/deepseek-v4-pro", 1000, 500)
    agent = _make_agent(
        workspace,
        provider,
        model="openrouter/deepseek/deepseek-v4-pro",
        window=8192,
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

    assert sink["context_max"] == 8192
    assert sink["context_used"] == 1500


# --------------------------------------------------------------------------- #
# construction-time ladder: _context_window_pinned + refresh_context_window   #
# --------------------------------------------------------------------------- #


def test_no_configured_window_resolves_via_the_ladder_and_is_unpinned(workspace):
    """An unresolvable model falls back to the documented default, unpinned."""
    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=None)

    assert agent._context_window_pinned is False
    assert agent.context_window_tokens == rates.DEFAULT_CONTEXT_WINDOW_TOKENS


def test_a_configured_window_is_pinned_at_construction(workspace):
    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=8192)

    assert agent._context_window_pinned is True
    assert agent.context_window_tokens == 8192


def test_refresh_context_window_is_a_noop_once_pinned(workspace, monkeypatch):
    """A pin is a deliberate override; a later model switch must not discard it."""
    _patch_live_openrouter_window(monkeypatch, 163840)

    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=8192)

    agent.model = "openrouter/deepseek/deepseek-v4-pro"
    agent.refresh_context_window()

    assert agent.context_window_tokens == 8192


def test_refresh_context_window_follows_the_new_model_when_unpinned(workspace, monkeypatch):
    """Unpinned, a ``/model`` switch re-walks the ladder for the new model."""
    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=None)
    assert agent.context_window_tokens == rates.DEFAULT_CONTEXT_WINDOW_TOKENS

    _patch_live_openrouter_window(monkeypatch, 163840)
    agent.model = "openrouter/deepseek/deepseek-v4-pro"
    agent.refresh_context_window()

    assert agent.context_window_tokens == 163840
