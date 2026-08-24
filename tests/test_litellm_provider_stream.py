"""Streaming tests for `LiteLLMProvider.chat_stream`.

Covers:
- happy-path: chat_stream yields StreamDelta sequence matching mock chunks
- _normalize_stream_chunk default OpenAI shape extraction
- None-content chunks (e.g. final stop chunk) are skipped (return None → no yield)
- signature parity with chat() (messages/tools/model/max_tokens/temperature/
  reasoning_effort/tool_choice all accepted; stream=True forwarded to acompletion)
- chat() and chat_stream() both forward the provider's api_key to acompletion
  as an explicit kwarg, rather than relying on it having been exported to the
  environment
- DeepSeek V4 tool continuations replay an empty reasoning_content field

Mocks patch `raven.providers.litellm_provider.acompletion` because the
provider module imports `from litellm import acompletion` at top level, so
patching `litellm.acompletion` after import would not be picked up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, StreamDelta
from raven.providers.litellm_provider import LiteLLMProvider

# ---------- Test doubles modelling OpenAI ChatCompletionChunk shape ----------


@dataclass
class _FakeDelta:
    content: str | None = None
    tool_calls: list[Any] | None = None


@dataclass
class _FakeChoice:
    delta: _FakeDelta
    finish_reason: str | None = None
    index: int = 0


@dataclass
class _FakeChunk:
    choices: list[_FakeChoice]
    usage: Any | None = None


def _chunk(content: str | None) -> _FakeChunk:
    return _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=content))])


class _FakeResponse:
    """Non-streaming acompletion result with one text choice."""

    def __init__(self, text: str) -> None:
        self.choices = [_FakeChoice(delta=_FakeDelta(content=text), finish_reason="stop")]
        self.usage = None


async def _fake_stream(chunks: list[_FakeChunk]):
    """Async generator standing in for litellm's streamed response."""
    for ch in chunks:
        yield ch


def _make_provider() -> LiteLLMProvider:
    # api_key kept truthy so the kwargs path that forwards it is exercised,
    # but no real network is touched — acompletion is patched.
    return LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")


# ----------------------------- Tests ---------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_yields_stream_deltas_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """chat_stream yields StreamDelta sequence matching mock OpenAI-shape chunks."""
    chunks = [_chunk("Hel"), _chunk("lo"), _chunk(" world")]

    captured_kwargs: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured_kwargs.update(kwargs)
        return _fake_stream(chunks)

    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        fake_acompletion,
    )

    provider = _make_provider()
    out: list[StreamDelta] = []
    async for delta in provider.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/gpt-4o",
    ):
        out.append(delta)

    assert [d.content for d in out] == ["Hel", "lo", " world"]
    assert all(isinstance(d, StreamDelta) for d in out)
    # stream=True must be forwarded to LiteLLM
    assert captured_kwargs.get("stream") is True
    # Usage must be requested explicitly — OpenAI-compatible providers omit the
    # trailing usage chunk otherwise, leaving cost / context tracking at zero.
    assert captured_kwargs.get("stream_options") == {"include_usage": True}


def test_normalize_stream_chunk_openai_shape_default() -> None:
    """_normalize_stream_chunk default path extracts OpenAI-shape content."""
    provider = _make_provider()
    chunk = _chunk("token")
    delta = provider._normalize_stream_chunk(chunk)
    assert delta is not None
    assert delta.content == "token"
    assert delta.tool_call_delta is None
    assert delta.usage is None


def test_normalize_stream_chunk_returns_none_for_empty_payload() -> None:
    """Chunks carrying nothing at all return None — chat_stream skips them."""
    provider = _make_provider()
    # No content, no tool_calls, no usage, and no finish_reason either.
    chunk = _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason=None)])
    assert provider._normalize_stream_chunk(chunk) is None


def test_normalize_stream_chunk_keeps_terminal_finish_reason() -> None:
    """A stop-marker chunk is no longer empty: finish_reason is its payload.

    Upstream states why generation stopped only on this chunk. Skipping it
    (which the emptiness check used to do, since content/tool_calls/usage are
    all absent there) throws away the one signal distinguishing "finished"
    from "cut off at the output ceiling".
    """
    provider = _make_provider()
    for reason in ("stop", "length", "tool_calls"):
        chunk = _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason=reason)])
        delta = provider._normalize_stream_chunk(chunk)
        assert delta is not None, f"terminal chunk with finish_reason={reason!r} was skipped"
        assert delta.finish_reason == reason
        assert delta.content is None


def test_normalize_stream_chunk_finish_reason_absent_mid_stream() -> None:
    """Content-bearing chunks mid-stream carry no finish_reason."""
    provider = _make_provider()
    delta = provider._normalize_stream_chunk(_chunk("token"))
    assert delta is not None
    assert delta.finish_reason is None


@pytest.mark.asyncio
async def test_chat_stream_skips_none_content_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mixed sequence with a None-content chunk: normalizer returns None → no yield."""
    chunks = [
        _chunk("a"),
        _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason=None)]),
        _chunk("b"),
    ]

    async def fake_acompletion(**_kwargs: Any):
        return _fake_stream(chunks)

    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        fake_acompletion,
    )

    provider = _make_provider()
    out = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert [d.content for d in out] == ["a", "b"]


@pytest.mark.asyncio
async def test_chat_stream_signature_parity_with_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """chat_stream accepts every chat() kwarg without raising.

    Smoke check: pass the full chat() parameter set and verify kwargs hit
    acompletion (stream=True, model/messages/tools/tool_choice present;
    reasoning_effort forwarded; max_tokens/temperature forwarded).
    """
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _fake_stream([_chunk("ok")])

    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        fake_acompletion,
    )

    provider = _make_provider()
    tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
    out: list[StreamDelta] = []
    async for delta in provider.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="openai/gpt-4o-mini",
        max_tokens=128,
        temperature=0.3,
        reasoning_effort="medium",
        tool_choice="auto",
    ):
        out.append(delta)

    assert [d.content for d in out] == ["ok"]
    assert captured["stream"] is True
    assert captured["max_tokens"] == 128
    assert captured["temperature"] == 0.3
    assert captured["reasoning_effort"] == "medium"
    assert captured["tool_choice"] == "auto"
    assert captured["tools"] == tools
    # model should be resolved (openai/gpt-4o-mini already has prefix → stays the same)
    assert "gpt-4o-mini" in captured["model"]


@pytest.mark.asyncio
async def test_chat_forwards_api_key_to_acompletion(monkeypatch: pytest.MonkeyPatch) -> None:
    """chat() must pass the provider's api_key explicitly to acompletion.

    A subagent spawned in-process reuses the main provider instance (see
    SubagentManager), so if this explicit forwarding were ever dropped in
    favor of relying on an exported environment variable, a request made
    under a different/missing env context (e.g. a subprocess or a provider
    with no matching env var) would silently lose the key.
    """
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _FakeResponse("hi")

    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        fake_acompletion,
    )

    provider = LiteLLMProvider(api_key="k-main", default_model="openai/gpt-4o")
    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o")

    assert captured["api_key"] == "k-main"


@pytest.mark.asyncio
async def test_chat_stream_forwards_api_key_to_acompletion(monkeypatch: pytest.MonkeyPatch) -> None:
    """chat_stream() must pass the provider's api_key explicitly to acompletion.

    Same regression as test_chat_forwards_api_key_to_acompletion, for the
    streaming code path.
    """
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _fake_stream([_chunk("ok")])

    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        fake_acompletion,
    )

    provider = LiteLLMProvider(api_key="k-main", default_model="openai/gpt-4o")
    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        pass

    assert captured["api_key"] == "k-main"


@pytest.mark.parametrize(
    ("model", "provider_name"),
    [
        ("deepseek/deepseek-v4-flash", "deepseek"),
        ("deepseek/deepseek-v4-pro", "deepseek"),
        ("openrouter/deepseek/deepseek-v4-pro", "openrouter"),
    ],
)
@pytest.mark.asyncio
async def test_chat_replays_empty_reasoning_for_deepseek_v4_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    provider_name: str,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _FakeResponse("ok")

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "probe", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "content": "done", "tool_call_id": "call_1"},
    ]
    tools = [{"type": "function", "function": {"name": "probe", "parameters": {}}}]
    provider = LiteLLMProvider(api_key="test-key", provider_name=provider_name, default_model=model)

    await provider.chat(messages=messages, tools=tools)

    assert captured["messages"][0]["reasoning_content"] == ""


@pytest.mark.asyncio
async def test_chat_stream_replays_empty_reasoning_for_deepseek_v4_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _fake_stream([_chunk("ok")])

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    messages = [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]}]
    tools = [{"type": "function", "function": {"name": "probe", "parameters": {}}}]
    provider = LiteLLMProvider(
        api_key="test-key",
        provider_name="deepseek",
        default_model="deepseek/deepseek-v4-flash",
    )

    async for _ in provider.chat_stream(messages=messages, tools=tools):
        pass

    assert captured["messages"][0]["reasoning_content"] == ""


@pytest.mark.parametrize(
    ("model", "provider_name"),
    [
        ("deepseek/deepseek-chat", "deepseek"),
        ("openai/gpt-4o", "openai"),
    ],
)
@pytest.mark.asyncio
async def test_chat_leaves_non_thinking_tool_call_messages_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    provider_name: str,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _FakeResponse("ok")

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    messages = [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]}]
    tools = [{"type": "function", "function": {"name": "probe", "parameters": {}}}]
    provider = LiteLLMProvider(api_key="test-key", provider_name=provider_name, default_model=model)

    await provider.chat(messages=messages, tools=tools)

    assert "reasoning_content" not in captured["messages"][0]


# --------- generation settings reach the request body (regression) ----------
#
# chat_stream used to declare literal defaults (max_tokens=4096,
# temperature=0.7, reasoning_effort=None). The agent loop calls it with
# messages/tools/model only, so those literals silently shadowed whatever the
# user had configured — a class of defect that is invisible in review because
# the signature reads as perfectly reasonable. Both assertions below therefore
# check the *outgoing request body*, not that a function was called.


@pytest.mark.asyncio
async def test_chat_stream_sends_configured_generation_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured generation settings reach acompletion's kwargs verbatim."""
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _fake_stream([_chunk("ok")])

    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        fake_acompletion,
    )

    provider = _make_provider()
    provider.generation = GenerationSettings(max_tokens=8192, temperature=0.1, reasoning_effort="low")

    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        pass

    assert captured["max_tokens"] == 8192
    assert captured["temperature"] == 0.1
    assert captured["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_chat_stream_explicit_arguments_still_win() -> None:
    """An explicit argument overrides the configured default."""
    captured: dict[str, Any] = {}

    class _Recorder(LLMProvider):
        async def chat(self, messages, tools=None, model=None, **kwargs: Any) -> LLMResponse:
            captured.update(kwargs)
            return LLMResponse(content="ok", finish_reason="stop")

        def get_default_model(self) -> str:
            return "stub"

    provider = _Recorder()
    provider.generation = GenerationSettings(max_tokens=8192, temperature=0.1, reasoning_effort="low")

    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}], max_tokens=256):
        pass

    assert captured["max_tokens"] == 256  # explicit wins
    assert captured["temperature"] == 0.1  # unset falls back to config


@pytest.mark.asyncio
async def test_base_chat_stream_fallback_sends_configured_settings() -> None:
    """The base non-streaming fallback resolves settings the same way.

    Providers without real streaming (azure / codex / custom) reach the model
    through this default implementation. Leaving its literals in place would
    keep them on 4096 even after the LiteLLM path is fixed.
    """
    captured: dict[str, Any] = {}

    class _ChatOnly(LLMProvider):
        async def chat(self, messages, tools=None, model=None, **kwargs: Any) -> LLMResponse:
            captured.update(kwargs)
            return LLMResponse(content="ok", finish_reason="stop")

        def get_default_model(self) -> str:
            return "stub"

    provider = _ChatOnly()
    provider.generation = GenerationSettings(max_tokens=8192, temperature=0.1, reasoning_effort="low")

    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        pass

    assert captured["max_tokens"] == 8192
    assert captured["temperature"] == 0.1
    assert captured["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_chat_stream_surfaces_upstream_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumer sees upstream's finish_reason on the terminal delta.

    Without this the loop can only guess why generation stopped, and a
    response cut off at the output ceiling is indistinguishable from one the
    model chose to end.
    """
    chunks = [
        _chunk("par"),
        _chunk("tial"),
        _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason="length")]),
    ]

    async def fake_acompletion(**_: Any):
        return _fake_stream(chunks)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    out: list[StreamDelta] = []
    async for delta in _make_provider().chat_stream(messages=[{"role": "user", "content": "hi"}]):
        out.append(delta)

    assert [d.content for d in out if d.content] == ["par", "tial"]
    assert out[-1].finish_reason == "length"
    # Mid-stream deltas stay clean so a consumer can key on "the one that has it".
    assert [d.finish_reason for d in out[:-1]] == [None, None]


@pytest.mark.asyncio
async def test_upstream_length_does_not_trip_the_error_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finish_reason now carries two unrelated meanings; they must not collide.

    The agent loop treats `finish_reason == "error"` as a replayed provider
    failure. Upstream values ride the same field, so a truncated response must
    not be mistaken for one.
    """
    chunks = [_FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason="length")])]

    async def fake_acompletion(**_: Any):
        return _fake_stream(chunks)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    out = [d async for d in _make_provider().chat_stream(messages=[{"role": "user", "content": "hi"}])]
    assert out[-1].finish_reason == "length"
    assert out[-1].finish_reason != "error"
    assert out[-1].error_classification is None


@pytest.mark.asyncio
async def test_chat_stream_omits_the_ceiling_when_nobody_asked_for_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OpenAI-compatible shape treats `max_tokens` as optional, and every
    surveyed agent leaves it out there rather than volunteering a number.

    Volunteering one is what this whole branch has been paying for: the number
    has to be right, has to match what any check compares against, and has to
    add up with whatever the prompt was allowed to grow to. Omitted, the server
    answers with its own limit and none of that applies.
    """
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _fake_stream([_chunk("ok")])

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    provider = LiteLLMProvider(api_key="test-key", provider_name="openrouter", default_model="openai/gpt-4o")
    provider.generation = GenerationSettings()

    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        pass

    assert "max_tokens" not in captured, "no ceiling was asked for and none is volunteered"


@pytest.mark.asyncio
async def test_no_ceiling_is_volunteered_even_where_the_api_requires_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic's Messages API does require `max_tokens` -- and LiteLLM's own
    transformation supplies it, which is where that knowledge belongs.

    Measured across all 26 rows LiteLLM files under `litellm_provider ==
    "anthropic"`, its number and ours agree on every one, because both read the
    same table. They can only differ on a model it does not know, and there
    both sides are guessing from a constant; ours is not the better guess.

    Deciding it here meant carrying a copy of LiteLLM's routing knowledge --
    which vendor speaks which API -- and then keeping the copy aligned. That
    alignment is what `wire_model_id` and three invariants existed for. The
    surveyed agents do not have this problem because each protocol adapter owns
    its own required fields; ours is LiteLLM, so it owns them.
    """
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _fake_stream([_chunk("ok")])

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    provider = LiteLLMProvider(api_key="test-key", default_model="anthropic/claude-opus-4-5")
    provider.generation = GenerationSettings()

    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        pass

    assert "max_tokens" not in captured, "LiteLLM's anthropic transformation fills this in"


@pytest.mark.asyncio
async def test_chat_stream_carries_an_explicit_pin_regardless(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller that asked for a short answer gets one, on any vendor."""
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _fake_stream([_chunk("ok")])

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    provider = LiteLLMProvider(api_key="test-key", provider_name="openrouter", default_model="openai/gpt-4o")
    provider.generation = GenerationSettings()

    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}], max_tokens=64):
        pass

    assert captured["max_tokens"] == 64
