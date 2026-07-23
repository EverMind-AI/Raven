"""Per-call timeout for `LiteLLMProvider` (issue #150).

Covers:
- chat() and chat_stream() forward `timeout` (= generation.timeout) to acompletion
- chat() wall-clock cap: a hung acompletion yields a structured error response
  classified as retryable `network` (so chat_with_retry retries / falls back)
- chat_stream() per-chunk idle cap: a mid-stream stall raises TimeoutError

Mocks patch `raven.providers.litellm_provider.acompletion` (imported at module
top, so patching `litellm.acompletion` post-import would not be picked up).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from raven.providers.base import GenerationSettings, StreamDelta
from raven.providers.litellm_provider import LiteLLMProvider


@dataclass
class _FakeDelta:
    content: str | None = None
    tool_calls: list[Any] | None = None


@dataclass
class _FakeChoice:
    delta: _FakeDelta
    finish_reason: str | None = None
    index: int = 0


@dataclass
class _FakeChunk:
    choices: list[_FakeChoice]
    usage: Any | None = None


def _chunk(content: str | None) -> _FakeChunk:
    return _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=content))])


def _make_provider(timeout: float = 600.0) -> LiteLLMProvider:
    provider = LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")
    provider.generation = GenerationSettings(timeout=timeout)
    return provider


class _FakeResponse:
    """Non-streaming acompletion result with one text choice."""

    def __init__(self, text: str) -> None:
        self.choices = [_FakeChoice(delta=_FakeDelta(content=text), finish_reason="stop")]
        self.usage = None


@pytest.mark.asyncio
async def test_chat_forwards_generation_timeout_to_acompletion(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _FakeResponse("hi")

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    provider = _make_provider(timeout=123.0)
    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o")
    assert captured["timeout"] == 123.0


@pytest.mark.asyncio
async def test_chat_stream_forwards_generation_timeout_to_acompletion(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_stream(chunks):
        for ch in chunks:
            yield ch

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return fake_stream([_chunk("ok")])

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    provider = _make_provider(timeout=77.0)
    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        pass
    assert captured["timeout"] == 77.0


@pytest.mark.asyncio
async def test_chat_wall_clock_cap_returns_classified_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend that never responds is bounded by the wall-clock cap and the
    result is a retryable `network` error, not an indefinite hang."""

    async def hanging_acompletion(**_kwargs: Any):
        await asyncio.sleep(10)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", hanging_acompletion)
    provider = _make_provider(timeout=0.05)
    resp = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o")
    assert resp.finish_reason == "error"
    assert resp.error_classification is not None
    assert resp.error_classification.category == "network"
    assert resp.error_classification.retryable is True


@pytest.mark.asyncio
async def test_chat_stream_idle_cap_raises_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that stalls after the first chunk trips the per-chunk idle cap."""

    async def one_then_hang(**_kwargs: Any):
        async def gen():
            yield _chunk("a")
            await asyncio.sleep(10)
            yield _chunk("b")

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", one_then_hang)
    provider = _make_provider(timeout=0.05)
    seen: list[StreamDelta] = []
    with pytest.raises(TimeoutError):
        async for delta in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            seen.append(delta)
    assert [d.content for d in seen] == ["a"]
