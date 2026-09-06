"""reasoning_content stream emit.

deepseek-v4-pro / qwen / o-series stream ``reasoning_content`` (thinking)
before ``content``. The streaming path (``_llm_call_stream``) used to drop it
entirely while the non-streaming ``chat_with_retry`` populated it — a
streaming/non-streaming parity gap leaving the TUI silent during thinking.

The backend emits the existing ``thinking.delta`` RPC event (zero schema
change); the dormant ui-tui ``Thinking`` render path consumes it via
``chatStream.ts:98``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from raven.agent.loop import AgentLoop
from raven.providers.base import StreamDelta
from raven.providers.litellm_provider import LiteLLMProvider


class _FakeProvider:
    """Provider stand-in exposing ``chat_stream`` only."""

    def __init__(self, chunks: list[StreamDelta]) -> None:
        self._chunks = chunks
        self.chat_stream_calls: list[dict[str, Any]] = []

    async def chat_stream(self, **kwargs: Any):
        self.chat_stream_calls.append(kwargs)
        for chunk in self._chunks:
            yield chunk


def _bind_helper(provider: _FakeProvider):
    fake_self = SimpleNamespace(provider=provider)
    return AgentLoop._llm_call_stream.__get__(fake_self)


# ---------------------------------------------------------------------------
# REQ-10: _llm_call_stream consumes reasoning_content, fires callback,
#         sets it on the final LLMResponse (parity with chat_with_retry).
# ---------------------------------------------------------------------------


async def test_reasoning_delta_fires_callback_and_accumulates() -> None:
    chunks = [
        StreamDelta(content=None, reasoning_content="The user "),
        StreamDelta(content=None, reasoning_content="wants me to "),
        StreamDelta(content=None, reasoning_content="check cron."),
        StreamDelta(content="Sure, "),
        StreamDelta(content="checking."),
    ]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)

    reasoning_received: list[str] = []
    content_received: list[str] = []

    async def on_reasoning(text: str) -> None:
        reasoning_received.append(text)

    async def on_token(text: str) -> None:
        content_received.append(text)

    response = await call(
        messages=[{"role": "user", "content": "check cron"}],
        tools=None,
        model="deepseek/deepseek-v4-pro",
        on_token_delta=on_token,
        on_reasoning_delta=on_reasoning,
    )

    assert reasoning_received == ["The user ", "wants me to ", "check cron."]
    assert content_received == ["Sure, ", "checking."]
    assert response.content == "Sure, checking."
    # Parity with non-streaming chat_with_retry path.
    assert response.reasoning_content == "The user wants me to check cron."


async def test_reasoning_callback_optional() -> None:
    """No on_reasoning_delta wired → reasoning still accumulates on response,
    no crash (CLI / cron / sentinel callers pass nothing)."""
    chunks = [
        StreamDelta(content=None, reasoning_content="hmm"),
        StreamDelta(content="answer"),
    ]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)

    response = await call(
        messages=[{"role": "user", "content": "x"}],
        tools=None,
        model="deepseek/deepseek-v4-pro",
        on_token_delta=None,
    )
    assert response.reasoning_content == "hmm"
    assert response.content == "answer"


async def test_no_reasoning_leaves_response_reasoning_none() -> None:
    """Plain models that never stream reasoning_content → reasoning_content
    stays None (not empty string)."""
    chunks = [StreamDelta(content="hi")]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)
    response = await call(
        messages=[{"role": "user", "content": "x"}],
        tools=None,
        model="anthropic/claude-sonnet-4-6",
        on_token_delta=None,
    )
    assert response.reasoning_content is None


# ---------------------------------------------------------------------------
# REQ-9: _normalize_stream_chunk extracts reasoning_content and does NOT drop
#        a reasoning-only chunk as empty.
# ---------------------------------------------------------------------------


def _make_chunk(*, content=None, reasoning=None):
    delta = SimpleNamespace(content=content, tool_calls=None)
    if reasoning is not None:
        delta.reasoning_content = reasoning
    choice = SimpleNamespace(delta=delta)
    return SimpleNamespace(choices=[choice], usage=None)


def test_normalize_extracts_reasoning_only_chunk() -> None:
    provider = LiteLLMProvider.__new__(LiteLLMProvider)
    chunk = _make_chunk(content=None, reasoning="thinking...")
    delta = provider._normalize_stream_chunk(chunk)
    assert delta is not None, "reasoning-only chunk must not be dropped"
    assert delta.reasoning_content == "thinking..."
    assert delta.content is None


def test_normalize_content_chunk_no_reasoning() -> None:
    provider = LiteLLMProvider.__new__(LiteLLMProvider)
    chunk = _make_chunk(content="hello", reasoning=None)
    delta = provider._normalize_stream_chunk(chunk)
    assert delta is not None
    assert delta.content == "hello"
    assert delta.reasoning_content is None
