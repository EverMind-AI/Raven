"""Base ``LLMProvider.chat_stream`` non-streaming fallback.

Providers that implement only non-streaming ``chat`` (azure / codex, and any
future bespoke provider) must still work in the TUI streaming path, which calls
``chat_stream``. The base default wraps ``chat`` into a single terminal delta.
"""

from __future__ import annotations

import json
from typing import Any

from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _ChatOnlyProvider(LLMProvider):
    """A provider that implements only ``chat`` (no real streaming)."""

    def __init__(self, response: LLMResponse) -> None:
        self._response = response

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
        return self._response

    def get_default_model(self) -> str:
        return "fake"


async def test_fallback_yields_single_terminal_delta() -> None:
    provider = _ChatOnlyProvider(
        LLMResponse(content="hello", usage={"total_tokens": 5}, reasoning_content="why")
    )
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
