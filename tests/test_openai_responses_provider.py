"""The Responses adapter streams its SSE body instead of replaying one reply.

DeepSeek's Responses route goes through ``OpenAIResponsesProvider``, which had
no ``chat_stream``: the base fallback consumed the whole SSE body and handed the
turn one terminal delta, so a chat answer appeared all at once when the call
ended. These pin the real streaming path -- one ``ChatDelta`` per
``response.output_text.delta`` -- without a live endpoint.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from raven.providers import openai_responses_provider as mod
from raven.providers.base import GenerationSettings


class _SSEResponse:
    """An httpx response stand-in that answers with a fixed SSE body."""

    status_code = 200

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def aiter_lines(self):
        for event in self._events:
            yield f"data: {json.dumps(event)}"
            yield ""

    async def aread(self) -> bytes:
        return b""


class _StreamContext:
    def __init__(self, response: _SSEResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _SSEResponse:
        return self._response

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _client_for(events: list[dict[str, Any]]):
    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        def stream(self, *args: object, **kwargs: object) -> _StreamContext:
            return _StreamContext(_SSEResponse(events))

    return _Client


@pytest.mark.asyncio
async def test_output_text_deltas_are_yielded_as_they_arrive(monkeypatch):
    events = [
        {"type": "response.created"},
        {"type": "response.output_text.delta", "delta": "Hello"},
        {"type": "response.output_text.delta", "delta": ", "},
        {"type": "response.output_text.delta", "delta": "world"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            },
        },
    ]
    monkeypatch.setattr(mod.httpx, "AsyncClient", _client_for(events))
    provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")

    deltas = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert [d.content for d in deltas if d.content] == ["Hello", ", ", "world"]
    assert deltas[-1].finish_reason == "stop"
    assert deltas[-1].usage["total_tokens"] == 7


@pytest.mark.asyncio
async def test_tool_calls_ride_the_terminal_delta(monkeypatch):
    events = [
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "id": "fc_1",
                "name": "search",
                "arguments": "",
            },
        },
        {"type": "response.function_call_arguments.delta", "call_id": "call_1", "delta": '{"q"'},
        {"type": "response.function_call_arguments.delta", "call_id": "call_1", "delta": ': "x"}'},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "id": "fc_1",
                "name": "search",
                "arguments": '{"q": "x"}',
            },
        },
        {"type": "response.completed", "response": {"status": "completed"}},
    ]
    monkeypatch.setattr(mod.httpx, "AsyncClient", _client_for(events))
    provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")

    deltas = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert [d.content for d in deltas if d.content] == []
    call = deltas[-1].tool_call_delta["tool_calls"][0]
    assert call["index"] == 0
    assert call["function"]["name"] == "search"
    assert json.loads(call["function"]["arguments"]) == {"q": "x"}
    assert deltas[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_a_refusal_streams_as_the_reply_it_is(monkeypatch):
    events = [
        {"type": "response.refusal.delta", "delta": "I can't help"},
        {"type": "response.refusal.delta", "delta": " with that."},
        {"type": "response.completed", "response": {"status": "completed"}},
    ]
    monkeypatch.setattr(mod.httpx, "AsyncClient", _client_for(events))
    provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")

    deltas = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    shown = [d for d in deltas if d.content]
    assert "".join(d.content for d in shown) == "I can't help with that."
    assert all(d.finish_reason == "refusal" for d in shown)
    assert deltas[-1].finish_reason == "refusal"


@pytest.mark.asyncio
async def test_an_error_event_ends_the_stream_as_an_error_delta(monkeypatch):
    events = [
        {"type": "response.output_text.delta", "delta": "half"},
        {"type": "error", "code": "server_is_overloaded", "message": "try later"},
    ]
    monkeypatch.setattr(mod.httpx, "AsyncClient", _client_for(events))
    provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")

    deltas = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert deltas[0].content == "half"
    assert "server_is_overloaded" in (deltas[-1].content or "")
    assert deltas[-1].finish_reason == "error"
    assert deltas[-1].error_classification is not None


@pytest.mark.asyncio
async def test_a_stream_without_a_terminal_event_reads_unknown(monkeypatch):
    events = [{"type": "response.output_text.delta", "delta": "cut"}]
    monkeypatch.setattr(mod.httpx, "AsyncClient", _client_for(events))
    provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")

    deltas = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert deltas[0].content == "cut"
    assert deltas[-1].finish_reason == "unknown"


@pytest.mark.asyncio
async def test_the_stream_watchdog_uses_the_idle_and_first_byte_budgets(monkeypatch):
    seen: list[tuple[float, float]] = []

    async def fake_iter(response, timeout, first_byte=0.0):
        seen.append((timeout, first_byte))
        for event in ():
            yield event

    monkeypatch.setattr(mod.httpx, "AsyncClient", _client_for([]))
    monkeypatch.setattr(mod, "_iter_sse", fake_iter)
    provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")
    provider.generation = GenerationSettings(timeout=600, stream_idle_timeout=7, first_byte_timeout=3)

    deltas = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert seen == [(7, 3)], "the stream must read the idle budget and the first-byte budget it was given"
    assert deltas[-1].finish_reason == "unknown"
