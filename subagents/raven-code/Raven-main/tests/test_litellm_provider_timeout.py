"""Per-call timeout for `LiteLLMProvider` (issue #150).

Covers:
- chat() and chat_stream() forward `timeout` (= generation.timeout) to acompletion
- chat() wall-clock cap: a hung acompletion yields a structured error response
  classified as retryable `network` (so chat_with_retry retries / falls back)
- chat_stream() per-chunk idle cap: a mid-stream stall raises TimeoutError
- chat_stream() splits the two questions a single timeout used to answer:
  `stream_idle_timeout` decides "is it dead" (no bytes for that long) and
  `timeout` decides "is it still worth waiting" (whole-request budget)

Mocks patch `raven.providers.litellm_provider.acompletion` (imported at module
top, so patching `litellm.acompletion` post-import would not be picked up).
"""

from __future__ import annotations

import asyncio
import time
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


def _make_idle_provider(timeout: float, idle: float) -> LiteLLMProvider:
    provider = LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")
    provider.generation = GenerationSettings(timeout=timeout, stream_idle_timeout=idle)
    return provider


@pytest.mark.asyncio
async def test_stream_stall_is_bounded_by_idle_not_by_the_whole_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that dies mid-generation must not hold the whole budget.

    Live incident (2026-09-01, dispatched autoresearch run): the request budget
    was 1800s and the only stall detector was that same 1800s, so a dead stream
    cost half an hour before anyone heard about it.
    """

    async def two_then_hang(**_kwargs: Any):
        async def gen():
            yield _chunk("a")
            yield _chunk("b")
            await asyncio.sleep(10)
            yield _chunk("c")

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", two_then_hang)
    provider = _make_idle_provider(timeout=10.0, idle=0.05)
    seen: list[StreamDelta] = []
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        async for delta in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            seen.append(delta)
    assert [d.content for d in seen] == ["a", "b"]
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_a_stream_that_never_opens_is_bounded_by_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero bytes ever is the same failure as zero bytes after the first chunk."""

    async def never_opens(**_kwargs: Any):
        await asyncio.sleep(10)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", never_opens)
    provider = _make_idle_provider(timeout=10.0, idle=0.05)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            pass
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_a_slow_but_progressing_stream_is_not_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The idle timer resets on every chunk, so slow is not dead."""

    async def slow(**_kwargs: Any):
        async def gen():
            for text in ("a", "b", "c"):
                await asyncio.sleep(0.02)
                yield _chunk(text)

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", slow)
    provider = _make_idle_provider(timeout=10.0, idle=0.5)
    seen = [d.content async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]
    assert seen == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_idle_timeout_zero_falls_back_to_the_whole_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 disables the idle cap; the pre-existing behavior is what is left."""

    async def one_then_hang(**_kwargs: Any):
        async def gen():
            yield _chunk("a")
            await asyncio.sleep(10)
            yield _chunk("b")

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", one_then_hang)
    provider = _make_idle_provider(timeout=0.05, idle=0.0)
    seen: list[StreamDelta] = []
    with pytest.raises(TimeoutError):
        async for delta in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            seen.append(delta)
    assert [d.content for d in seen] == ["a"]


@pytest.mark.asyncio
async def test_a_stream_that_trickles_forever_still_hits_the_whole_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idle cap alone cannot bound a backend that emits one token forever."""

    async def trickle(**_kwargs: Any):
        async def gen():
            while True:
                await asyncio.sleep(0.01)
                yield _chunk(".")

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", trickle)
    provider = _make_idle_provider(timeout=0.3, idle=5.0)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            if time.monotonic() - started > 3.0:
                pytest.fail("the whole-request budget never fired")
    assert time.monotonic() - started < 3.0
