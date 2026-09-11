"""Every adapter that declares ``llmFirstByteTimeout`` actually holds to it.

Reviewer, 2026-09-10: the setting reached every provider the factories build
(pinned in test_providers_factory) and was then implemented by one adapter of
four. ``AnthropicMessagesProvider`` -- the native Messages transport that
``anthropic/claude-opus-5`` routes onto, so what ``raven-code`` and
``raven-oncall`` run every turn -- built its client with the plain call budget
on all four httpx phases and waited for its first SSE event on the idle cap. So
a silent direct-Anthropic connection could hold a turn for ``llmCallTimeout``
while the declared 120-second ceiling never fired, and the loop's own
``FirstCallGuard`` ends the moment the provider is entered, so it does not
cover this either.

These ask the question the factory tests cannot: not whether the field arrived,
but what each adapter's open and first-event awaits were handed.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from raven.contracts.llm_provider import GenerationSettings
from raven.providers.first_byte import FirstByteTimeoutError


class _NeverOpens:
    """A client whose stream open never returns: the phase httpx left on read."""

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")
        _NeverOpens.seen_timeout = self.timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        return self

    async def aclose(self):
        return None


class _SilentStream:
    """A 200 whose body never produces a line."""

    status_code = 200

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        return self

    def aiter_lines(self):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(30)
        raise AssertionError("unreachable")  # pragma: no cover


def _anthropic(first_byte: float):
    from raven.providers import anthropic_messages_provider as mod

    provider = mod.AnthropicMessagesProvider(api_key="k", default_model="claude-opus-5")
    provider.generation = GenerationSettings(timeout=1800.0, stream_idle_timeout=180.0, first_byte_timeout=first_byte)
    return mod, provider


@pytest.mark.asyncio
async def test_the_anthropic_stream_open_is_bounded_by_the_first_byte_budget(monkeypatch):
    mod, provider = _anthropic(0.05)

    class _OpenNeverReturns:
        async def __aenter__(self):
            await asyncio.sleep(30)
            raise AssertionError("unreachable")  # pragma: no cover

        async def __aexit__(self, *exc):
            return False

    class _Client(_NeverOpens):
        def stream(self, *args, **kwargs):
            return _OpenNeverReturns()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)

    deltas = [d async for d in provider.chat_stream([{"role": "user", "content": "hi"}])]

    assert len(deltas) == 1 and deltas[0].finish_reason == "error"
    assert "llmFirstByteTimeout=0.05s" in (deltas[0].content or "")
    assert deltas[0].error_classification is not None
    assert deltas[0].error_classification.category == "first_byte_timeout"
    assert deltas[0].error_classification.retryable, "so the loop's ladder asks again"


@pytest.mark.asyncio
async def test_the_anthropic_first_stream_event_is_bounded_by_the_first_byte_budget(monkeypatch):
    mod, provider = _anthropic(0.05)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _SilentStream)

    deltas = [d async for d in provider.chat_stream([{"role": "user", "content": "hi"}])]

    assert len(deltas) == 1 and deltas[0].finish_reason == "error"
    assert "llmFirstByteTimeout=0.05s" in (deltas[0].content or "")
    assert deltas[0].error_classification.category == "first_byte_timeout"


@pytest.mark.asyncio
async def test_a_silent_anthropic_stream_is_given_up_far_short_of_the_call_budget(monkeypatch):
    """The measurement the finding is about: 0.05s, not 1800s."""
    mod, provider = _anthropic(0.05)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _SilentStream)

    loop = asyncio.get_running_loop()
    started = loop.time()
    [d async for d in provider.chat_stream([{"role": "user", "content": "hi"}])]
    waited = loop.time() - started

    assert waited < 5.0, f"waited {waited:.1f}s against a 1800s call budget"


@pytest.mark.asyncio
async def test_the_anthropic_first_event_stays_on_the_idle_cap_when_the_bound_is_off(monkeypatch):
    """0 restores exactly what this adapter did before the bound existed.

    ``stream_first_byte_budget`` answers the idle cap when no first-byte bound
    is configured, so the first event and every event after it wait the same
    180 s the adapter always waited -- the bound is off, not narrowed.
    """
    mod, provider = _anthropic(0)
    seen: list[tuple[float, float]] = []

    async def fake_consume(response, timeout, *, first_byte=0.0):
        seen.append((timeout, first_byte))
        from raven.contracts.llm_provider import ChatDelta

        yield ChatDelta(content="ok", finish_reason="stop")

    class _Opens:
        status_code = 200

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *a, **k):
            return self

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Opens)
    monkeypatch.setattr(mod, "consume_message_stream", fake_consume)

    deltas = [d async for d in provider.chat_stream([{"role": "user", "content": "hi"}])]

    assert [d.content for d in deltas] == ["ok"]
    assert seen == [(180.0, 180.0)], "both on the idle cap: the same wait as before the bound existed"


@pytest.mark.asyncio
async def test_the_anthropic_client_narrows_the_phases_before_the_generation(monkeypatch):
    """Not just the stream: a plain float put connect/write/pool on the call
    budget too, so a dead route cost 1800s on the non-streaming path."""
    mod, provider = _anthropic(120.0)
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, *a, **k):
            captured["timeout"] = k.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)

    await provider.chat([{"role": "user", "content": "hi"}])

    caps = captured["timeout"]
    assert isinstance(caps, httpx.Timeout)
    assert caps.connect == 120.0 and caps.write == 120.0 and caps.pool == 120.0
    assert caps.read == 1800.0, "the read is the generation itself and stays on the call budget"


@pytest.mark.asyncio
async def test_the_responses_first_event_is_bounded_by_the_first_byte_budget(monkeypatch):
    """The other direct adapter, which raven-research runs."""
    from raven.providers import openai_responses_provider as mod

    provider = mod.OpenAIResponsesProvider(api_key="k", default_model="gpt-5")
    provider.generation = GenerationSettings(timeout=1800.0, stream_idle_timeout=180.0, first_byte_timeout=0.05)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _SilentStream)

    out = await provider.chat([{"role": "user", "content": "hi"}])

    assert out.finish_reason == "error"
    assert "llmFirstByteTimeout=0.05s" in (out.content or "")


@pytest.mark.asyncio
async def test_the_codex_first_event_is_bounded_by_the_first_byte_budget(monkeypatch):
    """The third direct adapter, and the one whose chat-level wiring of this
    bound had no case: the reader it shares is asked directly below, which says
    nothing about what ``chat`` hands it."""
    from raven.providers import openai_codex_provider as mod

    provider = mod.OpenAICodexProvider(default_model="openai-codex/gpt-5")
    provider.generation = GenerationSettings(timeout=1800.0, stream_idle_timeout=180.0, first_byte_timeout=0.05)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _SilentStream)
    monkeypatch.setattr("raven.providers.chatgpt_token.access_token_and_account", lambda: ("tok", "acct"))

    out = await provider.chat([{"role": "user", "content": "hi"}])

    assert out.finish_reason == "error"
    assert "llmFirstByteTimeout=0.05s" in (out.content or "")


@pytest.mark.asyncio
async def test_the_shared_sse_reader_raises_the_named_error_on_a_silent_first_event():
    """The shape codex and responses share, asked directly."""
    from raven.providers.openai_codex_provider import _iter_sse

    with pytest.raises(FirstByteTimeoutError) as caught:
        async for _ in _iter_sse(_SilentStream(), 180.0, 0.05):
            raise AssertionError("unreachable")  # pragma: no cover

    assert caught.value.phase == "waiting for the first stream event"
    assert caught.value.budget == 0.05
