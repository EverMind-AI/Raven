"""LiteLLMProvider.chat_stream: a finish reason the library made up is marked as such.

LiteLLM's stream wrapper ends a stream whose upstream closed the connection without a
terminal chunk by fabricating one with ``finish_reason="stop"``, keeping the reason it
actually received (None) on the wrapper. The provider stamps that delta
``finish_synthesized`` so the consumer can tell a cut reply from a finished one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from raven.providers.litellm_provider import LiteLLMProvider


def _chunk(content: str | None = None, finish_reason: str | None = None) -> Any:
    delta = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)], usage=None)


class _Wrapper:
    """A stand-in for LiteLLM's CustomStreamWrapper: iterates chunks and remembers
    the finish reason the upstream sent, if any."""

    def __init__(
        self, chunks: list[Any], received_finish_reason: str | None, intermittent_finish_reason: str | None = None
    ) -> None:
        self._chunks = iter(chunks)
        self.received_finish_reason = received_finish_reason
        self.intermittent_finish_reason = intermittent_finish_reason
        self.closed = False

    def __aiter__(self) -> _Wrapper:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


async def _deltas(monkeypatch: pytest.MonkeyPatch, wrapper: _Wrapper) -> list[Any]:
    async def fake_acompletion(**_kwargs: Any) -> _Wrapper:
        return wrapper

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    provider = LiteLLMProvider(api_key="k", default_model="openrouter/z-ai/glm-5.3-flash")
    return [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]


@pytest.mark.asyncio
async def test_a_stop_the_library_made_up_is_marked_synthesized(monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper = _Wrapper([_chunk(content="par"), _chunk(finish_reason="stop")], received_finish_reason=None)
    deltas = await _deltas(monkeypatch, wrapper)
    assert [d.content for d in deltas] == ["par", None]
    assert deltas[-1].finish_reason == "stop"
    assert deltas[-1].finish_synthesized is True
    assert deltas[0].finish_synthesized is False
    assert wrapper.closed


@pytest.mark.asyncio
async def test_a_stop_the_upstream_sent_is_not_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper = _Wrapper([_chunk(content="done"), _chunk(finish_reason="stop")], received_finish_reason="stop")
    deltas = await _deltas(monkeypatch, wrapper)
    assert deltas[-1].finish_reason == "stop"
    assert deltas[-1].finish_synthesized is False


@pytest.mark.asyncio
async def test_a_wrapper_without_the_attribute_is_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper = _Wrapper([_chunk(finish_reason="stop")], received_finish_reason="stop")
    del wrapper.received_finish_reason
    deltas = await _deltas(monkeypatch, wrapper)
    assert deltas[-1].finish_synthesized is False


@pytest.mark.asyncio
async def test_a_stop_gemini_sent_but_the_wrapper_only_kept_as_intermittent_is_not_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gemini and vertex_ai chunks arrive stamped `_hidden_params["is_finished"] = False`,
    so the wrapper never records their terminal "stop" as received -- only as the last
    reason a chunk carried. Reading the first alone stamped every healthy gemini reply
    as a cut, and a reasoning-only reply then waited out the loop's error ladder."""
    wrapper = _Wrapper(
        [_chunk(content="done"), _chunk(finish_reason="stop")],
        received_finish_reason=None,
        intermittent_finish_reason="stop",
    )
    deltas = await _deltas(monkeypatch, wrapper)
    assert deltas[-1].finish_reason == "stop"
    assert deltas[-1].finish_synthesized is False


def _real_wrapper(provider: str, model: str, chunks: list[Any]) -> Any:
    """LiteLLM's own CustomStreamWrapper over an async stream of chunks, so the attribute
    names the provider reads are bound to the library's real API rather than to a stub."""
    import datetime

    from litellm.litellm_core_utils.litellm_logging import Logging
    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper

    async def upstream():
        for chunk in chunks:
            yield chunk

    logging = Logging(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        call_type="acompletion",
        start_time=datetime.datetime.now(),
        litellm_call_id="test",
        function_id="test",
    )
    return CustomStreamWrapper(
        completion_stream=upstream(), model=model, custom_llm_provider=provider, logging_obj=logging
    )


def _litellm_chunk(content: str | None = None, finish_reason: str | None = None, *, not_finished: bool = False) -> Any:
    from litellm import ModelResponseStream
    from litellm.types.utils import Delta, StreamingChoices

    chunk = ModelResponseStream(
        choices=[StreamingChoices(index=0, delta=Delta(content=content), finish_reason=finish_reason)]
    )
    if not_finished:
        chunk._hidden_params["is_finished"] = False
    return chunk


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "chunks", "synthesized"),
    [
        ("openai", "gpt-4o", [_litellm_chunk("hi"), _litellm_chunk(None, "stop")], False),
        (
            "gemini",
            "gemini/gemini-2.5-flash",
            [_litellm_chunk("hi"), _litellm_chunk(None, "stop", not_finished=True)],
            False,
        ),
        ("openai", "gpt-4o", [_litellm_chunk("hi")], True),
        ("gemini", "gemini/gemini-2.5-flash", [_litellm_chunk("hi", None, not_finished=True)], True),
    ],
    ids=["openai-finished", "gemini-finished-with-marker", "openai-cut", "gemini-cut"],
)
async def test_against_litellms_own_wrapper_only_a_missing_terminal_reason_is_a_cut(
    monkeypatch: pytest.MonkeyPatch, provider: str, model: str, chunks: list[Any], synthesized: bool
) -> None:
    """Driven through the library's real wrapper: a finished reply, openai or gemini
    with its marker, is not marked; a stream that ends without a terminal reason is,
    for both providers. The names `received_finish_reason` and
    `intermittent_finish_reason` are what the provider reads, so a rename in LiteLLM
    fails here rather than silently restoring the bug."""
    wrapper = _real_wrapper(provider, model, chunks)
    assert hasattr(wrapper, "received_finish_reason") and hasattr(wrapper, "intermittent_finish_reason")

    async def fake_acompletion(**_kwargs: Any) -> Any:
        return wrapper

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    provider_obj = LiteLLMProvider(api_key="k", default_model=model)
    deltas = [d async for d in provider_obj.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    finals = [d for d in deltas if d.finish_reason]
    assert finals, deltas
    assert finals[-1].finish_reason == "stop"
    assert finals[-1].finish_synthesized is synthesized
