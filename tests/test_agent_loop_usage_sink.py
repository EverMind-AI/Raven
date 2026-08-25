"""Tests for the usage_sink populated by AgentLoop and surfaced to the TUI.

Pins the wire shape that ``turn.send`` relays as ``message.complete.payload.usage``:
per-turn token counts plus the live context-window gauge (used / max / percent)
and the estimated cost. Before this, only the token counts were populated, so the
TUI context bar stayed frozen at 0% and never showed cost.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop import main as agent_loop_main
from raven.providers import rates
from raven.providers.base import LLMProvider, LLMResponse
from raven.providers.binding import ModelBinding, use_binding
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
    """Without an explicit window, an OpenRouter model LiteLLM lags on gets its real window from /models."""
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
async def test_usage_sink_context_max_stays_explicit_over_live_openrouter(workspace, monkeypatch):
    """An explicitly configured window wins even when the model's live window disagrees."""
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


@pytest.mark.asyncio
async def test_usage_sink_context_max_is_resolved_off_the_event_loop_thread(workspace, monkeypatch):
    """SF10: this per-call tier defaults to ``allow_fetch=True``, so a cold
    OpenRouter model with both caches expired can reach for a synchronous
    ~10s HTTP call. ``_run_agent_loop`` runs on the event loop, so that call
    must run on a worker thread, not inline."""
    seen: dict[str, threading.Thread] = {}

    def fake_resolve(model: str) -> int:
        seen["thread"] = threading.current_thread()
        return 99_999

    monkeypatch.setattr(agent_loop_main, "resolve_context_window", fake_resolve)

    provider = UsageProvider("stub", 1000, 500)
    agent = _make_agent(workspace, provider, model="stub", window=None)
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

    assert sink["context_max"] == 99_999
    assert seen["thread"] is not threading.current_thread()


# --------------------------------------------------------------------------- #
# construction-time ladder: _context_window_explicit + refresh_context_window   #
# --------------------------------------------------------------------------- #


def test_no_configured_window_resolves_via_the_ladder_and_is_not_explicit(workspace):
    """An unresolvable model falls back to the documented default, not explicit."""
    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=None)

    assert agent._configured_window is None
    assert agent.context_window_tokens == rates.DEFAULT_CONTEXT_WINDOW_TOKENS


def test_a_configured_window_is_explicit_at_construction(workspace):
    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=8192)

    assert agent._configured_window == 8192
    assert agent.context_window_tokens == 8192


def test_a_pinned_window_outranks_the_model_a_session_switched_to(workspace, monkeypatch):
    """A pin is a deliberate override; the model a session runs on cannot discard it."""
    _patch_live_openrouter_window(monkeypatch, 163840)

    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=8192)

    switched = ModelBinding(provider, "openrouter/deepseek/deepseek-v4-pro", agent._configured_window)
    with use_binding(switched):
        assert agent.context_window_tokens == 8192


def test_the_window_follows_the_model_the_turn_is_bound_to(workspace, monkeypatch):
    """Without an explicit pin, the window is whatever the turn's own model holds.

    The switch runs inside the running event loop, so the ladder is walked with
    ``allow_fetch=False`` -- an in-process cache entry of any age answers rather
    than a live fetch. Populated directly rather than through a mocked network
    call, which ``allow_fetch=False`` never reaches.
    """
    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=None)
    assert agent.context_window_tokens == rates.DEFAULT_CONTEXT_WINDOW_TOKENS

    # conftest's autouse guard stubs the fetch to a zero-argument lambda; restore
    # the real one so allow_fetch=False's in-process-cache branch actually runs.
    monkeypatch.setattr(rates, "_fetch_openrouter_models", _REAL_FETCH)
    rates._OPENROUTER_CACHE["deepseek/deepseek-v4-pro"] = {
        "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
        "context_length": 163840,
    }
    monkeypatch.setattr(rates, "_OPENROUTER_CACHE_TIME", time.time() - rates._OPENROUTER_CACHE_TTL - 1000)
    switched = ModelBinding(provider, "openrouter/deepseek/deepseek-v4-pro")
    with use_binding(switched):
        assert agent.context_window_tokens == 163840


# --------------------------------------------------------------------------- #
# allow_fetch=False: construction and refresh never touch the network        #
# --------------------------------------------------------------------------- #


def _forbid_network_client(monkeypatch):
    """Restore the real fetch, then count real ``httpx.Client`` builds.

    Not a raise: ``_fetch_openrouter_models`` wraps the fetch in
    ``except Exception`` to degrade on a network failure, so a raise from here
    would be swallowed as "the network failed" and the test would pass for the
    wrong reason. A counter the caller asserts is 0 actually distinguishes
    "never touched the network" from "touched it and degraded".
    """
    counter = {"calls": 0}
    real_client = rates.httpx.Client

    def _counting_client(*args, **kwargs):
        counter["calls"] += 1
        return real_client(*args, **kwargs)

    monkeypatch.setattr(rates, "_fetch_openrouter_models", _REAL_FETCH)
    monkeypatch.setattr(rates.httpx, "Client", _counting_client)
    return counter


def test_construction_on_an_openrouter_model_never_touches_the_network(workspace, monkeypatch, tmp_path):
    """The regression this fixes: constructing on an unmapped OpenRouter model
    used to fetch synchronously, blocking startup for up to 10s."""
    from raven.providers import model_catalog_cache

    monkeypatch.setattr(model_catalog_cache, "_CACHE_PATH", tmp_path / "model-catalog.json", raising=False)
    counter = _forbid_network_client(monkeypatch)

    provider = UsageProvider("openrouter/deepseek/deepseek-v4-pro", 0, 0)
    agent = _make_agent(workspace, provider, model="openrouter/deepseek/deepseek-v4-pro", window=None)

    assert agent.context_window_tokens == rates.DEFAULT_CONTEXT_WINDOW_TOKENS
    assert counter["calls"] == 0


def test_reading_the_window_for_an_openrouter_model_never_touches_the_network(workspace, monkeypatch, tmp_path):
    """The window is read inside a running turn, so resolving it must not freeze
    the event loop on a synchronous fetch."""
    from raven.providers import model_catalog_cache

    monkeypatch.setattr(model_catalog_cache, "_CACHE_PATH", tmp_path / "model-catalog.json", raising=False)

    provider = UsageProvider("stub", 0, 0)
    agent = _make_agent(workspace, provider, model="stub", window=None)

    counter = _forbid_network_client(monkeypatch)
    switched = ModelBinding(provider, "openrouter/deepseek/deepseek-v4-pro")
    with use_binding(switched):
        assert agent.context_window_tokens == rates.DEFAULT_CONTEXT_WINDOW_TOKENS
    assert counter["calls"] == 0
