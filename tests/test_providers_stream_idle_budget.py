"""The per-chunk watchdog of every SSE adapter runs on ``stream_idle_timeout``.

Reviewer, 2026-09-07: the setting reached every provider the factories build
(pinned in test_providers_factory) and was then ignored by two of the three
SSE adapters, which fed their watchdog ``self.generation.timeout``. With
llmCallTimeout 600 and streamIdleTimeout 7 the Codex adapter's watchdog saw
7 and the other two saw 600. The factory tests could not see that: they
asked whether the field arrived, not what the watchdog was handed. These ask
the second question, one adapter at a time.
"""

from __future__ import annotations

import pytest

from raven.contracts.llm_provider import ChatDelta, GenerationSettings


class _Response:
    status_code = 200


class _StreamContext:
    async def __aenter__(self):
        return _Response()

    async def __aexit__(self, *exc):
        return False


class _Client:
    """Stands in for httpx.AsyncClient: opens a "stream" that answers 200."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        return _StreamContext()


@pytest.mark.asyncio
async def test_the_responses_sse_watchdog_runs_on_the_stream_idle_budget(monkeypatch):
    from raven.providers import openai_responses_provider as mod

    seen: list[float] = []

    async def fake_consume(response, timeout, **kwargs):
        seen.append(timeout)
        return "ok", [], "stop"

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mod, "_consume_sse", fake_consume)
    provider = mod.OpenAIResponsesProvider(api_key="k", default_model="gpt-5")
    provider.generation = GenerationSettings(timeout=600, stream_idle_timeout=7)

    out = await provider.chat([{"role": "user", "content": "hi"}])

    assert out.content == "ok"
    assert seen == [7], "the watchdog was handed the call budget (600) before; it is the idle budget now"


@pytest.mark.asyncio
async def test_the_anthropic_sse_watchdog_runs_on_the_stream_idle_budget(monkeypatch):
    from raven.providers import anthropic_messages_provider as mod

    seen: list[float] = []

    async def fake_consume(response, timeout, **kwargs):
        seen.append(timeout)
        yield ChatDelta(content="ok", finish_reason="stop")

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mod, "consume_message_stream", fake_consume)
    provider = mod.AnthropicMessagesProvider(api_key="k", default_model="claude-opus-4-1")
    provider.generation = GenerationSettings(timeout=600, stream_idle_timeout=7)

    deltas = [d async for d in provider.chat_stream([{"role": "user", "content": "hi"}])]

    assert [d.content for d in deltas] == ["ok"]
    assert seen == [7], "the watchdog was handed the call budget (600) before; it is the idle budget now"


@pytest.mark.asyncio
async def test_a_generation_from_before_the_field_falls_back_to_the_call_budget(monkeypatch):
    """The getattr fallback: a settings object with no stream_idle_timeout
    (an older caller's, or a test double's) keeps the behaviour it had."""
    from raven.providers import openai_responses_provider as mod

    seen: list[float] = []

    async def fake_consume(response, timeout, **kwargs):
        seen.append(timeout)
        return "ok", [], "stop"

    class _OldSettings:
        timeout = 42
        temperature = 0.7
        max_tokens = None
        reasoning_effort = None

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mod, "_consume_sse", fake_consume)
    provider = mod.OpenAIResponsesProvider(api_key="k", default_model="gpt-5")
    provider.generation = _OldSettings()

    await provider.chat([{"role": "user", "content": "hi"}])

    assert seen == [42]
