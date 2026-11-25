"""Steady streaming responses must still obey the configured whole-call budget."""

import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace

import pytest

from raven.contracts.llm_provider import ChatDelta, GenerationSettings
from raven.providers.base import LLMProvider
from raven.providers.call_timeout import CallTimeoutError, _tail_budget, bounded_stream, call_timeout
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


class _TrailingStream:
    """A settled reply whose iterator keeps handing deltas over.

    The inner generator is kept reachable from the instance so the only way it
    can be closed is the decorator's own ``aclosing``: were it collected instead,
    its ``finally`` would run on the way out and the assertion could not tell the
    two apart.
    """

    def __init__(self, trail: int):
        self.trail = trail
        self.closed = False
        self.inner = None
        self.generation = GenerationSettings(timeout=0.3, stream_idle_timeout=0.1)

    async def _deltas(self):
        try:
            yield ChatDelta(content="x", finish_reason="stop")
            for _ in range(self.trail):
                await asyncio.sleep(0.04)
                yield ChatDelta(content=None, usage={"prompt_tokens": 1})
        finally:
            self.closed = True

    @bounded_stream
    def chat_stream(self):
        self.inner = self._deltas()
        return self.inner


class _BufferedStream:
    """Deltas that need no await, so only the precheck can refuse one."""

    generation = GenerationSettings(timeout=0.05)

    @bounded_stream
    async def chat_stream(self):
        yield ChatDelta(content="a")
        yield ChatDelta(content="b")


class _BufferedTail:
    """A settled reply whose trailing deltas need no await either.

    After the terminal delta no suspension point follows, so ``asyncio.timeout_at``
    cannot fire before a buffered delta is handed over; the precheck at the top of
    the pull is the only guard that refuses one once the tail window is spent.
    """

    generation = GenerationSettings(timeout=0.3, stream_idle_timeout=0.1)

    @bounded_stream
    async def chat_stream(self):
        yield ChatDelta(content="x", finish_reason="stop")
        yield ChatDelta(content=None, usage={"prompt_tokens": 1})
        yield ChatDelta(content=None, usage={"prompt_tokens": 2})


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


@pytest.mark.asyncio
async def test_settled_stream_delivers_its_cleanup_pull():
    """The usage snapshot a provider sends after its terminal delta still arrives."""
    stream = _TrailingStream(trail=1).chat_stream()
    deltas = [delta async for delta in stream]
    assert [delta.content for delta in deltas] == ["x", None]
    assert deltas[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_settled_stream_stops_a_trailing_dribble():
    """A reply that keeps trickling after its terminal delta is still bounded."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    trailing = _TrailingStream(trail=50)
    stream = trailing.chat_stream()
    deltas = [delta async for delta in stream]
    elapsed = loop.time() - started

    assert deltas[0].finish_reason == "stop"
    # Unbounded, the 50 pulls would take two seconds, twenty times the idle
    # window this stream is configured with.
    assert len(deltas) < 10
    assert elapsed < 1.0
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.asyncio
async def test_settled_stream_closes_once_the_tail_window_has_gone():
    """The tail window opens once, so a consumer that then lingers gets the end.

    The pull that finds the window spent returns without resuming the inner
    generator, so ``aclosing`` is the only thing left that closes it. The inner
    generator is held from the instance to keep collection from closing it too,
    which would make this assertion pass for the wrong reason.
    """
    trailing = _TrailingStream(trail=50)
    stream = trailing.chat_stream()
    assert (await anext(stream)).finish_reason == "stop"
    assert (await anext(stream)).content is None
    await asyncio.sleep(0.15)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert trailing.closed
    await stream.aclose()


@pytest.mark.asyncio
async def test_settled_tail_refuses_a_buffered_delta_after_its_window():
    """Once the tail window is spent, a delta already in hand is refused, not delivered.

    The delta needs no await, so ``asyncio.timeout_at`` has no suspension point to
    fire at; the precheck is the only thing that can refuse it.
    """
    stream = _BufferedTail().chat_stream()
    assert (await anext(stream)).finish_reason == "stop"
    assert (await anext(stream)).usage == {"prompt_tokens": 1}
    await asyncio.sleep(0.15)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await stream.aclose()


def test_tail_budget_falls_back_to_the_call_budget_without_an_idle_setting():
    """A duck-typed generation without ``stream_idle_timeout`` still gets a bounded tail."""
    generation = SimpleNamespace(timeout=600.0)
    assert _tail_budget(generation, 600.0) == 600.0


@pytest.mark.asyncio
async def test_settled_stream_keeps_an_inner_timeout_identity():
    """The tail window is ours; a timeout the upstream raises is still its own."""
    inner = TimeoutError("upstream-specific timeout")

    class _RaisingTail:
        generation = GenerationSettings(timeout=0.3, stream_idle_timeout=0.1)

        @bounded_stream
        async def chat_stream(self):
            yield ChatDelta(content="x", finish_reason="stop")
            raise inner

    stream = _RaisingTail().chat_stream()
    assert (await anext(stream)).finish_reason == "stop"
    with pytest.raises(TimeoutError) as caught:
        await anext(stream)
    assert caught.value is inner
    await stream.aclose()


@pytest.mark.asyncio
async def test_budget_already_spent_refuses_a_buffered_delta():
    """A pull that needs no await is refused once the budget is gone."""
    stream = _BufferedStream().chat_stream()
    assert (await anext(stream)).content == "a"
    await asyncio.sleep(0.08)
    with pytest.raises(CallTimeoutError):
        await anext(stream)
    await stream.aclose()


@pytest.mark.parametrize("budget", [429, 500, 600])
def test_timeout_budget_is_not_misread_as_an_http_status(budget):
    verdict = LLMProvider.classify_error(CallTimeoutError(budget))
    assert verdict.category == "network"
    assert verdict.retryable and verdict.should_fallback


@pytest.mark.parametrize("budget", [429, 500, 600])
def test_relayed_timeout_budget_is_not_misread_as_an_http_status(budget):
    """A host from before the call-timeout kind is still read by its class name."""
    data = encode_exception(CallTimeoutError(budget), status=None)
    # What such a host sends: the opaque carrier, no call-timeout kind.
    data["kind"] = "timeout"
    data.pop("timeout", None)
    carrier = decode_exception(data)
    assert not isinstance(carrier, CallTimeoutError)
    verdict = LLMProvider.classify_error(carrier)
    assert verdict.category == "network"
    assert verdict.retryable and verdict.should_fallback


@pytest.mark.parametrize("budget", [429.0, 500.0, 600.0])
def test_relayed_timeout_is_rebuilt_as_its_own_class(budget):
    """A current host sends the call-timeout kind, so the class itself comes back."""
    data = encode_exception(CallTimeoutError(budget), status=None)
    assert data["kind"] == "call_timeout"
    rebuilt = decode_exception(data)
    assert isinstance(rebuilt, CallTimeoutError)
    assert rebuilt.budget == budget
    verdict = LLMProvider.classify_error(rebuilt)
    assert verdict.category == "network"
    assert verdict.retryable and verdict.should_fallback


@pytest.mark.asyncio
async def test_inner_timeout_keeps_its_own_identity():
    inner = TimeoutError("upstream-specific timeout")
    with pytest.raises(TimeoutError) as caught:
        async with call_timeout(60):
            raise inner
    assert caught.value is inner
