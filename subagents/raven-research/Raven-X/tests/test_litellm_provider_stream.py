"""Streaming tests for `LiteLLMProvider.chat_stream`.

Covers:
- happy-path: chat_stream yields StreamDelta sequence matching mock chunks
- _normalize_stream_chunk default OpenAI shape extraction
- None-content chunks (e.g. final stop chunk) are skipped (return None → no yield)
- signature parity with chat() (messages/tools/model/max_tokens/temperature/
  reasoning_effort/tool_choice all accepted; stream=True forwarded to acompletion)

Mocks patch `raven.providers.litellm_provider.acompletion` because the
provider module imports `from litellm import acompletion` at top level, so
patching `litellm.acompletion` after import would not be picked up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from raven.providers.base import StreamDelta
from raven.providers.litellm_provider import LiteLLMProvider

# ---------- Test doubles modelling OpenAI ChatCompletionChunk shape ----------


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


async def _fake_stream(chunks: list[_FakeChunk]):
    """Async generator standing in for litellm's streamed response."""
    for ch in chunks:
        yield ch


def _make_provider() -> LiteLLMProvider:
    # api_key kept truthy so the kwargs path that forwards it is exercised,
    # but no real network is touched — acompletion is patched.
    return LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")


# ----------------------------- Tests ---------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_yields_stream_deltas_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """chat_stream yields StreamDelta sequence matching mock OpenAI-shape chunks."""
    chunks = [_chunk("Hel"), _chunk("lo"), _chunk(" world")]

    captured_kwargs: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured_kwargs.update(kwargs)
        return _fake_stream(chunks)

    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        fake_acompletion,
    )

    provider = _make_provider()
    out: list[StreamDelta] = []
    async for delta in provider.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/gpt-4o",
    ):
        out.append(delta)

    assert [d.content for d in out] == ["Hel", "lo", " world"]
    assert all(isinstance(d, StreamDelta) for d in out)
    # stream=True must be forwarded to LiteLLM
    assert captured_kwargs.get("stream") is True
    # Usage must be requested explicitly — OpenAI-compatible providers omit the
    # trailing usage chunk otherwise, leaving cost / context tracking at zero.
    assert captured_kwargs.get("stream_options") == {"include_usage": True}


def test_normalize_stream_chunk_openai_shape_default() -> None:
    """_normalize_stream_chunk default path extracts OpenAI-shape content."""
    provider = _make_provider()
    chunk = _chunk("token")
    delta = provider._normalize_stream_chunk(chunk)
    assert delta is not None
    assert delta.content == "token"
    assert delta.tool_call_delta is None
    assert delta.usage is None


def test_normalize_stream_chunk_returns_none_for_empty_payload() -> None:
    """Chunks with no payload at all return None — chat_stream skips them."""
    provider = _make_provider()
    chunk = _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason=None)])
    assert provider._normalize_stream_chunk(chunk) is None


def test_normalize_stream_chunk_carries_the_stop_marker() -> None:
    """dr@3.4. A pure stop-marker chunk is a payload, not noise.

    The provider's finish_reason rides on exactly this chunk, and it is the
    only source for "length" / "content_filter" - dropping it forced the
    stream consumer to fabricate ``finish_reason`` from the presence of tool
    calls, which is why the dr@2.9 truncation fallback could never fire on the
    streaming path.
    """
    provider = _make_provider()
    chunk = _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason="length")])
    delta = provider._normalize_stream_chunk(chunk)
    assert delta is not None
    assert delta.finish_reason == "length"
    assert delta.content is None and delta.tool_call_delta is None and delta.usage is None


@pytest.mark.asyncio
async def test_chat_stream_skips_none_content_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mixed sequence with a None-content chunk: normalizer returns None → no yield."""
    chunks = [
        _chunk("a"),
        _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason=None)]),
        _chunk("b"),
    ]

    async def fake_acompletion(**_kwargs: Any):
        return _fake_stream(chunks)

    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        fake_acompletion,
    )

    provider = _make_provider()
    out = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert [d.content for d in out] == ["a", "b"]


@pytest.mark.asyncio
async def test_chat_stream_signature_parity_with_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """chat_stream accepts every chat() kwarg without raising.

    Smoke check: pass the full chat() parameter set and verify kwargs hit
    acompletion (stream=True, model/messages/tools/tool_choice present;
    reasoning_effort forwarded; max_tokens/temperature forwarded).
    """
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _fake_stream([_chunk("ok")])

    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        fake_acompletion,
    )

    provider = _make_provider()
    tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
    out: list[StreamDelta] = []
    async for delta in provider.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="openai/gpt-4o-mini",
        max_tokens=128,
        temperature=0.3,
        reasoning_effort="medium",
        tool_choice="auto",
    ):
        out.append(delta)

    assert [d.content for d in out] == ["ok"]
    assert captured["stream"] is True
    assert captured["max_tokens"] == 128
    assert captured["temperature"] == 0.3
    assert captured["reasoning_effort"] == "medium"
    assert captured["tool_choice"] == "auto"
    assert captured["tools"] == tools
    # model should be resolved (openai/gpt-4o-mini already has prefix → stays the same)
    assert "gpt-4o-mini" in captured["model"]


@pytest.mark.asyncio
async def test_chat_stream_forwards_request_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streams must carry the per-attempt timeout too — without it a stalled
    connection inherits LiteLLM's 6000s default and hangs the whole turn
    (observed as batch runs dying with zero tool calls on a congested
    endpoint). For streams the client timeout bounds the inter-chunk gap, so
    healthy long generations are unaffected."""
    from raven.providers.base import GenerationSettings

    captured_kwargs: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured_kwargs.update(kwargs)
        return _fake_stream([_chunk("hi")])

    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        fake_acompletion,
    )

    provider = _make_provider()
    provider.generation = GenerationSettings(timeout=240.0)
    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o"):
        pass

    assert captured_kwargs.get("timeout") == 240.0


def test_normalize_stream_chunk_keeps_the_empty_choices_usage_tail() -> None:
    """dr@3.4. ``stream_options.include_usage`` sends usage on a chunk with
    EMPTY choices - the shape the early ``if not choices`` return was throwing
    away, so a streamed turn's only usage record never reached the consumer."""
    provider = _make_provider()

    @dataclass
    class _FakeUsage:
        prompt_tokens: int = 11
        completion_tokens: int = 7
        total_tokens: int = 18

    delta = provider._normalize_stream_chunk(_FakeChunk(choices=[], usage=_FakeUsage()))
    assert delta is not None
    assert delta.usage == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert delta.content is None and delta.tool_call_delta is None

    # Empty choices AND no usage is still noise.
    assert provider._normalize_stream_chunk(_FakeChunk(choices=[], usage=None)) is None


@pytest.mark.asyncio
async def test_chat_stream_bounds_pin_by_model_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pin above the model's own output ceiling is clamped to it, exactly as
    the non-streaming path clamps via send_max_tokens -- unbounded, the same
    pin built two different requests and only the streaming one was rejected."""
    from raven.providers import rates

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _fake_stream([_chunk("ok")])

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    monkeypatch.setattr(rates, "resolve_max_output_tokens", lambda model, **_: 64)

    provider = _make_provider()
    async for _ in provider.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/gpt-4o",
        max_tokens=128,
    ):
        pass

    assert captured["max_tokens"] == 64
