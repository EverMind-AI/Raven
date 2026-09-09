"""Unit tests for the AgentLoop streaming wire.

Tests focus on the ``_llm_call_stream`` helper and the
``on_token_delta`` branch in ``_run_agent_loop``. Per the convention used by
``test_agent_loop_injected_skill_ids.py``, we avoid constructing a real
AgentLoop and instead bind the helper to a minimal stand-in.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.recovery import RecoveryLimits
from raven.providers.base import ChatDelta, ErrorClassification, LLMProvider, LLMResponse
from raven.providers.rates import DEFAULT_MAX_OUTPUT_TOKENS


class _FakeProvider:
    """Provider stand-in exposing only ``chat_stream`` (and ``chat_with_retry`` unused).

    ``emits_unparsed_reasoning`` defaults to False, mirroring
    ``LLMProvider``'s own default: only a provider shaped like a parser-less
    self-hosted backend opts into the orphan-``</think>`` split.
    """

    def __init__(self, chunks: list[ChatDelta], emits_unparsed_reasoning: bool = False) -> None:
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
        _recovery_limits=RecoveryLimits(),
    )
    return AgentLoop._llm_call_stream.__get__(fake_self)


# ---------------------------------------------------------------------------
# _llm_call_stream basic content accumulation
# ---------------------------------------------------------------------------


async def test_llm_call_stream_accumulates_content_and_triggers_callback() -> None:
    """Each non-empty content chunk triggers on_token_delta; final response
    has accumulated content."""
    chunks = [
        ChatDelta(content="Hello"),
        ChatDelta(content=" "),
        ChatDelta(content="world"),
        ChatDelta(content="!"),
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
    # No terminal finish_reason arrived, so none may be fabricated: "unknown"
    # keeps a died-mid-stream reply distinguishable from a completed one.
    assert response.finish_reason == "unknown"
    assert response.tool_calls == []


async def test_llm_call_stream_skips_none_content_chunks() -> None:
    """Chunks with content=None do not fire the callback nor accumulate."""
    chunks = [
        ChatDelta(content="A"),
        ChatDelta(content=None, usage={"prompt_tokens": 5}),
        ChatDelta(content="B"),
        ChatDelta(content=None),
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
        ChatDelta(content="x"),
        ChatDelta(
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
        ChatDelta(
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
        ChatDelta(
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
    # The fragments never carried a terminal finish_reason; the aggregate must
    # not claim "tool_calls" on the upstream's behalf.
    assert response.finish_reason == "unknown"


# ---------------------------------------------------------------------------
# kwargs propagation to chat_stream
# ---------------------------------------------------------------------------


async def test_llm_call_stream_passes_messages_tools_model_to_provider() -> None:
    """on_token_delta path forwards messages / tools / model to provider.chat_stream."""
    chunks = [ChatDelta(content="ok")]
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


async def test_llm_call_stream_timeout_after_output_fails_the_turn_unless_asked() -> None:
    """A mid-stream stall (TimeoutError from the per-chunk idle cap) after words were
    streamed follows the same rule as any other mid-stream failure: the turn fails
    (N-TURNFAILED) unless the caller asked to retry after output. Handed back as a
    retryable response instead, the loop's own ladder asked again and an
    interactive client received the words of two attempts. Asked for, the response
    is structured and retryable, with the streamed content preserved on it."""

    class _TimeoutStreamProvider:
        classify_error = LLMProvider.classify_error

        async def chat_stream(self, **_kwargs: Any):
            yield ChatDelta(content="partial")
            raise TimeoutError

    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    with pytest.raises(TimeoutError):
        await _bind_helper(_TimeoutStreamProvider())(messages=[], tools=None, model="m", on_token_delta=on_delta)
    assert seen == ["partial"]

    fake_self = SimpleNamespace(
        provider=_TimeoutStreamProvider(),
        _MAX_STREAM_RECONNECTS=AgentLoop._MAX_STREAM_RECONNECTS,
        _recovery_limits=RecoveryLimits(llm_retry_after_output=True),
    )
    response = await AgentLoop._llm_call_stream.__get__(fake_self)(
        messages=[], tools=None, model="m", on_token_delta=on_delta
    )

    assert response.finish_reason == "error"
    assert response.error_classification is not None
    assert response.error_classification.category == "network"
    assert response.error_classification.retryable is True
    assert response.content == "partial"


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
        ChatDelta(
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
            yield ChatDelta(content="partial")
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
            yield ChatDelta(content="recovered")

    provider = _FailFirstConnect()
    call = _bind_helper(provider)
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert provider.calls == 2
    # The reconnected stream carried no terminal reason either, and none is
    # invented now; what this states is that the reconnect happened.
    assert response.finish_reason == "unknown"
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
    """Provider yields zero chunks → response.content == '' + finish_reason='unknown'.

    A zero-chunk stream said nothing about why it ended; reporting "stop" would
    make it indistinguishable from a normal finish in recorded trajectories.
    """
    provider = _FakeProvider([])
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert isinstance(response, LLMResponse)
    assert response.content == ""
    assert response.tool_calls == []
    assert response.finish_reason == "unknown"


async def test_a_stream_cut_before_its_terminal_chunk_is_a_transport_failure() -> None:
    """Reasoning arrived, then the library made up a stop because the upstream closed
    the connection: nothing deliverable came, so the reply is a retryable network
    error for the loop's ladder, and the log says what was lost."""
    from loguru import logger

    provider = _FakeProvider(
        [
            ChatDelta(content=None, reasoning_content="working out the deck"),
            ChatDelta(content=None, finish_reason="stop", finish_synthesized=True),
        ]
    )
    call = _bind_helper(provider)
    logged: list[str] = []
    sink = logger.add(lambda m: logged.append(str(m)), level="WARNING", format="{message}")
    try:
        response = await call(messages=[], tools=None, model="m", on_token_delta=None)
    finally:
        logger.remove(sink)

    assert response.finish_reason == "error"
    assert response.error_classification is not None
    assert response.error_classification.category == "network"
    assert response.error_classification.retryable is True
    assert "cut off by the connection" in (response.content or "")
    assert response.tool_calls == []
    assert any("without the upstream's terminal chunk" in line and "20 chars of reasoning" in line for line in logged)


async def test_a_made_up_stop_after_content_still_delivers_the_content() -> None:
    """Content that did arrive is the reply; only the finish reason is left unknown,
    because the upstream never said why it ended."""
    provider = _FakeProvider(
        [
            ChatDelta(content="Hello"),
            ChatDelta(content=None, finish_reason="stop", finish_synthesized=True),
        ]
    )
    call = _bind_helper(provider)

    response = await call(messages=[], tools=None, model="m", on_token_delta=None)

    assert response.content == "Hello"
    assert response.finish_reason == "unknown"
    assert response.error_classification is None


async def test_a_stop_the_upstream_sent_with_no_content_stays_an_empty_reply() -> None:
    """The existing shape: an honest empty reply is still the loop's empty-response
    recovery to own, not a transport failure."""
    provider = _FakeProvider(
        [
            ChatDelta(content=None, reasoning_content="hmm"),
            ChatDelta(content=None, finish_reason="stop"),
        ]
    )
    call = _bind_helper(provider)

    response = await call(messages=[], tools=None, model="m", on_token_delta=None)

    assert response.finish_reason == "stop"
    assert response.content == ""
    assert response.error_classification is None


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
        ChatDelta(content="raw reasoning"),
        ChatDelta(content="</think>\n"),
        ChatDelta(content="final answer"),
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
        ChatDelta(content="discussing the "),
        ChatDelta(content="</think>"),
        ChatDelta(content=" tag in my answer"),
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
        ChatDelta(content=None, reasoning_content="thinking"),
        ChatDelta(content="visible</think> more text"),
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


def _provider_with_ceiling(chunks: list[ChatDelta], max_tokens: int = 4096) -> _FakeProvider:
    """A provider whose configured ceiling the loop can compare usage against."""
    provider = _FakeProvider(chunks)
    provider.generation = SimpleNamespace(max_tokens=max_tokens)
    return provider


async def test_truncation_detected_from_upstream_finish_reason() -> None:
    """Signal 1: the backend says it stopped at the ceiling."""
    chunks = [ChatDelta(content="partial"), ChatDelta(content=None, finish_reason="length")]
    response = await _bind_helper(_provider_with_ceiling(chunks))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.truncated is True
    assert response.finish_reason == "length"


async def test_unparseable_arguments_on_the_last_call_are_read_as_a_cut() -> None:
    """The backend claims a clean `tool_calls` stop and the arguments do not
    parse -- gpt-4o's measured shape on a ceiling hit, 4 of 4 probes.

    Generation is sequential, so nothing arrives after a cut: a repair on the
    last call of a turn is the turn ending mid-write. The reading is stated as
    likely rather than certain, since a model can also just write bad JSON, and
    the refusal does not depend on which it was.

    What this replaces is a comparison of usage against the ceiling, which
    needed the request and the check to agree on a number and a model id.
    """
    chunks = [
        ChatDelta(
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
        ChatDelta(content=None, finish_reason="tool_calls"),
    ]
    response = await _bind_helper(_provider_with_ceiling(chunks))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.truncated is False, "the turn-level verdict still belongs to the upstream"
    assert response.tool_calls[0].run_meta.arguments_repaired is True
    assert response.tool_calls[0].run_meta.last_of_turn is True, "the position is what makes it readable"
    assert response.tool_calls[0].run_meta.truncation is None, "a reading, not a recorded fact"
    assert response.tool_calls[0].arguments["_raw_arguments"] == '{"content": "def foo('


async def test_complete_response_is_not_flagged_truncated() -> None:
    """None of the three signals present: a normal turn stays unflagged."""
    chunks = [
        ChatDelta(content="all done"),
        ChatDelta(content=None, usage={"completion_tokens": 12}, finish_reason="stop"),
    ]
    response = await _bind_helper(_provider_with_ceiling(chunks, max_tokens=4096))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.truncated is False
    assert response.finish_reason == "stop"


async def test_complete_tool_call_is_not_flagged_truncated() -> None:
    """Well-formed tool arguments must not read as truncation."""
    chunks = [
        ChatDelta(
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
        ChatDelta(content=None, usage={"completion_tokens": 20}, finish_reason="tool_calls"),
    ]
    response = await _bind_helper(_provider_with_ceiling(chunks, max_tokens=4096))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.truncated is False
    assert response.tool_calls[0].arguments == {"path": "a.py"}


async def test_a_clean_stop_with_no_unparsed_call_is_not_truncation() -> None:
    """Usage no longer participates: a turn the upstream calls done, whose last
    call parses, is done -- whatever the token count says.

    The comparison that used to live here (usage against the resolved ceiling)
    required the check and the request to agree on one number and one model id.
    They drifted five times on this branch, and each time the check stopped
    firing without ever failing. No surveyed agent carries it.
    """
    provider = _FakeProvider(
        [
            ChatDelta(content="x", usage={"completion_tokens": DEFAULT_MAX_OUTPUT_TOKENS}),
            ChatDelta(content=None, finish_reason="stop"),
        ]
    )  # no .generation at all
    response = await _bind_helper(provider)(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="not/in-any-catalogue"
    )

    assert response.truncated is False
    assert response.max_tokens is None, "no ceiling is claimed when the loop sent none"


def _two_calls_last_one_cut() -> list[ChatDelta]:
    return [
        ChatDelta(
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
        ChatDelta(content=None, finish_reason="length"),
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
    assert response.tool_calls[1].run_meta.truncation is not None


async def test_truncation_marker_never_reaches_the_assistant_message() -> None:
    """It is metadata about the call, not an argument the model wrote.

    ``openai_tool_call`` serializes ``arguments`` into the assistant message
    that goes back upstream next turn, and the loop does that before the
    registry ever sees the call. A marker living in that dict would therefore
    be echoed to the model as a field it never sent.
    """
    response = await _bind_helper(_provider_with_ceiling(_two_calls_last_one_cut(), max_tokens=4096))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    from raven.providers.tool_calls import openai_tool_call

    payload = json.dumps([openai_tool_call(tc) for tc in response.tool_calls])

    assert response.tool_calls[1].run_meta is not None
    assert "truncation" not in payload
    assert "run_meta" not in payload
    assert "_truncated" not in payload


async def test_a_cut_inside_tool_arguments_still_marks_the_call() -> None:
    """The other order: text first, then the call, cut while writing it."""
    chunks = [
        ChatDelta(content="I will write the file now"),
        ChatDelta(
            content=None,
            tool_call_delta={
                "tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "write_file", "arguments": '{"path": "a.py"'}}
                ]
            },
        ),
        ChatDelta(content=None, finish_reason="length"),
    ]
    response = await _bind_helper(_provider_with_ceiling(chunks, max_tokens=4096))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.tool_calls[0].run_meta is not None


# ---------------------------------------------------------------------------
# reasoning_ms -- how long the call spent thinking
#
# Measured here because this is the only layer that watches the deltas arrive.
# A browser clock cannot stand in for it: it lives only as long as the page that
# saw the stream, so a reload or a session switch has nothing left to read.
# ---------------------------------------------------------------------------

_GAP_S = 0.12


class _PacedProvider:
    """Yields chunks, sleeping ``_GAP_S`` wherever the script says ``None``."""

    def __init__(self, script: list[ChatDelta | None]) -> None:
        self._script = script

    async def chat_stream(self, **kwargs: Any):
        for step in self._script:
            if step is None:
                await asyncio.sleep(_GAP_S)
                continue
            yield step

    def emits_unparsed_reasoning(self) -> bool:
        return False


async def test_the_thinking_clock_runs_to_the_first_answer_token() -> None:
    """It measures the thought, and stops when prose takes over -- the work that
    follows the thought is not thinking time."""
    response = await _bind_helper(
        _PacedProvider(
            [
                ChatDelta(content=None, reasoning_content="let me"),
                None,
                ChatDelta(content="Hello"),
                None,
                None,
                ChatDelta(content=" world"),
            ]
        )
    )(messages=[{"role": "user", "content": "hi"}], tools=None, model="m")

    assert response.reasoning_ms is not None
    assert response.reasoning_ms >= _GAP_S * 1000 / 2
    assert response.reasoning_ms < _GAP_S * 1000 * 2, "the clock kept running past the first token"


async def test_the_thinking_clock_stops_at_the_first_tool_call() -> None:
    """A call is the other thing that ends a thought."""
    response = await _bind_helper(
        _PacedProvider(
            [
                ChatDelta(content=None, reasoning_content="I need the file"),
                None,
                ChatDelta(
                    content=None,
                    tool_call_delta={
                        "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]
                    },
                ),
                None,
                None,
            ]
        )
    )(messages=[{"role": "user", "content": "hi"}], tools=None, model="m")

    assert response.reasoning_ms is not None
    assert _GAP_S * 1000 / 2 <= response.reasoning_ms < _GAP_S * 1000 * 2


async def test_a_call_that_never_thought_reports_no_thinking_time() -> None:
    """None is "not measured", which a reader must not draw as a zero."""
    response = await _bind_helper(_FakeProvider([ChatDelta(content="hi")]))(
        messages=[{"role": "user", "content": "hi"}], tools=None, model="m"
    )

    assert response.reasoning_ms is None


# ---------------------------------------------------------------------------
# the loop's own ladder, after the reconnects
# ---------------------------------------------------------------------------


def _bind_with_ladder(provider: Any, delays: tuple[float, ...]):
    """Bind the helper to a stand-in that also carries the turn's recovery limits."""
    fake_self = SimpleNamespace(
        provider=provider,
        _MAX_STREAM_RECONNECTS=AgentLoop._MAX_STREAM_RECONNECTS,
        _recovery_limits=RecoveryLimits(llm_error_retry_delays=delays),
    )
    return AgentLoop._llm_call_stream.__get__(fake_self)


class _FailsThenStreams:
    """Raises `failures` retryable errors before the first delta, then streams an answer."""

    classify_error = LLMProvider.classify_error

    def __init__(self, failures: int, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or _ApiError("APIError: OpenrouterException - Server disconnected")
        self.calls = 0

    async def chat_stream(self, **_kwargs: Any):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
            yield  # pragma: no cover - makes this an async generator
        yield ChatDelta(content="recovered")


async def test_llm_call_stream_waits_out_the_loops_ladder_before_giving_up() -> None:
    """The provider's reconnect is immediate and single; a gateway that serves error
    pages for a few minutes outlasts it. The loop's ladder is waited out next, and the
    turn goes on -- one measured build lost 62 minutes of work to a 40-second outage."""
    provider = _FailsThenStreams(failures=3)
    call = _bind_with_ladder(provider, delays=(0.0, 0.0))

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert provider.calls == 4, "one call, one reconnect, two waits, then the answer"
    assert response.content == "recovered"


async def test_llm_call_stream_raises_once_the_ladder_is_spent() -> None:
    """The ladder is the budget: past it the turn fails the way N-TURNFAILED asks."""
    provider = _FailsThenStreams(failures=10)
    call = _bind_with_ladder(provider, delays=(0.0,))

    async def on_delta(_text: str) -> None:
        return None

    with pytest.raises(_ApiError):
        await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert provider.calls == 3  # the call, the reconnect, the one wait


async def test_llm_call_stream_hands_an_image_refusal_back_as_an_error_response() -> None:
    """A picture the endpoint will not take is not waited on and not raised: the
    recovery is to take the picture out of the messages, which only the loop can do,
    so the verdict travels back as the error response its strip-and-retry acts on."""
    provider = _FailsThenStreams(
        failures=10, error=_BadRequestError("At most 0 image(s) may be provided in one prompt.")
    )
    call = _bind_with_ladder(provider, delays=(0.0, 0.0))

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert provider.calls == 1
    assert response.finish_reason == "error"
    assert response.error_classification is not None and response.error_classification.strip_images


class _DiesMidStream:
    """Streams a few words, then loses the connection; answers whole on the next call."""

    classify_error = LLMProvider.classify_error

    def __init__(self) -> None:
        self.calls = 0

    async def chat_stream(self, **_kwargs: Any):
        self.calls += 1
        if self.calls == 1:
            yield ChatDelta(content="half an ")
            raise _ApiError("APIError: OpenrouterException - Network connection lost.")
        yield ChatDelta(content="whole answer")


async def test_llm_call_stream_retries_after_output_only_when_asked() -> None:
    """A mid-stream failure after words were streamed fails the turn, as N-TURNFAILED
    asks -- a person watching would see the words twice. An unattended caller says so
    through the limits and gets the call asked again: one deck build had two hours
    behind it when a dropped connection ended the turn with nothing published."""
    from types import SimpleNamespace

    provider = _DiesMidStream()
    fake_self = SimpleNamespace(
        provider=provider,
        _MAX_STREAM_RECONNECTS=AgentLoop._MAX_STREAM_RECONNECTS,
        _recovery_limits=RecoveryLimits(llm_error_retry_delays=(0.0,), llm_retry_after_output=True),
    )
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    response = await AgentLoop._llm_call_stream.__get__(fake_self)(
        messages=[], tools=None, model="m", on_token_delta=on_delta
    )
    assert response.content == "whole answer", "the half is dropped, the retry's answer is the answer"
    assert seen == ["half an ", "whole answer"], "the caller saw both -- the price the caller agreed to"
    assert provider.calls == 2

    fresh = _DiesMidStream()
    fake_self = SimpleNamespace(
        provider=fresh,
        _MAX_STREAM_RECONNECTS=AgentLoop._MAX_STREAM_RECONNECTS,
        _recovery_limits=RecoveryLimits(llm_error_retry_delays=(0.0,)),
    )
    with pytest.raises(_ApiError):
        await AgentLoop._llm_call_stream.__get__(fake_self)(messages=[], tools=None, model="m", on_token_delta=on_delta)
    assert fresh.calls == 1, "off, the turn fails on the first mid-stream error"
