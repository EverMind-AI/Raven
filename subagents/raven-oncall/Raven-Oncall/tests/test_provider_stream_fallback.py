"""Base ``LLMProvider.chat_stream`` non-streaming fallback.

Providers that implement only non-streaming ``chat`` (azure / codex, and any
future bespoke provider) must still work in the TUI streaming path, which calls
``chat_stream``. The base default wraps ``chat`` into a single terminal delta.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from raven.providers.base import ErrorClassification, GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest


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
    provider = _ChatOnlyProvider(LLMResponse(content="hello", usage={"total_tokens": 5}, reasoning_content="why"))
    deltas = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert len(deltas) == 1
    assert deltas[0].content == "hello"
    assert deltas[0].usage == {"total_tokens": 5}
    assert deltas[0].reasoning_content == "why"
    assert deltas[0].tool_call_delta is None


async def test_fallback_propagates_error_finish_reason_and_classification() -> None:
    """A chat() error response (e.g. Azure non-200) must not be silently
    stripped down to plain content when replayed through the terminal delta --
    the caller needs finish_reason + error_classification to detect it."""
    classification = ErrorClassification(category="http_4xx", should_fallback=True)
    provider = _ChatOnlyProvider(
        LLMResponse(
            content="Azure OpenAI API Error 404: deployment not found",
            finish_reason="error",
            error_classification=classification,
        )
    )
    deltas = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert len(deltas) == 1
    assert deltas[0].content == "Azure OpenAI API Error 404: deployment not found"
    assert deltas[0].finish_reason == "error"
    assert deltas[0].error_classification is classification


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


# ---------------------------------------------------------------------------
# The stream is closed on every exit, including the most likely one
# ---------------------------------------------------------------------------


class _FakeStream:
    """A stream whose first pull can be made to hang, and that records aclose."""

    def __init__(self, chunks, *, stall_first=False, refuse_first_pull=False, log=None, name="stream"):
        self._chunks = list(chunks)
        self._stall_first = stall_first
        self._refuse_first_pull = refuse_first_pull
        self._log = log
        self._name = name
        self.pulls = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.pulls += 1
        if self._stall_first and self.pulls == 1:
            await asyncio.sleep(3600)
        if self._refuse_first_pull and self.pulls == 1:
            raise RuntimeError("litellm.BadRequestError: 400 cache_control: Extra inputs are not permitted")
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self):
        if self._log is not None and not self.closed:
            self._log.append(f"close:{self._name}")
        self.closed = True


@pytest.mark.asyncio
async def test_a_first_chunk_timeout_still_closes_the_stream(monkeypatch):
    """The likeliest timeout there is, and it used to leak.

    Hoisting the first pull out of the try/finally left the underlying HTTP
    stream open on exactly the exit that happens most -- a gateway queueing or
    cold-starting before the first byte. `aclosing()` upstream cannot recover it:
    the exception has already driven the generator to a terminated state, where
    `aclose()` is a no-op.
    """
    from raven.providers.litellm_provider import LiteLLMProvider

    stream = _FakeStream([], stall_first=True)
    provider = LiteLLMProvider(api_key="k", default_model="anthropic/claude-fable-5")
    provider.generation = GenerationSettings(temperature=0, max_tokens=8, timeout=0.05)
    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        _always(stream),
        raising=False,
    )

    with pytest.raises(asyncio.TimeoutError):
        async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            pass

    assert stream.closed, "the stream was left open on a first-chunk timeout"


def _always(stream):
    async def _acompletion(**kwargs):
        return stream

    return _acompletion


class _RefusingOnce:
    """An acompletion stand-in that refuses the field once, at a chosen point.

    Records the streams it hands out and the order of opens and closes, because
    "the refused stream was closed before its replacement was opened" is a claim
    about sequence, not just about a final state.
    """

    def __init__(self, *, at: str):
        self.at = at
        self.calls = 0
        self.streams: list[_FakeStream] = []
        self.events: list[str] = []

    async def __call__(self, **kwargs):
        self.calls += 1
        first_try = self.calls == 1
        self.events.append(f"open{self.calls}")
        if self.at == "open" and first_try:
            raise RuntimeError(
                "litellm.BadRequestError: 400 messages.0.content.0.text.cache_control: Extra inputs are not permitted"
            )
        stream = _FakeStream(
            [_chunk()],
            refuse_first_pull=self.at == "pull" and first_try,
            log=self.events,
            name=f"stream{self.calls}",
        )
        self.streams.append(stream)
        return stream


def _chunk():
    class _C:
        choices: list = []

    return _C()


@pytest.mark.parametrize("thrown_at", ["open", "pull"])
@pytest.mark.asyncio
async def test_a_refused_stream_recovers_wherever_the_refusal_is_thrown(monkeypatch, thrown_at):
    """Both points are "before the first chunk", and each route picks one.

    An OpenAI-shaped route raises while opening; a gateway that defers the
    request until the first pull raises there. Covering only the pull would
    leave the learned downgrade out of reach on the open-raising route -- and
    that route is the one the TUI streams over.
    """
    from raven.providers import prompt_cache
    from raven.providers.litellm_provider import LiteLLMProvider

    prompt_cache.reset_suppressions()
    acompletion = _RefusingOnce(at=thrown_at)
    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", acompletion, raising=False)

    model = "openrouter/anthropic/claude-3-haiku"
    provider = LiteLLMProvider(api_key="k", default_model=model, provider_name="openrouter")
    provider.generation = GenerationSettings(temperature=0, max_tokens=8, timeout=5)

    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}], model=model):
        pass

    assert acompletion.calls == 2, f"refusal at {thrown_at} was not retried"
    assert prompt_cache.is_suppressed(model), f"refusal at {thrown_at} was not learned"

    # Every stream handed out is closed, and the refused one is closed *before*
    # its replacement is opened -- at most one live at a time. Asserting only the
    # final state left the close-before-reopen fix with nothing pinning it:
    # deleting it kept every related test green.
    assert all(s.closed for s in acompletion.streams), f"left open: {acompletion.events}"
    if thrown_at == "pull":
        assert acompletion.events.index("close:stream1") < acompletion.events.index("open2"), (
            f"the refused stream outlived its replacement's open: {acompletion.events}"
        )
    prompt_cache.reset_suppressions()


@pytest.mark.asyncio
async def test_a_none_chunk_does_not_end_the_stream(monkeypatch):
    """A chunk of None is a chunk, not the end of the stream.

    Pulling the first chunk before the loop needs a value meaning "there was
    none"; reusing None for that let a provider yielding one truncate the
    response silently, which is not what the loop did before it was restructured.
    """
    from raven.providers.litellm_provider import LiteLLMProvider

    def _text(value):
        class _Delta:
            content = value
            tool_calls = None
            reasoning_content = None

        class _Choice:
            delta = _Delta()

        class _C:
            choices = [_Choice()]
            usage = None

        return _C()

    stream = _FakeStream([_text("a"), None, _text("b")])
    provider = LiteLLMProvider(api_key="k", default_model="anthropic/claude-fable-5")
    provider.generation = GenerationSettings(temperature=0, max_tokens=8, timeout=5)
    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", _always(stream), raising=False)

    seen = [d.content async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert seen == ["a", "b"], f"the None chunk cut the stream short: {seen}"
    assert stream.closed
