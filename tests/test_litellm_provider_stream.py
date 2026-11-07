"""Streaming tests for `LiteLLMProvider.chat_stream`.

Covers:
- happy-path: chat_stream yields ChatDelta sequence matching mock chunks
- _normalize_stream_chunk default OpenAI shape extraction
- None-content chunks (e.g. final stop chunk) are skipped (return None → no yield)
- signature parity with chat() (messages/tools/model/max_tokens/temperature/
  reasoning_effort/tool_choice all accepted; stream=True forwarded to acompletion)
- chat() and chat_stream() both forward the provider's api_key to acompletion
  as an explicit kwarg, rather than relying on it having been exported to the
  environment

Mocks patch `raven.providers.litellm_provider.acompletion` because the
provider module imports `from litellm import acompletion` at top level, so
patching `litellm.acompletion` after import would not be picked up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from raven.providers.base import ChatDelta, GenerationSettings, LLMProvider, LLMResponse
from raven.providers.litellm_provider import LiteLLMProvider
from raven.providers.rates import resolve_max_output_tokens

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
    """chat_stream yields ChatDelta sequence matching mock OpenAI-shape chunks."""
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
    out: list[ChatDelta] = []
    async for delta in provider.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/gpt-4o",
    ):
        out.append(delta)

    assert [d.content for d in out] == ["Hel", "lo", " world"]
    assert all(isinstance(d, ChatDelta) for d in out)
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
    out: list[ChatDelta] = []
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


def test_construction_does_not_set_the_litellm_module_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two adapters must not fight over litellm.api_base (R1): a per-vendor
    api_base travels per call, never process-wide."""
    import litellm

    monkeypatch.setattr(litellm, "api_base", None, raising=False)
    LiteLLMProvider(api_key="KA", api_base="http://a/v1", default_model="a/one")
    LiteLLMProvider(api_key="KB", api_base="http://b/v1", default_model="b/two")
    assert litellm.api_base is None


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

    out: list[ChatDelta] = []
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
async def test_chat_stream_names_the_ceiling_when_nobody_asked_for_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OpenAI-compatible shape treats `max_tokens` as optional, and this
    branch once left it out on the reasoning that the server would then answer
    with the model's own limit.

    An omitted ceiling is not the model's ceiling. Captured request bodies
    confirm nothing reached the wire, and a deck run's answers were still cut
    at exactly 16384 -- by a component that was never identified, which is the
    argument: a bound we do not send is one we can neither move nor report
    (`flag_truncation` could only log `max_tokens=None`). Asking does move it:
    the same endpoint served 40000 in full to a request that named it. So one
    is named, and `send_max_tokens` is what bounds it.
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

    wire_id = provider.wire_model_id("openai/gpt-4o")
    assert captured["max_tokens"] == resolve_max_output_tokens(wire_id), "the owner's number, on the wire"
    assert resolve_max_output_tokens(wire_id) != resolve_max_output_tokens("openai/gpt-4o"), (
        "the id the request goes out under, not the stored name: LiteLLM files 4096 for "
        "openrouter/openai/gpt-4o and 16384 for openai/gpt-4o, so naming a ceiling makes "
        "the gateway spelling's own row the one that reaches the wire"
    )


@pytest.mark.asyncio
async def test_the_ceiling_is_named_on_a_route_that_would_have_filled_it_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic's Messages API requires `max_tokens`, and LiteLLM's own
    transformation would supply it -- measured, its number and ours agree on
    all 26 rows it files under `litellm_provider == "anthropic"`, because both
    read the same table.

    Named here anyway, so there is one rule rather than a per-route exemption:
    they can only differ on a model LiteLLM does not know, and there its guess
    is unstated while ours is `DEFAULT_MAX_OUTPUT_TOKENS` -- the smallest
    ceiling among the current claude models, which is the number this repo
    already sends on its own Anthropic transport.
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

    assert captured["max_tokens"] == resolve_max_output_tokens("anthropic/claude-opus-4-5")


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


@pytest.mark.parametrize("cost", [0, 0.012345, None, -1, True, "0.5", float("nan"), float("inf")])
@pytest.mark.parametrize(
    "name,base", [("openrouter", "https://openrouter.ai/api/v1"), ("custom", "https://gateway.example/v1")]
)
def test_reported_cost_and_cache_survive_both_response_paths(cost, name, base):
    from litellm import ModelResponse, Usage

    from raven.agent.loop.main import AgentLoop
    from raven.observability.usage import normalize

    provider = LiteLLMProvider(provider_name=name, api_base=base)
    usage = Usage(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        prompt_tokens_details={"cached_tokens": 60, "cache_write_tokens": 10},
        cost=cost,
    )
    response = ModelResponse(
        choices=[{"message": {"content": "ok", "role": "assistant"}, "finish_reason": "stop"}], usage=usage
    )
    parsed = provider._parse_response(response)
    delta = provider._normalize_stream_chunk(_FakeChunk(choices=[], usage=usage))
    assert delta is not None
    assert parsed.usage == delta.usage
    snapshot = AgentLoop._build_usage_snapshot(parsed, "openrouter/test", "session")
    expected = cost if type(cost) in (int, float) and cost >= 0 and cost < float("inf") else None
    assert snapshot.cost_usd == expected
    assert normalize(parsed.usage, "openrouter/test")["cost_usd"] == expected
    assert (snapshot.input_tokens, snapshot.cache_read_tokens, snapshot.cache_write_tokens) == (30, 60, 10)


@pytest.mark.parametrize(
    "name,base",
    [
        ("custom", "https://gateway.example/v1"),
        ("openrouter", "https://gateway.example/v1"),
        ("openrouter", "https://openrouter.ai.example/v1"),
    ],
)
def test_reported_cost_is_independent_of_endpoint(name, base):
    from types import SimpleNamespace

    provider = LiteLLMProvider(provider_name=name, api_base=base)
    usage = provider._normalize_usage(SimpleNamespace(prompt_tokens=100, cost=1.23, cost_usd=9.99))
    assert usage["cost_usd"] == 1.23
    assert "cost_usd" not in provider._normalize_usage(SimpleNamespace(prompt_tokens=100, cost_usd=9.99))


def test_missing_and_zero_cache_are_distinct():
    from litellm import Usage

    provider = _make_provider()
    absent = provider._normalize_usage(Usage(prompt_tokens=10))
    zero = provider._normalize_usage(Usage(prompt_tokens=10, prompt_tokens_details={"cached_tokens": 0}))
    assert "cache_read_input_tokens" not in absent
    assert zero["cache_read_input_tokens"] == 0
    assert "cache_creation_input_tokens" not in zero


@pytest.mark.parametrize(
    "name,base", [("openrouter", "https://openrouter.ai/api/v1"), ("custom", "https://gateway.example/v1")]
)
async def test_usage_only_stream_frames_merge_without_double_counting(monkeypatch, name, base):
    from litellm import Usage

    from raven.providers.streaming import stream_llm_call

    provider = LiteLLMProvider(provider_name=name, api_base=base)
    chunks = [
        _chunk("ok"),
        _FakeChunk(
            choices=[],
            usage=Usage(prompt_tokens=10, completion_tokens=2, cost=0.2, prompt_tokens_details={"cached_tokens": 0}),
        ),
        _FakeChunk(choices=[], usage=Usage(prompt_tokens=10, completion_tokens=2, cost=0.2)),
        _FakeChunk(choices=[], usage={"total_tokens": 12, "cost": None}),
    ]

    async def complete(**kwargs):
        return _fake_stream(chunks)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", complete)
    response = await stream_llm_call(
        provider, messages=[{"role": "user", "content": "hi"}], tools=None, model="openrouter/test"
    )
    assert response.usage["cost_usd"] == 0.2
    assert response.usage["prompt_tokens"] == 10
    assert response.usage["cache_read_input_tokens"] == 0


@pytest.mark.parametrize("name", ["openrouter", "custom"])
def test_raw_response_cost_survives_installed_sdk(name):
    from unittest.mock import Mock

    import httpx
    from litellm import ModelResponse
    from litellm.llms.openai.chat.gpt_transformation import OpenAIGPTConfig
    from litellm.llms.openrouter.chat.transformation import OpenrouterConfig

    raw = httpx.Response(
        200,
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        json={
            "id": "test",
            "model": "test",
            "object": "chat.completion",
            "created": 1,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "total_tokens": 105,
                "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 10},
                "cost": 0.1234,
            },
        },
    )
    config = OpenrouterConfig() if name == "openrouter" else OpenAIGPTConfig()
    response = config.transform_response(
        model="test",
        raw_response=raw,
        model_response=ModelResponse(),
        logging_obj=Mock(),
        request_data={},
        messages=[],
        optional_params={},
        litellm_params={},
        encoding=None,
    )
    provider = LiteLLMProvider(provider_name=name, api_base="https://gateway.example/v1")
    usage = provider._parse_response(response).usage
    assert usage["cost_usd"] == 0.1234
    assert usage["cache_read_input_tokens"] == 60
    assert usage["cache_creation_input_tokens"] == 10


@pytest.mark.parametrize("name", ["openrouter", "custom"])
@pytest.mark.parametrize("cost", [0.1234, 0, None])
async def test_api_stream_usage_survives_sdk_reassembly(monkeypatch, name, cost):
    import json

    import httpx

    from raven.providers.streaming import stream_llm_call

    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 2,
        "total_tokens": 102,
        "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 10},
    }
    if cost is not None:
        usage["cost"] = cost
    common = {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "openai/gpt-4.1-nano"}
    frames = [
        {**common, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "OK"}, "finish_reason": None}]},
        {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {**common, "choices": [], "usage": usage},
    ]
    body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"

    async def send(client, request, **kwargs):
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=body)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    # SDK-calculated stream costs must never replace a missing API amount.
    monkeypatch.setattr("litellm.include_cost_in_streaming_usage", True)
    provider = LiteLLMProvider(
        provider_name=name,
        api_base="https://openrouter.ai/api/v1" if name == "openrouter" else "https://gateway.example/v1",
        api_key="test-key",
        default_model="openai/gpt-4.1-nano",
    )
    response = await stream_llm_call(
        provider, messages=[{"role": "user", "content": "Reply OK."}], tools=None, model="openai/gpt-4.1-nano"
    )
    assert response.content == "OK"
    assert response.usage.get("cost_usd") == cost
    assert response.usage["prompt_tokens"] == 100
    assert response.usage["cache_read_input_tokens"] == 60
    assert response.usage["cache_creation_input_tokens"] == 10


# ---------- how the upstream said the reply ended --------------------------------


def _response_with(message: Any, *, finish_reason: str | None = "stop", usage: Any = None) -> Any:
    """A real ``ModelResponse`` around ``message``.

    ``Choices`` writes ``stop`` over a finish reason of None when it is built, so
    a choice that never carried one has to be cleared after.
    """
    from litellm.types.utils import Choices, ModelResponse, Usage

    response = ModelResponse(
        choices=[Choices(finish_reason="stop", index=0, message=message)],
        usage=usage or Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
    )
    response.choices[0].finish_reason = finish_reason
    return response


def test_a_missing_finish_reason_reads_unknown() -> None:
    """A reply that never said how it ended was reported as ``stop``, which made a
    reply cut by the connection indistinguishable from a finished one."""
    from litellm.types.utils import Message

    parsed = _make_provider()._parse_response(
        _response_with(Message(content="hi", role="assistant"), finish_reason=None)
    )

    assert parsed.content == "hi"
    assert parsed.finish_reason == "unknown"


def test_a_structured_refusal_is_a_refusal() -> None:
    """OpenAI-shaped upstreams put a refusal in ``message.refusal`` and leave
    ``content`` null, so the reply read as an empty response, whatever the
    ``stop`` beside it said."""
    from litellm.types.utils import Message

    parsed = _make_provider()._parse_response(
        _response_with(Message(content=None, role="assistant", refusal="I can't"))
    )

    assert parsed.content == "I can't"
    assert parsed.finish_reason == "refusal"


def test_a_refusal_is_an_answer_whatever_its_accounting_says() -> None:
    """An empty reply whose prompt was billed as a token is judged a request the
    upstream never processed. A refusal is the reply, so that judgment must not
    reach it."""
    from litellm.types.utils import Message, Usage

    refused = _response_with(
        Message(content=None, role="assistant", refusal="I can't"),
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )

    parsed = _make_provider()._parse_response(refused, sent_chars=12000)

    assert parsed.finish_reason == "refusal"
    assert parsed.error_classification is None


@dataclass
class _RefusingDelta(_FakeDelta):
    refusal: str | None = None


class _SdkLikeWrapper:
    """A stream wrapper that keeps the SDK's finish bookkeeping: no reason is
    received until a chunk carrying one has gone by, so everything before the final
    chunk is read while both records are still empty."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = iter(chunks)
        self.received_finish_reason: str | None = None
        self.intermittent_finish_reason: str | None = None

    def __aiter__(self) -> "_SdkLikeWrapper":
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None
        reason = chunk.choices[0].finish_reason if chunk.choices else None
        if reason:
            self.received_finish_reason = self.intermittent_finish_reason = reason
        return chunk

    async def aclose(self) -> None:
        pass


def _refusal_stream() -> _SdkLikeWrapper:
    return _SdkLikeWrapper(
        [
            _FakeChunk(choices=[_FakeChoice(delta=_RefusingDelta(refusal="I can't"))]),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason="stop")]),
        ]
    )


@pytest.mark.asyncio
async def test_a_streamed_refusal_keeps_its_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal streams as ``delta.refusal`` and the stream then closes on a plain
    ``stop``. The refusal is the verdict; the stop only says the stream is over."""
    from raven.providers.streaming import stream_llm_call

    async def fake_acompletion(**_: Any):
        return _refusal_stream()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    response = await stream_llm_call(
        _make_provider(), messages=[{"role": "user", "content": "hi"}], tools=None, model="openai/gpt-4o"
    )

    assert response.content == "I can't"
    assert response.finish_reason == "refusal"


@pytest.mark.asyncio
async def test_a_streamed_refusal_is_not_taken_for_a_made_up_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper has recorded no finish reason when a refusal goes by, which is
    the very state that marks a stop as the library's own invention. A refusal is
    read off a chunk the upstream sent, so it is never that."""

    async def fake_acompletion(**_: Any):
        return _refusal_stream()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    deltas = [d async for d in _make_provider().chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert (deltas[0].content, deltas[0].finish_reason) == ("I can't", "refusal")
    assert deltas[0].finish_synthesized is False


def test_a_delta_without_a_refusal_is_unchanged() -> None:
    """The SDK's own delta type does not declare the field, so most chunks never
    carry one."""
    delta = _make_provider()._normalize_stream_chunk(_chunk("token"))

    assert delta is not None
    assert (delta.content, delta.finish_reason) == ("token", None)


# ---------- a refusal as litellm hands it over -------------------------------------

_REFUSAL = "I can't help with that."


def _refusing_openai_upstream(request: Any) -> Any:
    """An OpenAI chat-completions endpoint that refuses, in the shape it answers each call with."""
    import json

    import httpx

    body = json.loads(request.content)
    if not body.get("stream"):
        message = {"role": "assistant", "content": None, "refusal": _REFUSAL}
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-r",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o",
                "choices": [{"index": 0, "finish_reason": "stop", "logprobs": None, "message": message}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
            },
        )
    head = {"id": "chatcmpl-r", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o"}
    deltas = [
        ({"role": "assistant", "content": None, "refusal": ""}, None),
        ({"refusal": "I can't "}, None),
        ({"refusal": "help with that."}, None),
        ({}, "stop"),
    ]
    events = [
        "data: "
        + json.dumps({**head, "choices": [{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}]})
        + "\n\n"
        for delta, finish in deltas
    ]
    return httpx.Response(
        200, content=("".join(events) + "data: [DONE]\n\n").encode(), headers={"content-type": "text/event-stream"}
    )


def _through_litellm(monkeypatch: pytest.MonkeyPatch, upstream: Any = _refusing_openai_upstream) -> None:
    """Send the provider's calls through litellm's own OpenAI route, onto ``upstream`` (the refusing one)."""
    import httpx
    import litellm
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key="test-key",
        base_url="http://upstream.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )

    async def acompletion(**kwargs: Any) -> Any:
        return await litellm.acompletion(**kwargs, client=client)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", acompletion)


@pytest.mark.asyncio
async def test_an_openai_refusal_is_read_where_litellm_files_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Converting the upstream's body, litellm moves every field its ``Message`` does
    not declare into ``provider_specific_fields``, so the refusal is not on
    ``message.refusal`` by the time the provider reads the reply."""
    _through_litellm(monkeypatch)

    response = await _make_provider().chat(messages=[{"role": "user", "content": "hi"}])

    assert (response.content, response.finish_reason) == (_REFUSAL, "refusal")


def test_a_refusal_filed_with_a_chunks_provider_fields_is_read() -> None:
    """A delta litellm builds keeps an undeclared field where it was given, so a
    refusal filed with its provider fields reads like one on ``delta.refusal``."""
    from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

    chunk = ModelResponseStream(choices=[StreamingChoices(delta=Delta(provider_specific_fields={"refusal": _REFUSAL}))])

    delta = _make_provider()._normalize_stream_chunk(chunk)

    assert delta is not None
    assert (delta.content, delta.finish_reason) == (_REFUSAL, "refusal")


@pytest.mark.asyncio
async def test_a_streamed_openai_refusal_never_reaches_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """litellm's stream wrapper drops a chunk whose only payload is a refusal: its
    test for an empty chunk does not count the field. What reaches the provider is
    the opening chunk, whose refusal is still empty, and a bare ``stop``, which the
    stream reads as an empty reply. The loop reads the refusal on its first resend,
    which goes out unstreamed (the end-to-end test below). Pinned as litellm's
    behaviour, so a litellm that starts passing the words on fails here, and this
    becomes the test that the stream reads them."""
    from raven.providers.streaming import stream_llm_call

    _through_litellm(monkeypatch)

    deltas = [d async for d in _make_provider().chat_stream(messages=[{"role": "user", "content": "hi"}])]
    response = await stream_llm_call(
        _make_provider(), messages=[{"role": "user", "content": "hi"}], tools=None, model="openai/gpt-4o"
    )

    assert [(d.content, d.finish_reason) for d in deltas if d.content or d.finish_reason] == [(None, "stop")]
    assert (response.content, response.finish_reason) == ("", "stop")


# ---------- through the loop: the first resend of an empty streamed reply ----------

_ANSWER = "Here it is."


def _refuses_then_a_fallback_answers(seen: list[tuple[str, bool]]) -> Any:
    """``gpt-4o`` refuses as ``_refusing_openai_upstream`` does; any other model streams ``_ANSWER``.

    Every call is written down as (model, streamed), so a test reads which calls streamed.
    """
    import json

    import httpx

    def upstream(request: Any) -> Any:
        body = json.loads(request.content)
        seen.append((body["model"], bool(body.get("stream"))))
        if body["model"] == "gpt-4o":
            return _refusing_openai_upstream(request)
        head = {"id": "chatcmpl-a", "object": "chat.completion.chunk", "created": 1, "model": body["model"]}
        events = [
            "data: "
            + json.dumps({**head, "choices": [{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}]})
            + "\n\n"
            for delta, finish in [({"role": "assistant", "content": _ANSWER}, None), ({}, "stop")]
        ]
        return httpx.Response(
            200, content=("".join(events) + "data: [DONE]\n\n").encode(), headers={"content-type": "text/event-stream"}
        )

    return upstream


@pytest.mark.asyncio
@pytest.mark.parametrize("fallbacks", [["openai/gpt-4o-mini"], []], ids=["a_fallback_left", "none_left"])
async def test_a_streamed_refusal_is_resent_once_unstreamed_and_read_as_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, fallbacks: list[str]
) -> None:
    """litellm drops the refusal from the stream, so the loop sees an empty stop. Its first resend goes
    out unstreamed, where the refusal is read: the turn takes the refusal's one switch to a fallback
    model, or fails with the model's explanation, instead of resending an empty stream three times."""
    import asyncio

    from raven.agent.loop import AgentLoop, turn_path
    from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
    from raven.agent.loop.recovery import REFUSAL_EXPLANATION, REFUSAL_FAILURE

    class _NoWait:
        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

        async def sleep(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(turn_path, "asyncio", _NoWait())
    seen: list[tuple[str, bool]] = []
    _through_litellm(monkeypatch, _refuses_then_a_fallback_answers(seen))
    loop = AgentLoop(
        provider=_make_provider(),
        workspace=tmp_path,
        model="openai/gpt-4o",
        policy=TurnPolicy(max_iterations=5),
        host=HostWiring(),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    shown: list[str] = []

    async def sink(delta: str) -> None:
        shown.append(delta)

    final, _used, _messages, outcome = await loop._run_agent_loop(
        [{"role": "user", "content": "hi"}], on_token_delta=sink, fallback_models=fallbacks
    )

    if fallbacks:
        assert seen == [("gpt-4o", True), ("gpt-4o", False), ("gpt-4o-mini", True)]
        assert final == _ANSWER and outcome.status == "completed"
        assert shown == [_REFUSAL, _ANSWER], "the refusal as a stream would have shown it, then the answer"
    else:
        assert seen == [("gpt-4o", True), ("gpt-4o", False)]
        assert outcome.status == "error"
        assert outcome.error == REFUSAL_FAILURE + REFUSAL_EXPLANATION.format(text=_REFUSAL)
