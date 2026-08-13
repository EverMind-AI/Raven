"""Unit tests for the AgentLoop streaming wire.

Tests focus on the ``_llm_call_stream`` helper and the
``on_token_delta`` branch in ``_run_agent_loop``. Per the convention used by
``test_agent_loop_injected_skill_ids.py``, we avoid constructing a real
AgentLoop and instead bind the helper to a minimal stand-in.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from raven.agent.loop import AgentLoop
from raven.providers.base import ErrorClassification, LLMProvider, LLMResponse, StreamDelta
from raven.providers.rates import DEFAULT_MAX_OUTPUT_TOKENS


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
    """Bind ``_llm_call_stream`` to a SimpleNamespace stand-in for ``self``."""
    fake_self = SimpleNamespace(provider=provider)
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


# ---------------------------------------------------------------------------
# Truncation detection: three independent signals, OR'd
# ---------------------------------------------------------------------------


def _provider_with_ceiling(chunks: list[StreamDelta], max_tokens: int = 4096) -> _FakeProvider:
    """A provider whose configured ceiling the loop can compare usage against."""
    provider = _FakeProvider(chunks)
    provider.generation = SimpleNamespace(max_tokens=max_tokens)
    return provider


async def test_truncation_detected_from_upstream_finish_reason() -> None:
    """Signal 1: the backend says it stopped at the ceiling."""
    chunks = [StreamDelta(content="partial"), StreamDelta(content=None, finish_reason="length")]
    response = await _bind_helper(_provider_with_ceiling(chunks))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.truncated is True
    assert response.finish_reason == "length"


async def test_truncation_detected_from_output_tokens_alone() -> None:
    """Signal 2: usage reached the ceiling even though the backend says "stop".

    Some backends report a clean stop on a truncated reply, so trusting
    finish_reason alone would miss this entirely.
    """
    chunks = [
        StreamDelta(content="partial"),
        StreamDelta(content=None, usage={"completion_tokens": 4096}, finish_reason="stop"),
    ]
    response = await _bind_helper(_provider_with_ceiling(chunks, max_tokens=4096))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.truncated is True
    assert response.max_tokens == 4096


async def test_truncation_detected_from_incomplete_tool_arguments() -> None:
    """Signal 3: the tool call's JSON was cut mid-string.

    Needs no cooperation from the backend at all -- this one is computed from
    what actually arrived.
    """
    chunks = [
        StreamDelta(
            content=None,
            tool_call_delta={
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "write_file", "arguments": '{"content": "def foo('},
                    }
                ]
            },
        ),
        StreamDelta(content=None, finish_reason="tool_calls"),
    ]
    response = await _bind_helper(_provider_with_ceiling(chunks))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.truncated is True
    assert response.tool_calls[0].arguments["_raw_arguments"] == '{"content": "def foo('


async def test_complete_response_is_not_flagged_truncated() -> None:
    """None of the three signals present: a normal turn stays unflagged."""
    chunks = [
        StreamDelta(content="all done"),
        StreamDelta(content=None, usage={"completion_tokens": 12}, finish_reason="stop"),
    ]
    response = await _bind_helper(_provider_with_ceiling(chunks, max_tokens=4096))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.truncated is False
    assert response.finish_reason == "stop"


async def test_complete_tool_call_is_not_flagged_truncated() -> None:
    """Well-formed tool arguments must not read as truncation."""
    chunks = [
        StreamDelta(
            content=None,
            tool_call_delta={
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ]
            },
        ),
        StreamDelta(content=None, usage={"completion_tokens": 20}, finish_reason="tool_calls"),
    ]
    response = await _bind_helper(_provider_with_ceiling(chunks, max_tokens=4096))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.truncated is False
    assert response.tool_calls[0].arguments == {"path": "a.py"}


async def test_ceiling_check_compares_against_what_was_sent() -> None:
    """Signal 2 measures usage against the ceiling the request actually carried.

    Reading it from configuration while the provider resolved its own is how
    this check stops firing without ever failing: the two numbers drift and
    the comparison quietly stops matching. A provider with no ``generation``
    at all still has a resolvable ceiling -- the catalogue default -- so there
    is no "unknown ceiling" branch left to sit out.
    """
    provider = _FakeProvider(
        [
            StreamDelta(content="x", usage={"completion_tokens": DEFAULT_MAX_OUTPUT_TOKENS}),
            StreamDelta(content=None, finish_reason="stop"),
        ]
    )  # no .generation at all
    response = await _bind_helper(provider)(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="not/in-any-catalogue"
    )

    assert response.max_tokens == DEFAULT_MAX_OUTPUT_TOKENS
    assert response.truncated is True


async def test_output_below_the_resolved_ceiling_is_not_truncation() -> None:
    """The other side of the same comparison."""
    provider = _FakeProvider(
        [
            StreamDelta(content="x", usage={"completion_tokens": 12}),
            StreamDelta(content=None, finish_reason="stop"),
        ]
    )
    response = await _bind_helper(provider)(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="not/in-any-catalogue"
    )

    assert response.truncated is False


async def test_pinned_generation_ceiling_wins_over_the_catalogue() -> None:
    """An explicit number is a call-site decision, not a hint.

    This is the path a caller like the personalizer takes when it asks for a
    short answer -- the catalogue must not widen it back out.
    """
    provider = _provider_with_ceiling(
        [
            StreamDelta(content="x", usage={"completion_tokens": 120}),
            StreamDelta(content=None, finish_reason="stop"),
        ],
        max_tokens=120,
    )
    response = await _bind_helper(provider)(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="anthropic/claude-opus-4-5"
    )

    assert response.max_tokens == 120
    assert response.truncated is True


def _two_calls_last_one_cut() -> list[StreamDelta]:
    return [
        StreamDelta(
            content=None,
            tool_call_delta={
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    },
                    {
                        "index": 1,
                        "id": "call_2",
                        "function": {"name": "write_file", "arguments": '{"content": "def foo('},
                    },
                ]
            },
        ),
        StreamDelta(content=None, finish_reason="length"),
    ]


async def test_only_the_last_tool_call_is_marked_truncated() -> None:
    """Calls arrive in order, so everything before the last one finished intact.

    Marking all of them tells a model that a complete call was cut off; if that
    call is genuinely malformed it then gets the wrong diagnosis for a mistake
    it really did make.
    """
    response = await _bind_helper(_provider_with_ceiling(_two_calls_last_one_cut(), max_tokens=4096))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.truncated is True
    assert response.tool_calls[0].run_meta is None
    assert response.tool_calls[1].run_meta is not None
    assert response.tool_calls[1].run_meta.truncation.at_tokens == 4096


async def test_truncation_marker_never_reaches_the_assistant_message() -> None:
    """It is metadata about the call, not an argument the model wrote.

    ``to_openai_tool_call`` serializes ``arguments`` into the assistant message
    that goes back upstream next turn, and the loop does that before the
    registry ever sees the call. A marker living in that dict would therefore
    be echoed to the model as a field it never sent.
    """
    response = await _bind_helper(_provider_with_ceiling(_two_calls_last_one_cut(), max_tokens=4096))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    payload = json.dumps([tc.to_openai_tool_call() for tc in response.tool_calls])

    assert response.tool_calls[1].run_meta is not None
    assert "truncation" not in payload
    assert "run_meta" not in payload
    assert "_truncated" not in payload


async def test_a_pin_above_the_model_ceiling_is_bounded_by_it() -> None:
    """A pin is a call site's decision, but not a licence to exceed the model.

    Every pin in the tree today is far below any real ceiling, so this bound
    never fires -- which is why it has to be written down: the first pin that
    is not below it would be a rejected request, with nothing at the call site
    saying why.
    """
    provider = _provider_with_ceiling(
        [StreamDelta(content="x"), StreamDelta(content=None, finish_reason="stop")],
        max_tokens=10_000_000,
    )
    response = await _bind_helper(provider)(messages=[{"role": "user", "content": "hi"}], tools=None, model="gpt-4o")

    assert response.max_tokens == 16384  # gpt-4o's catalogue ceiling, not the pin
