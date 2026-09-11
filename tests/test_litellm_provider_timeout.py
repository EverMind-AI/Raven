"""Per-call timeout for `LiteLLMProvider` (issue #150).

Covers:
- chat() and chat_stream() forward `timeout` (= generation.timeout) to acompletion
- chat() wall-clock cap: a hung acompletion yields a structured error response
  classified as retryable `network` (so chat_with_retry retries / falls back)
- chat_stream() first-byte cap: the open and the first chunk share one
  `first_byte_timeout` deadline, tighter than the idle cap and far tighter than
  the call budget; a stall there raises FirstByteTimeoutError, which classifies
  retryable so the ladder asks again
- chat() narrows connect/write/pool to the first-byte budget while the read
  keeps the whole call's -- a non-streaming completion has no early byte to
  wait for, so only the phases before the model starts are separable
- chat_stream() per-chunk idle cap: a mid-stream stall raises TimeoutError
  after `stream_idle_timeout`, NOT after the whole-call `timeout` -- reusing
  the wide call budget as the idle line once let a dead stream hang an agent
  for 28 minutes (2026-09-01)
- a steady stream slower in total than `stream_idle_timeout` still completes
  (the idle timer resets on every chunk)

Mocks patch `raven.providers.litellm_provider.acompletion` (imported at module
top, so patching `litellm.acompletion` post-import would not be picked up).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from raven.contracts.llm_provider import ChatDelta, GenerationSettings
from raven.providers.first_byte import FirstByteTimeoutError
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


def _make_provider(
    timeout: float = 600.0,
    stream_idle_timeout: float = 180.0,
    first_byte_timeout: float = 120.0,
) -> LiteLLMProvider:
    provider = LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")
    provider.generation = GenerationSettings(
        timeout=timeout,
        stream_idle_timeout=stream_idle_timeout,
        first_byte_timeout=first_byte_timeout,
    )
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
    provider = _make_provider(timeout=123.0, first_byte_timeout=0)
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
    provider = _make_provider(timeout=77.0, first_byte_timeout=0)
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
    provider = _make_provider(timeout=0.05, stream_idle_timeout=999.0)
    resp = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o")
    assert resp.finish_reason == "error"
    assert resp.error_classification is not None
    assert resp.error_classification.category == "network"
    assert resp.error_classification.retryable is True


@pytest.mark.asyncio
async def test_chat_stream_idle_cap_raises_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that stalls after the first chunk trips the per-chunk idle cap
    at `stream_idle_timeout`, even while the whole-call `timeout` is still wide
    (the 2026-09-01 hang: a dead connection sat under the wide cap for 28 min)."""

    async def one_then_hang(**_kwargs: Any):
        async def gen():
            yield _chunk("a")
            await asyncio.sleep(10)
            yield _chunk("b")

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", one_then_hang)
    provider = _make_provider(timeout=999.0, stream_idle_timeout=0.05)
    seen: list[ChatDelta] = []
    with pytest.raises(TimeoutError):
        async for delta in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            seen.append(delta)
    assert [d.content for d in seen] == ["a"]


@pytest.mark.asyncio
async def test_chat_stream_open_hang_bounded_by_first_byte_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """An acompletion call that never even returns a stream is bounded by the
    first-byte budget, not the idle cap and not the call budget: a stream that
    never began is a different event from one that stopped mid-answer."""

    async def never_opens(**_kwargs: Any):
        await asyncio.sleep(10)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", never_opens)
    provider = _make_provider(timeout=999.0, stream_idle_timeout=999.0, first_byte_timeout=0.05)
    with pytest.raises(FirstByteTimeoutError) as caught:
        async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            pass
    assert caught.value.phase == "opening the stream"
    assert caught.value.budget == 0.05
    assert caught.value.waited >= 0.05
    assert "llmFirstByteTimeout" in str(caught.value)


@pytest.mark.asyncio
async def test_chat_stream_first_chunk_hang_bounded_by_first_byte_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway that opens the stream and then sends nothing is the same
    failure at the other await, and reports the phase it stalled in."""

    async def opens_then_silent(**_kwargs: Any):
        async def gen():
            await asyncio.sleep(10)
            yield _chunk("never")

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", opens_then_silent)
    provider = _make_provider(timeout=999.0, stream_idle_timeout=999.0, first_byte_timeout=0.05)
    with pytest.raises(FirstByteTimeoutError) as caught:
        async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            pass
    assert caught.value.phase == "waiting for the first chunk"


@pytest.mark.asyncio
async def test_first_byte_budget_is_one_deadline_over_open_and_first_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half the budget spent opening leaves half for the first chunk, not a
    fresh budget: a route that defers the request to the first pull would
    otherwise get to spend the bound twice on one stall."""

    async def slow_open_then_silent(**_kwargs: Any):
        await asyncio.sleep(0.15)

        async def gen():
            await asyncio.sleep(10)
            yield _chunk("never")

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", slow_open_then_silent)
    provider = _make_provider(timeout=999.0, stream_idle_timeout=999.0, first_byte_timeout=0.3)
    started = asyncio.get_running_loop().time()
    with pytest.raises(FirstByteTimeoutError) as caught:
        async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            pass
    elapsed = asyncio.get_running_loop().time() - started
    assert caught.value.phase == "waiting for the first chunk"
    assert elapsed < 0.6


@pytest.mark.asyncio
async def test_first_byte_timeout_classifies_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The verdict the ladder reads: retryable, so a stall is asked again rather
    than ending the run, and named so the record says which bound fired."""

    async def never_opens(**_kwargs: Any):
        await asyncio.sleep(10)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", never_opens)
    provider = _make_provider(timeout=999.0, first_byte_timeout=0.05)
    verdict = provider.classify_error(FirstByteTimeoutError(phase="opening the stream", budget=0.05, waited=0.06))
    assert verdict.category == "first_byte_timeout"
    assert verdict.retryable is True
    assert verdict.should_fallback is True


@pytest.mark.asyncio
async def test_chat_narrows_connect_but_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-streaming path has no first byte to wait for -- its body does not
    begin until the answer is finished -- so the first-byte budget goes on the
    phases before the model starts and the read keeps the whole call's."""
    import httpx

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _FakeResponse("hi")

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    provider = _make_provider(timeout=1800.0, first_byte_timeout=120.0)
    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o")
    sent = captured["timeout"]
    assert isinstance(sent, httpx.Timeout)
    assert sent.connect == 120.0
    assert sent.write == 120.0
    assert sent.pool == 120.0
    assert sent.read == 1800.0


@pytest.mark.asyncio
async def test_long_generation_survives_the_first_byte_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the first chunk has arrived the first-byte bound is spent: a stream
    that then takes many times that budget to finish still completes, because
    what governs from there is the idle cap per chunk and the call budget in
    total."""

    async def first_fast_then_long(**_kwargs: Any):
        async def gen():
            yield _chunk("a")
            for text in ["b", "c", "d", "e", "f"]:
                await asyncio.sleep(0.06)
                yield _chunk(text)

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", first_fast_then_long)
    provider = _make_provider(timeout=999.0, stream_idle_timeout=0.5, first_byte_timeout=0.05)
    seen: list[ChatDelta] = []
    async for delta in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        seen.append(delta)
    assert [d.content for d in seen] == ["a", "b", "c", "d", "e", "f"]


@pytest.mark.asyncio
async def test_chat_stream_steady_slow_stream_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chunks each arriving within the idle cap complete the stream even when
    the total run time exceeds `stream_idle_timeout` (the timer resets)."""

    async def steady(**_kwargs: Any):
        async def gen():
            for text in ["a", "b", "c", "d", "e"]:
                await asyncio.sleep(0.06)
                yield _chunk(text)

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", steady)
    provider = _make_provider(timeout=999.0, stream_idle_timeout=0.2)
    seen: list[ChatDelta] = []
    async for delta in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        seen.append(delta)
    assert [d.content for d in seen] == ["a", "b", "c", "d", "e"]
