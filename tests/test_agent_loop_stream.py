"""Unit tests for the AgentLoop streaming wire.

Tests focus on the ``_llm_call_stream`` helper and the
``on_token_delta`` branch in ``_run_agent_loop``. Per the convention used by
``test_agent_loop_injected_skill_ids.py``, we avoid constructing a real
AgentLoop and instead bind the helper to a minimal stand-in.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.base import ErrorClassification, LLMProvider, LLMResponse, StreamDelta


class _FakeProvider:
    """Provider stand-in exposing only ``chat_stream`` (and ``chat_with_retry`` unused).

    ``emits_unparsed_reasoning`` defaults to False, mirroring
    ``LLMProvider``'s own default: only a provider shaped like a parser-less
    self-hosted backend opts into the orphan-``</think>`` split.
    """

    def __init__(self, chunks: list[StreamDelta], emits_unparsed_reasoning: bool = False) -> None:
        self._chunks = chunks
        self.chat_stream_calls: list[dict[str, Any]] = []
        self._emits_unparsed_reasoning = emits_unparsed_reasoning

    async def chat_stream(self, **kwargs: Any):
        self.chat_stream_calls.append(kwargs)
        for chunk in self._chunks:
            yield chunk

    def emits_unparsed_reasoning(self) -> bool:
        return self._emits_unparsed_reasoning


def _bind_helper(provider: _FakeProvider):
    """Bind ``_llm_call_stream`` to a SimpleNamespace stand-in for ``self``.

    The stand-in carries every attribute the helper reads; the reconnect budget
    is taken from the real class so these tests assert the shipped behavior.
    """
    fake_self = SimpleNamespace(
        provider=provider,
        _MAX_STREAM_RECONNECTS=AgentLoop._MAX_STREAM_RECONNECTS,
    )
    return AgentLoop._llm_call_stream.__get__(fake_self)


# ---------------------------------------------------------------------------
# _llm_call_stream basic content accumulation
# ---------------------------------------------------------------------------


async def test_llm_call_stream_accumulates_content_and_triggers_callback() -> None:
    """Each non-empty content chunk triggers on_token_delta; final response
    has accumulated content."""
    chunks = [
        StreamDelta(content="Hello"),
        StreamDelta(content=" "),
        StreamDelta(content="world"),
        StreamDelta(content="!"),
    ]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)

    deltas_received: list[str] = []

    async def on_delta(text: str) -> None:
        deltas_received.append(text)

    response = await call(
        messages=[{"role": "user", "content": "say hi"}],
        tools=None,
        model="anthropic/claude-sonnet-4-6",
        on_token_delta=on_delta,
    )

    assert deltas_received == ["Hello", " ", "world", "!"]
    assert response.content == "Hello world!"
    assert response.finish_reason == "stop"
    assert response.tool_calls == []


async def test_llm_call_stream_skips_none_content_chunks() -> None:
    """Chunks with content=None do not fire the callback nor accumulate."""
    chunks = [
        StreamDelta(content="A"),
        StreamDelta(content=None, usage={"prompt_tokens": 5}),
        StreamDelta(content="B"),
        StreamDelta(content=None),
    ]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)

    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    response = await call(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="m",
        on_token_delta=on_delta,
    )

    assert deltas == ["A", "B"]
    assert response.content == "AB"


# ---------------------------------------------------------------------------
# Usage propagation
# ---------------------------------------------------------------------------


async def test_llm_call_stream_captures_final_usage() -> None:
    """The last non-None usage in the stream is preserved on the response."""
    chunks = [
        StreamDelta(content="x"),
        StreamDelta(
            content=None,
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        ),
    ]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(
        messages=[],
        tools=None,
        model="m",
        on_token_delta=on_delta,
    )

    assert response.usage["total_tokens"] == 15


# ---------------------------------------------------------------------------
# tool_call_delta accumulation (best-effort v0.1)
# ---------------------------------------------------------------------------


async def test_llm_call_stream_collects_tool_call_fragments() -> None:
    """Incremental tool_call_delta fragments accumulate into a final ToolCallRequest.

    v0.1 first-cut: handles the common case where one tool call is streamed
    with id + function.name on the first fragment and argument JSON suffix
    on later fragments. Multi-tool / out-of-order index merging is a v0.2 ask.
    """
    chunks = [
        StreamDelta(
            content=None,
            tool_call_delta={
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "function": {"name": "fs.read", "arguments": '{"path":'},
                    }
                ]
            },
        ),
        StreamDelta(
            content=None,
            tool_call_delta={
                "tool_calls": [
                    {
                        "id": None,
                        "function": {"name": None, "arguments": ' "/tmp/x"}'},
                    }
                ]
            },
        ),
    ]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(
        messages=[],
        tools=None,
        model="m",
        on_token_delta=on_delta,
    )

    assert response.has_tool_calls
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.name == "fs.read"
    assert tc.arguments == {"path": "/tmp/x"}
    assert response.finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# kwargs propagation to chat_stream
# ---------------------------------------------------------------------------


async def test_llm_call_stream_passes_messages_tools_model_to_provider() -> None:
    """on_token_delta path forwards messages / tools / model to provider.chat_stream."""
    chunks = [StreamDelta(content="ok")]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    msgs = [{"role": "user", "content": "ping"}]
    tools = [{"type": "function", "function": {"name": "noop"}}]
    await call(messages=msgs, tools=tools, model="m1", on_token_delta=on_delta)

    assert len(provider.chat_stream_calls) == 1
    call_kwargs = provider.chat_stream_calls[0]
    assert call_kwargs["messages"] == msgs
    assert call_kwargs["tools"] == tools
    assert call_kwargs["model"] == "m1"


# ---------------------------------------------------------------------------
# Default LLMResponse shape (no chunks)
# ---------------------------------------------------------------------------


async def test_llm_call_stream_timeout_returns_structured_error() -> None:
    """A mid-stream stall (TimeoutError from the per-chunk idle cap) terminates
    with a structured, retryable error response instead of propagating and
    crashing the turn. Already-streamed content is preserved on the response."""

    class _TimeoutStreamProvider:
        classify_error = LLMProvider.classify_error

        async def chat_stream(self, **_kwargs: Any):
            yield StreamDelta(content="partial")
            raise TimeoutError

    call = _bind_helper(_TimeoutStreamProvider())
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert response.finish_reason == "error"
    assert response.error_classification is not None
    assert response.error_classification.category == "network"
    assert response.error_classification.retryable is True
    assert response.content == "partial"
    assert seen == ["partial"]


class _ApiError(Exception):
    """Stand-in for ``litellm.APIError``: a 500 the classifier calls retryable."""

    status_code = 500


class _BadRequestError(Exception):
    """Stand-in for a 400: the classifier calls it fatal (no retry, no fallback)."""

    status_code = 400


async def test_llm_call_stream_error_delta_is_not_rendered_as_a_token() -> None:
    """A non-streaming provider's chat() error, replayed through the base
    fallback as a single terminal delta with finish_reason='error', must not
    be treated as ordinary streamed content: on_token_delta must not fire for
    it, and the final response must surface finish_reason + classification
    instead of a fabricated 'stop'/'tool_calls'."""
    classification = ErrorClassification(category="http_4xx", should_fallback=True)
    chunks = [
        StreamDelta(
            content="Azure OpenAI API Error 404: deployment not found",
            finish_reason="error",
            error_classification=classification,
        ),
    ]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    response = await call(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="m",
        on_token_delta=on_delta,
    )

    assert seen == []
    assert response.content == "Azure OpenAI API Error 404: deployment not found"
    assert response.finish_reason == "error"
    assert response.error_classification is classification


async def test_llm_call_stream_does_not_reconnect_after_emitting_deltas() -> None:
    """Reconnecting a stream that already emitted deltas would duplicate them in
    the caller's UI, so a partially-streamed failure is not retried — it
    propagates, which is what makes the turn fail (N-TURNFAILED)."""

    class _FailAfterContent:
        classify_error = LLMProvider.classify_error

        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_kwargs: Any):
            self.calls += 1
            yield StreamDelta(content="partial")
            raise _ApiError("APIError: OpenrouterException - Server disconnected")

    provider = _FailAfterContent()
    call = _bind_helper(provider)
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    with pytest.raises(_ApiError):
        await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert provider.calls == 1  # retryable, but deltas already reached the caller
    assert seen == ["partial"]


async def test_llm_call_stream_reconnects_when_nothing_was_emitted() -> None:
    """A retryable failure before the first delta is safe to reconnect: no output
    reached the caller, so the second attempt is indistinguishable from the first."""

    class _FailFirstConnect:
        classify_error = LLMProvider.classify_error

        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_kwargs: Any):
            self.calls += 1
            if self.calls == 1:
                raise _ApiError("APIError: OpenrouterException - Server disconnected")
                yield  # pragma: no cover - makes this an async generator
            yield StreamDelta(content="recovered")

    provider = _FailFirstConnect()
    call = _bind_helper(provider)
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert provider.calls == 2
    assert response.finish_reason == "stop"
    assert response.content == "recovered"
    assert seen == ["recovered"]


async def test_llm_call_stream_does_not_reconnect_a_fatal_error() -> None:
    """A non-retryable failure (400) is raised at once — a reconnect would just
    reproduce it."""

    class _FailFatally:
        classify_error = LLMProvider.classify_error

        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_kwargs: Any):
            self.calls += 1
            raise _BadRequestError("invalid request")
            yield  # pragma: no cover - makes this an async generator

    provider = _FailFatally()
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    with pytest.raises(_BadRequestError):
        await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert provider.calls == 1


async def test_llm_call_stream_surfaces_errors_from_a_provider_without_a_classifier() -> None:
    """A duck-typed provider need not implement classify_error. Consulting it
    unguarded would replace the real failure with an AttributeError raised from
    inside the handler."""

    class _NoClassifier:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_kwargs: Any):
            self.calls += 1
            raise _ApiError("APIError: OpenrouterException - Server disconnected")
            yield  # pragma: no cover - makes this an async generator

    provider = _NoClassifier()
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    with pytest.raises(_ApiError):
        await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert provider.calls == 1  # unclassifiable -> fatal, no reconnect


async def test_llm_call_stream_timeout_does_not_reconnect() -> None:
    """The per-chunk idle cap already waited the full timeout, so a stalled stream
    ends the call rather than doubling the stall with a reconnect."""

    class _StallProvider:
        classify_error = LLMProvider.classify_error

        def __init__(self) -> None:
            self.calls = 0

        async def chat_stream(self, **_kwargs: Any):
            self.calls += 1
            raise TimeoutError
            yield  # pragma: no cover - makes this an async generator

    provider = _StallProvider()
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert provider.calls == 1
    assert response.finish_reason == "error"
    assert response.error_classification is not None
    assert response.error_classification.category == "network"


async def test_llm_call_stream_empty_stream_yields_empty_content() -> None:
    """Provider yields zero chunks → response.content == '' + finish_reason='stop'."""
    provider = _FakeProvider([])
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert isinstance(response, LLMResponse)
    assert response.content == ""
    assert response.tool_calls == []
    assert response.finish_reason == "stop"


# ---------------------------------------------------------------------------
# Orphan <think> recovery -- backend never emitted a structured
# reasoning delta, and the accumulated content carries a closing tag with no
# opener (the server's prompt template swallowed it). Only fires for a
# provider shaped like a parser-less self-hosted backend
# (``emits_unparsed_reasoning() == True``); a normal direct/gateway provider
# leaves a bare closing tag in its content alone (F12).
# ---------------------------------------------------------------------------


async def test_llm_call_stream_splits_orphan_think_from_content() -> None:
    chunks = [
        StreamDelta(content="raw reasoning"),
        StreamDelta(content="</think>\n"),
        StreamDelta(content="final answer"),
    ]
    provider = _FakeProvider(chunks, emits_unparsed_reasoning=True)
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert response.reasoning_content == "raw reasoning"
    assert response.content == "final answer"


async def test_llm_call_stream_leaves_orphan_think_alone_for_non_leaking_provider() -> None:
    """A provider not shaped like a parser-less self-hosted backend keeps a
    bare closing tag as ordinary content (F12 regression guard)."""
    chunks = [
        StreamDelta(content="discussing the "),
        StreamDelta(content="</think>"),
        StreamDelta(content=" tag in my answer"),
    ]
    provider = _FakeProvider(chunks, emits_unparsed_reasoning=False)
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert response.reasoning_content is None
    assert response.content == "discussing the </think> tag in my answer"


async def test_llm_call_stream_leaves_structured_reasoning_alone() -> None:
    """A non-empty structured reasoning_content stream wins outright; an
    orphan tag inside content (if any) is left untouched."""
    chunks = [
        StreamDelta(content=None, reasoning_content="thinking"),
        StreamDelta(content="visible</think> more text"),
    ]
    provider = _FakeProvider(chunks, emits_unparsed_reasoning=True)
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert response.reasoning_content == "thinking"
    assert response.content == "visible</think> more text"
