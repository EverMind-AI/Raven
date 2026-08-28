"""Unit tests for the AgentLoop streaming wire.

Tests focus on the ``_llm_call_stream`` helper and the
``on_token_delta`` branch in ``_run_agent_loop``. Per the convention used by
``test_agent_loop_injected_skill_ids.py``, we avoid constructing a real
AgentLoop and instead bind the helper to a minimal stand-in.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMResponse, StreamDelta


class _FakeProvider:
    """Provider stand-in exposing only ``chat_stream`` (and ``chat_with_retry`` unused)."""

    def __init__(self, chunks: list[StreamDelta]) -> None:
        self._chunks = chunks
        self.chat_stream_calls: list[dict[str, Any]] = []

    async def chat_stream(self, **kwargs: Any):
        self.chat_stream_calls.append(kwargs)
        for chunk in self._chunks:
            yield chunk


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
# Terminal-chunk finish_reason / error_classification / truncation
# ---------------------------------------------------------------------------


async def test_llm_call_stream_terminal_length_marks_truncated() -> None:
    """The terminal chunk's finish_reason='length' survives assembly and the
    response is judged truncated, same as the non-streaming path would."""
    chunks = [
        StreamDelta(content="partial answ"),
        StreamDelta(content=None, finish_reason="length"),
    ]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert response.finish_reason == "length"
    assert response.truncated is True


async def test_llm_call_stream_terminal_length_refuses_last_tool_call() -> None:
    """A turn cut at the ceiling puts its last streamed call in doubt: the
    truncation verdict lands on that call's run_meta so the registry refuses it."""
    chunks = [
        StreamDelta(
            content=None,
            tool_call_delta={
                "tool_calls": [{"id": "call_1", "function": {"name": "fs.write", "arguments": '{"path": "/tmp/x"'}}]
            },
        ),
        StreamDelta(content=None, finish_reason="length"),
    ]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert response.truncated is True
    tc = response.tool_calls[0]
    # Cut mid-arguments: the buffer did not parse strictly, so the repair is
    # recorded -- the one locally computable clue the call never finished.
    assert tc.run_meta is not None
    assert tc.run_meta.arguments_repaired is True
    assert tc.run_meta.truncation is not None


async def test_llm_call_stream_unparsed_last_call_marked_without_length() -> None:
    """A cut the upstream never admits to (finish_reason='stop' on a truncated
    response) still marks the unparsed last call as last-of-turn, not truncated.

    The reported 'stop' is normalized to 'tool_calls': gateways answer a
    tool-call stream with 'stop', so on a turn that carries calls the payload
    outranks that word -- only 'length' / 'content_filter' survive as-is."""
    chunks = [
        StreamDelta(
            content=None,
            tool_call_delta={
                "tool_calls": [{"id": "call_1", "function": {"name": "fs.write", "arguments": '{"path": "/tmp'}}]
            },
        ),
        StreamDelta(content=None, finish_reason="stop"),
    ]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert response.truncated is False
    assert response.finish_reason == "tool_calls"
    tc = response.tool_calls[0]
    assert tc.run_meta is not None
    assert tc.run_meta.arguments_repaired is True
    assert tc.run_meta.last_of_turn is True
    assert tc.run_meta.truncation is None


async def test_llm_call_stream_propagates_terminal_error() -> None:
    """The base-class fallback surfaces a failed chat() as one terminal delta;
    the assembly must not relabel it 'stop' and drop the classification."""
    from raven.providers.base import ErrorClassification

    classification = ErrorClassification("network", retryable=True, should_fallback=True)
    chunks = [
        StreamDelta(
            content="Error calling LLM (network): boom", finish_reason="error", error_classification=classification
        ),
    ]
    provider = _FakeProvider(chunks)
    call = _bind_helper(provider)

    async def on_delta(_text: str) -> None:
        return None

    response = await call(messages=[], tools=None, model="m", on_token_delta=on_delta)

    assert response.finish_reason == "error"
    assert response.error_classification is classification
    assert response.truncated is False
