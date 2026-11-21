"""Steady streaming responses must still obey the configured whole-call budget."""

import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace

import pytest

from raven.contracts.llm_provider import ChatDelta, GenerationSettings
from raven.providers.base import LLMProvider
from raven.providers.call_timeout import CallTimeoutError, bounded_stream, call_timeout
from raven.providers.relay_wire import decode_exception, encode_exception
from raven.providers.streaming import stream_llm_call


@pytest.fixture
def steady_provider(request, monkeypatch):
    kind = request.param
    state = SimpleNamespace(closed=False, produced=0, kind=kind)

    async def chunks():
        try:
            for _ in range(60):
                state.produced += 1
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="x", tool_calls=None), finish_reason=None)],
                    usage=None,
                )
                await asyncio.sleep(0.01)
        finally:
            state.closed = True

    class Response:
        status_code = 200

        async def aiter_lines(self):
            for _ in range(60):
                state.produced += 1
                event = (
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}}
                    if kind == "anthropic"
                    else {"type": "response.output_text.delta", "delta": "x"}
                )
                yield "data: " + json.dumps(event)
                yield ""
                await asyncio.sleep(0.01)
            final = (
                {"type": "message_stop"}
                if kind == "anthropic"
                else {"type": "response.completed", "response": {"status": "completed"}}
            )
            yield "data: " + json.dumps(final)
            yield ""

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            state.closed = True

        def stream(self, *args, **kwargs):
            return self

        def aiter_lines(self):
            return Response().aiter_lines()

        status_code = 200

    if kind == "litellm":
        from raven.providers import litellm_provider as mod

        async def complete(**kwargs):
            return chunks()

        monkeypatch.setattr(mod, "acompletion", complete)
        provider = mod.LiteLLMProvider(api_key="test", default_model="openai/gpt-4o")
    else:
        monkeypatch.setattr("httpx.AsyncClient", Client)
        if kind == "anthropic":
            from raven.providers.anthropic_messages_provider import AnthropicMessagesProvider

            provider = AnthropicMessagesProvider(api_key="test", default_model="claude-opus-4-1")
        elif kind == "responses":
            from raven.providers.openai_responses_provider import OpenAIResponsesProvider

            provider = OpenAIResponsesProvider(api_key="test", default_model="gpt-5")
        else:
            from raven.providers.openai_codex_provider import OpenAICodexProvider

            monkeypatch.setattr("raven.providers.chatgpt_token.access_token_and_account", lambda: ("test", "test"))
            monkeypatch.setattr("raven.providers.codex_client_version.resolve", lambda: "0.0.0")
            provider = OpenAICodexProvider(default_model="gpt-5")
    provider.generation = GenerationSettings(timeout=0.1, stream_idle_timeout=1, first_byte_timeout=1)
    return provider, state


@pytest.mark.asyncio
@pytest.mark.parametrize("steady_provider", ["litellm", "anthropic", "responses", "codex"], indirect=True)
async def test_steady_stream_cannot_outlive_call_budget(steady_provider):
    provider, state = steady_provider
    if state.kind in ("litellm", "anthropic"):
        with pytest.raises(TimeoutError, match="llmCallTimeout") as caught:
            async for _ in provider.chat_stream([{"role": "user", "content": "hi"}]):
                pass
        assert provider.classify_error(caught.value).category == "network"
    else:
        result = await provider.chat([{"role": "user", "content": "hi"}])
        assert result.finish_reason == "error"
        assert result.error_classification.category == "network"
        assert "llmCallTimeout" in result.content
    assert state.closed
    assert state.produced < 60


@pytest.mark.asyncio
@pytest.mark.parametrize("steady_provider", ["litellm", "anthropic"], indirect=True)
async def test_timeout_hands_back_partial_text_without_replaying(steady_provider):
    provider, state = steady_provider
    shown = []

    async def render(text):
        shown.append(text)

    result = await stream_llm_call(
        provider,
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model=None,
        on_token_delta=render,
        hand_back_partial=True,
    )
    assert result.finish_reason == "stream_cut"
    assert result.content == "".join(shown) == "x" * state.produced
    assert state.closed
    assert 0 < state.produced < 60


@pytest.mark.asyncio
@pytest.mark.parametrize("steady_provider", ["litellm", "anthropic"], indirect=True)
async def test_deadline_does_not_cancel_a_consumer_between_pulls(steady_provider):
    provider, state = steady_provider
    async with aclosing(provider.chat_stream([{"role": "user", "content": "hi"}])) as stream:
        assert (await anext(stream)).content == "x"
        await asyncio.sleep(0.15)
        with pytest.raises(CallTimeoutError):
            await anext(stream)
    assert state.closed
    assert state.produced == 1
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("steady_provider", ["litellm", "anthropic", "responses", "codex"], indirect=True)
async def test_external_cancellation_is_preserved_and_closes_the_stream(steady_provider):
    provider, state = steady_provider
    provider.generation = GenerationSettings(timeout=60)

    async def consume():
        async with aclosing(provider.chat_stream([{"role": "user", "content": "hi"}])) as stream:
            async for _ in stream:
                pass

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(5):
            while state.produced == 0:
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _FinishingStream:
    generation = GenerationSettings(timeout=0.05)

    def __init__(self, finish_reason: str = "stop"):
        self.finish_reason = finish_reason

    @bounded_stream
    async def chat_stream(self):
        yield ChatDelta(content="x")
        yield ChatDelta(content=None, finish_reason=self.finish_reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["stop", "tool_calls", "length", "refusal"])
async def test_delivered_final_delta_is_not_reported_as_a_timeout(finish_reason):
    """A pull after the terminal delta is cleanup, not a late model request."""
    stream = _FinishingStream(finish_reason).chat_stream()
    assert (await anext(stream)).content == "x"
    terminal = await anext(stream)
    assert terminal.finish_reason == finish_reason
    await asyncio.sleep(0.08)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await stream.aclose()


@pytest.mark.parametrize("budget", [429, 500, 600])
def test_timeout_budget_is_not_misread_as_an_http_status(budget):
    verdict = LLMProvider.classify_error(CallTimeoutError(budget))
    assert verdict.category == "network"
    assert verdict.retryable and verdict.should_fallback


@pytest.mark.parametrize("budget", [429, 500, 600])
def test_relayed_timeout_budget_is_not_misread_as_an_http_status(budget):
    """The relay rebuilds the failure as a carrier class, not CallTimeoutError."""
    carrier = decode_exception(encode_exception(CallTimeoutError(budget), status=None))
    assert not isinstance(carrier, CallTimeoutError)
    verdict = LLMProvider.classify_error(carrier)
    assert verdict.category == "network"
    assert verdict.retryable and verdict.should_fallback


@pytest.mark.asyncio
async def test_inner_timeout_keeps_its_own_identity():
    inner = TimeoutError("upstream-specific timeout")
    with pytest.raises(TimeoutError) as caught:
        async with call_timeout(60):
            raise inner
    assert caught.value is inner
