"""Base ``LLMProvider.chat_stream`` non-streaming fallback.

Providers that implement only non-streaming ``chat`` (azure / codex, and any
future bespoke provider) must still work in the TUI streaming path, which calls
``chat_stream``. The base default wraps ``chat`` into a single terminal delta.
"""

from __future__ import annotations

import json
from typing import Any

from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest


class _ChatOnlyProvider(LLMProvider):
    """A provider that implements only ``chat`` (no real streaming)."""

    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append({"max_tokens": max_tokens, "temperature": temperature, "reasoning_effort": reasoning_effort})
        return self._response

    def get_default_model(self) -> str:
        return "fake"


async def test_fallback_yields_single_terminal_delta() -> None:
    provider = _ChatOnlyProvider(LLMResponse(content="hello", usage={"total_tokens": 5}, reasoning_content="why"))
    deltas = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert len(deltas) == 1
    assert deltas[0].content == "hello"
    assert deltas[0].usage == {"total_tokens": 5}
    assert deltas[0].reasoning_content == "why"
    assert deltas[0].tool_call_delta is None


async def test_fallback_encodes_tool_calls_for_reconstruction() -> None:
    provider = _ChatOnlyProvider(
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(id="call_1", name="search", arguments={"q": "x"})],
        )
    )
    deltas = [d async for d in provider.chat_stream(messages=[])]

    assert len(deltas) == 1
    tc = deltas[0].tool_call_delta["tool_calls"][0]
    assert tc["index"] == 0
    assert tc["id"] == "call_1"
    assert tc["function"]["name"] == "search"
    assert json.loads(tc["function"]["arguments"]) == {"q": "x"}


async def test_fallback_forwards_the_configured_generation_settings_to_chat() -> None:
    """With no explicit arguments the fallback hands ``chat`` the provider's
    configured settings, the same resolution ``chat_with_retry`` performs. It
    used to forward only ``max_tokens`` that way and hard-code the rest, so a
    chat-only provider on the streaming path ran with temperature 0.7 and no
    reasoning effort whatever the config said."""
    provider = _ChatOnlyProvider(LLMResponse(content="x"))
    provider.generation = GenerationSettings(max_tokens=77, temperature=0.2, reasoning_effort="max")

    _ = [d async for d in provider.chat_stream(messages=[])]

    assert provider.calls == [{"max_tokens": 77, "temperature": 0.2, "reasoning_effort": "max"}]


async def test_fallback_lets_explicit_arguments_win_over_the_configured_ones() -> None:
    provider = _ChatOnlyProvider(LLMResponse(content="x"))
    provider.generation = GenerationSettings(max_tokens=77, temperature=0.2, reasoning_effort="max")

    _ = [d async for d in provider.chat_stream(messages=[], max_tokens=5, temperature=0.9, reasoning_effort="low")]

    assert provider.calls == [{"max_tokens": 5, "temperature": 0.9, "reasoning_effort": "low"}]
