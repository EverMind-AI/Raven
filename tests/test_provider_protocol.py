"""Tests for model wire protocol defaults and explicit overrides."""

import json

import httpx
import pytest

from raven.config.schema import Config
from raven.providers.anthropic_messages_provider import AnthropicMessagesProvider, convert_messages
from raven.providers.factory import make_provider
from raven.providers.litellm_provider import LiteLLMProvider
from raven.providers.openai_responses_provider import OpenAIResponsesProvider
from raven.providers.protocol import effective_protocol
from raven.providers.wire import wire_model


def _config(model: str, *, override: str | None = None) -> Config:
    custom = {
        "apiKey": "test-key",
        "apiBase": "https://openrouter.ai/api/v1",
        "models": [model],
    }
    if override is not None:
        custom["modelProtocols"] = {model: override}
    return Config.model_validate(
        {
            "agents": {"defaults": {"model": model, "provider": "custom"}},
            "providers": {"custom": custom},
        }
    )


def _provider_config(provider: str, model: str, *, override: str | None = None) -> Config:
    key = "sk-or-test" if provider == "openrouter" else "test-key"
    section = {"apiKey": key, "models": [model]}
    if override is not None:
        section["modelProtocols"] = {model: override}
    return Config.model_validate(
        {
            "agents": {"defaults": {"model": model, "provider": provider}},
            "providers": {provider: section},
        }
    )


def test_model_protocol_defaults_and_overrides() -> None:
    config = _config("openai/gpt-5.6-sol")
    assert effective_protocol(config.providers.custom, "openai/gpt-5.6-sol") == "responses"
    assert effective_protocol(config.providers.custom, "anthropic/claude-opus-5") == "anthropic"
    assert effective_protocol(config.providers.custom, "google/gemini-3") == "chat"
    assert (
        effective_protocol(_config("openai/gpt-5.6-sol", override="chat").providers.custom, "openai/gpt-5.6-sol")
        == "chat"
    )


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "unknown", "llama-4", "o3", None])
def test_unlisted_models_default_to_chat(model):
    assert effective_protocol(None, model) == "chat"
    assert effective_protocol({}, model) == "chat"


@pytest.mark.parametrize(
    "model",
    [
        "qwen3.8-max",
        "Qwen/Qwen3-Coder",
        "seed-2.0",
        "doubao-seed-2-0-pro",
        "MiniMax-M3",
        "deepseek-chat",
        "deepseek/deepseek-v4-pro",
        "openai/gpt-5",
        "kimi-k3",
        "openrouter/moonshotai/kimi-k2.5",
    ],
)
def test_response_series_defaults_preserve_manual_choices(model):
    assert effective_protocol(None, model) == "responses"
    for protocol in ("chat", "responses", "anthropic"):
        assert effective_protocol({"protocol": protocol}, model) == protocol
        assert effective_protocol({"model_protocols": {model: protocol}}, model) == protocol


def test_claude_default_and_explicit_protocol_priority():
    assert effective_protocol(None, "anthropic/claude-opus-5") == "anthropic"
    assert effective_protocol({"protocol": "chat"}, "glm-5.3-flash") == "chat"
    assert effective_protocol({"protocol": "responses"}, "claude-opus-5") == "responses"
    assert isinstance(make_provider(_config("glm-5.3-flash")), AnthropicMessagesProvider)


def test_factory_selects_protocol_adapter() -> None:
    assert isinstance(make_provider(_config("openai/gpt-5.6-sol")), OpenAIResponsesProvider)
    assert isinstance(make_provider(_config("anthropic/claude-opus-5")), AnthropicMessagesProvider)
    assert isinstance(make_provider(_config("openai/gpt-5.6-sol", override="chat")), LiteLLMProvider)


def test_openrouter_selects_native_protocol_adapters() -> None:
    response = make_provider(_provider_config("openrouter", "openrouter/openai/gpt-5.6-sol"))
    messages = make_provider(_provider_config("openrouter", "openrouter/anthropic/claude-opus-5"))

    assert isinstance(response, OpenAIResponsesProvider)
    assert isinstance(messages, AnthropicMessagesProvider)
    assert response.wire_model_id("openrouter/openai/gpt-5.6-sol") == "openai/gpt-5.6-sol"
    assert messages.wire_model_id("openrouter/anthropic/claude-opus-5") == "anthropic/claude-opus-5"


def test_native_clients_strip_their_storage_prefix() -> None:
    assert wire_model("openai/gpt-5.6-sol", client_provider="openai") == "gpt-5.6-sol"
    assert wire_model("anthropic/claude-opus-5", client_provider="anthropic") == "claude-opus-5"


def _anthropic_sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


async def test_anthropic_stream_carries_thinking_blocks_and_tool_calls() -> None:
    """A thinking turn that ends in a tool call used to raise inside the stream
    consumer: the terminal delta handed ``thinking_blocks`` to a ChatDelta that
    had no such field, so every claude turn with extended thinking died with
    ``unexpected keyword argument`` and the page showed only the words said before
    the tool call."""
    from raven.providers.anthropic_messages_provider import consume_message_stream

    body = _anthropic_sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 12}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "fetch first"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-1"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Looking it up."}},
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "web_fetch", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"url": "https://ex'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": 'ample.com"}'},
        },
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}},
        {"type": "message_stop"},
    )
    deltas = [d async for d in consume_message_stream(httpx.Response(200, content=body), timeout=5.0)]

    terminal = deltas[-1]
    assert terminal.finish_reason == "tool_calls"
    assert terminal.thinking_blocks == [{"type": "thinking", "thinking": "fetch first", "signature": "sig-1"}]
    assert "".join(d.content or "" for d in deltas) == "Looking it up."
    fragments = [d.tool_call_delta["tool_calls"][0] for d in deltas if d.tool_call_delta]
    assert fragments[0]["function"]["name"] == "web_fetch"
    assert "".join(f["function"]["arguments"] for f in fragments) == '{"url": "https://example.com"}'


async def test_stream_accumulator_keeps_the_thinking_blocks_for_the_next_turn() -> None:
    """The signed blocks must come back on the response the loop stores, or the
    next Anthropic request replays a tool_use turn without its thinking and is
    refused."""
    from raven.contracts.llm_provider import ChatDelta
    from raven.providers.streaming import stream_llm_call

    blocks = [{"type": "thinking", "thinking": "plan", "signature": "sig"}]

    class _Stub:
        generation = None

        async def chat_stream(self, **_kwargs):
            yield ChatDelta(content=None, reasoning_content="plan")
            yield ChatDelta(content="done", thinking_blocks=None)
            yield ChatDelta(content=None, finish_reason="stop", usage={"output_tokens": 3}, thinking_blocks=blocks)

    response = await stream_llm_call(_Stub(), messages=[{"role": "user", "content": "hi"}], tools=None, model="m")
    assert response.content == "done"
    assert response.thinking_blocks == blocks
    assert response.reasoning_content == "plan"


def test_anthropic_parse_message_ignores_the_response_id() -> None:
    """The non-streaming parser handed ``response_id`` to LLMResponse, a field the
    contract does not have, so every Visual Domain Selector call on a claude model
    failed and the selector degraded to its full catalog."""
    from raven.providers.anthropic_messages_provider import parse_message

    response = parse_message(
        {
            "id": "msg_1",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "content": [{"type": "text", "text": "ok"}],
        }
    )
    assert response.content == "ok" and response.finish_reason == "stop"


def test_anthropic_requests_end_on_the_user_side() -> None:
    """A hook note appended after the last exchange is a trailing assistant
    message; claude models refuse that as prefill, so it crosses as user text."""
    from raven.providers.anthropic_messages_provider import convert_messages

    _, converted = convert_messages(
        [
            {"role": "user", "content": "draw the poster"},
            {"role": "assistant", "content": "starting"},
            {"role": "user", "content": "go on"},
            {"role": "assistant", "content": "<task_state>items: 1, 2</task_state>"},
        ]
    )
    assert [m["role"] for m in converted] == ["user", "assistant", "user"]
    assert converted[-1]["content"][-1]["text"] == "<task_state>items: 1, 2</task_state>"


def _anthropic_body(**overrides):
    from raven.providers.anthropic_messages_provider import build_request_body

    kwargs = dict(
        model="claude-opus-5",
        messages=[{"role": "user", "content": "write the page"}],
        tools=None,
        max_tokens=None,
        temperature=None,
        reasoning_effort="high",
        tool_choice=None,
        stream=True,
    )
    kwargs.update(overrides)
    return build_request_body(**kwargs)


def test_anthropic_default_output_ceiling_leaves_room_for_the_file() -> None:
    """Every request went out with max_tokens 4096 unless the operator set one, and
    the thinking budget was carved out of that same number: a poster's HTML was cut
    at the ceiling on every write, and a high-effort turn could come back as
    nothing but thinking, which the loop reads as an empty turn."""
    body = _anthropic_body(model="claude-haiku-4-5")
    assert body["max_tokens"] == 64000
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert _anthropic_body(model="claude-haiku-4-5", reasoning_effort="max")["thinking"]["budget_tokens"] == 64000 // 2
    small = _anthropic_body(model="claude-haiku-4-5", max_tokens=4096)
    assert small["max_tokens"] == 4096 and small["thinking"]["budget_tokens"] == 2048
    assert "thinking" not in _anthropic_body(model="claude-haiku-4-5", reasoning_effort=None)
    assert _anthropic_body()["max_tokens"] == 64000


def test_anthropic_thinks_adaptively_on_the_models_that_require_it() -> None:
    """Claude 4.7 and later reject a budgeted ``thinking.type: "enabled"`` and
    take depth as ``output_config.effort``; the 4.5 and 4.6 models are the other
    way round. The model id decides, prefixed or not."""
    adaptive = _anthropic_body(model="anthropic/claude-opus-5", reasoning_effort="xhigh")
    assert adaptive["thinking"] == {"type": "adaptive"} and adaptive["output_config"] == {"effort": "xhigh"}
    assert "temperature" not in adaptive
    assert _anthropic_body(model="claude-fable-5-1", reasoning_effort="minimal")["output_config"] == {"effort": "low"}
    budgeted = _anthropic_body(model="claude-haiku-4-5", reasoning_effort="high")
    assert budgeted["thinking"] == {"type": "enabled", "budget_tokens": 8192} and "output_config" not in budgeted
    assert "thinking" not in _anthropic_body(model="claude-opus-5", reasoning_effort=None)


def test_anthropic_repairs_the_thinking_mode_a_400_complains_about() -> None:
    """An id the family regex cannot read is guessed wrong half the time; the
    400 names the mode the model wants, and the body is rewritten for one retry."""
    from raven.providers.anthropic_messages_provider import rewrite_on_400

    body = _anthropic_body(model="claude-3-7-sonnet-latest", reasoning_effort="high")
    assert body["thinking"]["type"] == "enabled"
    assert rewrite_on_400(body, 'thinking.type.enabled is not supported on this model; use thinking.type: "adaptive"')
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {"effort": "high"}

    back = _anthropic_body(model="anthropic/claude-opus-5", reasoning_effort="max")
    assert rewrite_on_400(back, "thinking.type: adaptive is not supported on this model")
    assert back["thinking"] == {"type": "enabled", "budget_tokens": 64000 // 2} and "output_config" not in back

    level = _anthropic_body(model="claude-sonnet-5", reasoning_effort="xhigh")
    assert rewrite_on_400(level, "output_config.effort: xhigh is not a valid effort level for this model")
    assert "output_config" not in level and level["thinking"] == {"type": "adaptive"}
    assert not rewrite_on_400(level, "something else entirely")


def test_anthropic_replays_only_signed_thinking_blocks() -> None:
    """A stream cut at the output ceiling ends inside the thinking, so the block
    carries no signature; sent back, the API refused it as a modified thinking
    block and the next turn died."""
    from raven.providers.anthropic_messages_provider import convert_messages

    _, converted = convert_messages(
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "thinking_blocks": [
                    {"type": "thinking", "thinking": "cut off here", "signature": ""},
                    {"type": "thinking", "thinking": "settled", "signature": "sig-2"},
                    {"type": "redacted_thinking", "data": ""},
                ],
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
    )
    replayed = [b for b in converted[1]["content"] if b["type"] in {"thinking", "redacted_thinking"}]
    assert replayed == [{"type": "thinking", "thinking": "settled", "signature": "sig-2"}]


async def test_anthropic_retries_once_at_the_ceiling_the_model_names(monkeypatch) -> None:
    """The default ceiling suits the current models; an older one refuses it and
    names its own limit, which is the one number the retry needs."""
    from raven.providers import anthropic_messages_provider as mod

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["max_tokens"] > 8192:
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"max_tokens: {body['max_tokens']} > 8192, which is the maximum allowed number "
                        "of output tokens for claude-3-5-sonnet-20241022",
                    },
                },
            )
        return httpx.Response(
            200,
            content=_anthropic_sse(
                {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "fits"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ),
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    # An owner that knows nothing about this model, so the request starts at the claude fallback.
    monkeypatch.setattr(mod, "send_max_tokens", lambda gen, model, pinned=None, allow_fetch=True: pinned or 64000)
    provider = AnthropicMessagesProvider(
        api_key="k", api_base="https://api.anthropic.com/v1", default_model="claude-3-5-sonnet-20241022"
    )

    deltas = [d async for d in provider.chat_stream([{"role": "user", "content": "hi"}], reasoning_effort="high")]
    again = [d async for d in provider.chat_stream([{"role": "user", "content": "hi again"}], reasoning_effort="high")]

    # The refusal is earned once: the ceiling it named is remembered for the model.
    assert [b["max_tokens"] for b in seen] == [64000, 8192, 8192]
    assert seen[1]["thinking"]["budget_tokens"] == 8192 - 4096
    assert "".join(d.content or "" for d in deltas) == "fits" and deltas[-1].finish_reason == "stop"
    assert again[-1].finish_reason == "stop"


def _stream_ok(text: str) -> bytes:
    return _anthropic_sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    )


def test_anthropic_asks_the_shared_owner_for_the_ceiling(monkeypatch) -> None:
    """The output ceiling has one owner, `send_max_tokens`, which the loop's
    reservation also reads. A second definition inside this transport disagreed
    with it on every model the catalogue knows: asking 64000 of a 32000 model,
    and half of what a 128000 model offers."""
    from raven.providers import anthropic_messages_provider as mod

    asked: list[tuple[str, int | None]] = []

    def owner(gen, model, pinned=None, allow_fetch=True):
        asked.append((model, pinned))
        return min(pinned, 32000) if pinned else 32000

    monkeypatch.setattr(mod, "send_max_tokens", owner)
    provider = AnthropicMessagesProvider(api_key="k", default_model="claude-opus-4-1")
    common = dict(messages=[{"role": "user", "content": "hi"}], tools=None, model=None, tool_choice=None, stream=True)
    body = provider._body(max_tokens=None, temperature=0.1, reasoning_effort="high", **common)
    assert asked == [("claude-opus-4-1", None)] and body["max_tokens"] == 32000
    pinned = provider._body(max_tokens=2048, temperature=0.1, reasoning_effort=None, **common)
    assert pinned["max_tokens"] == 2048


async def test_a_400_that_named_no_ceiling_teaches_none(monkeypatch) -> None:
    """A curator-shaped call pins max_tokens=2048; the model answers that its
    thinking mode is the other one. That repair leaves the pin in place, and
    remembering the pin as the model's ceiling clamped every later request to
    2048 -- a 31x reduction, silently. Only a ceiling complaint teaches."""
    from raven.providers import anthropic_messages_provider as mod

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body.get("thinking", {}).get("type") == "enabled":
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "thinking.type: enabled is not supported for this model; it thinks adaptively",
                    },
                },
            )
        return httpx.Response(200, content=_stream_ok("ok"))

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    monkeypatch.setattr(mod, "send_max_tokens", lambda gen, model, pinned=None, allow_fetch=True: pinned or 64000)
    provider = AnthropicMessagesProvider(
        api_key="k", api_base="https://api.anthropic.com/v1", default_model="claude-sonnet-4-5"
    )

    short = [
        d
        async for d in provider.chat_stream(
            [{"role": "user", "content": "curate"}], max_tokens=2048, reasoning_effort="high"
        )
    ]
    ordinary = [
        d async for d in provider.chat_stream([{"role": "user", "content": "build the page"}], reasoning_effort="high")
    ]

    assert short[-1].finish_reason == "stop" and ordinary[-1].finish_reason == "stop"
    # The ordinary turn earns the same thinking 400 once (this model is budgeted
    # by id), then goes through -- at the owner's number, not the earlier pin.
    assert [b["max_tokens"] for b in seen] == [2048, 2048, 64000, 64000]
    assert seen[1]["thinking"] == {"type": "adaptive"}
    assert provider._ceilings == {}


def test_anthropic_sends_no_temperature_to_a_model_that_thinks_by_default() -> None:
    """Claude 4.7 and later think adaptively unless told otherwise, and a
    thinking request accepts no temperature but the default; the repository's
    0.1 on the ordinary request (no reasoning effort configured) was refused."""
    from raven.providers.anthropic_messages_provider import rewrite_on_400

    quiet = _anthropic_body(model="anthropic/claude-opus-5", reasoning_effort=None, temperature=0.1)
    assert "temperature" not in quiet and "thinking" not in quiet
    older = _anthropic_body(model="claude-haiku-4-5", reasoning_effort=None, temperature=0.1)
    assert older["temperature"] == 0.1
    assert rewrite_on_400(older, "temperature may only be set to 1 when thinking is enabled") == "temperature"
    assert "temperature" not in older


async def test_the_anthropic_transport_lets_the_cache_optimizer_place_breakpoints() -> None:
    """The base default says no provider caches; this transport must say yes
    for a Claude model, or the strategy places no marks and every request of a
    150k-token design session is billed uncached."""
    from raven.token_wise.cache_optimizer import CacheOptimizer

    provider = AnthropicMessagesProvider(
        api_key="k",
        api_base="https://openrouter.ai/api/v1",
        default_model="openrouter/claude-opus-5",
        provider_name="openrouter",
    )
    assert provider.supports_prompt_caching("openrouter/claude-opus-5") is True
    assert provider.supports_prompt_caching("openrouter/gpt-5.6-sol") is False

    optimizer = CacheOptimizer(supports_caching=provider.supports_prompt_caching)
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}]
    tools = [
        {
            "type": "function",
            "function": {"name": "noop", "description": "n", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    messages, tools, model = await optimizer.before_llm_call(messages, tools, "openrouter/claude-opus-5")
    body = provider._body(
        messages=messages,
        tools=tools,
        model=model,
        max_tokens=64,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
        stream=False,
    )
    assert json.dumps(body).count("cache_control") == 3


async def test_tail_breakpoints_survive_the_anthropic_conversion() -> None:
    """The optimizer marks the last block of the last two messages. On a turn
    that only called a tool the marked text block is empty and dropped, and a
    tool result is wrapped in a tool_result block; both used to lose the mark,
    which left every iteration of a tool loop cached only up to the system
    prompt (16.8k of a 150k prefix in production)."""
    from raven.token_wise.cache_optimizer import CacheOptimizer

    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": [{"type": "text", "text": "ok"}]},
    ]
    marked, _, _ = await CacheOptimizer(supports_caching=lambda _m: True).before_llm_call(
        messages, None, "openrouter/claude-opus-5"
    )
    system, wire = convert_messages(marked)
    assert "cache_control" in system[-1]
    assistant, tool_result = wire[-2], wire[-1]
    assert assistant["content"][-1]["type"] == "tool_use" and "cache_control" in assistant["content"][-1]
    assert tool_result["content"][-1]["type"] == "tool_result" and "cache_control" in tool_result["content"][-1]
    assert not any("cache_control" in inner for inner in tool_result["content"][-1]["content"])

    # The marked turn must render exactly like the same turn once the mark has
    # moved on, or the cached prefix breaks at every iteration.
    _, unmarked = convert_messages(messages)
    assert [b for b in assistant["content"] if "cache_control" not in b] == unmarked[-2]["content"][:-1]
    assert {k: v for k, v in assistant["content"][-1].items() if k != "cache_control"} == unmarked[-2]["content"][-1]


@pytest.mark.parametrize("protocol", ["responses", "messages", "messages_stream"])
@pytest.mark.parametrize(
    "name,endpoint",
    [
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("custom", "https://gateway.example/v1"),
    ],
)
@pytest.mark.parametrize(
    "cost,expected_cost", [(0.84, 0.84), (0, 0), (None, None), (-1, None), (True, None), ("0.84", None)]
)
async def test_native_routes_preserve_reported_usage(monkeypatch, protocol, name, endpoint, cost, expected_cost):
    from raven.agent.loop.main import AgentLoop
    from raven.providers.anthropic_messages_provider import AnthropicMessagesProvider
    from raven.providers.openai_responses_provider import OpenAIResponsesProvider
    from raven.providers.streaming import stream_llm_call

    def without_missing_cost(value):
        if isinstance(value, dict):
            return {key: without_missing_cost(item) for key, item in value.items() if key != "cost" or item is not None}
        if isinstance(value, list):
            return [without_missing_cost(item) for item in value]
        return value

    def handler(request):
        if protocol == "responses":
            events = [
                {"type": "response.output_text.delta", "delta": "ok"},
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 5,
                            "total_tokens": 105,
                            "cost": cost,
                            "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 10},
                        },
                    },
                },
            ]
            return httpx.Response(200, content=_anthropic_sse(*without_missing_cost(events)))
        usage = {
            "input_tokens": 30,
            "output_tokens": 5,
            "cache_read_input_tokens": 60,
            "cache_creation_input_tokens": 10,
            "cost": cost,
        }
        if protocol == "messages":
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": without_missing_cost(usage),
                },
            )
        events = [
            {"type": "message_start", "message": {"usage": {**usage, "cost": None}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5, "cost": cost},
            },
            {"type": "message_stop"},
        ]
        return httpx.Response(200, content=_anthropic_sse(*without_missing_cost(events)))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    provider_class = OpenAIResponsesProvider if protocol == "responses" else AnthropicMessagesProvider
    provider = provider_class(api_key="test", api_base=endpoint, provider_name=name, default_model="test")
    if protocol == "messages_stream":
        response = await stream_llm_call(
            provider, messages=[{"role": "user", "content": "hi"}], tools=None, model="test"
        )
    else:
        response = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="test")
    assert response.finish_reason == "stop", response.content
    snap = AgentLoop._build_usage_snapshot(response, "test", "session")
    assert snap.cost_usd == expected_cost
    assert (snap.input_tokens, snap.output_tokens, snap.cache_read_tokens, snap.cache_write_tokens) == (30, 5, 60, 10)


async def test_responses_partial_usage_preserves_cost_and_cache():
    from raven.providers.openai_codex_provider import _consume_sse
    from raven.providers.usage import responses_usage

    events = [
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 5,
                    "cost": 0.4,
                    "input_tokens_details": {"cached_tokens": 60},
                },
            },
        },
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "usage": {
                    "cost": None,
                    "total_tokens": 105,
                    "input_tokens_details": {"cached_tokens": None},
                },
            },
        },
    ]
    sink = {}
    response = httpx.Response(200, content=_anthropic_sse(*events))
    await _consume_sse(response, 5, usage_sink=sink)
    usage = responses_usage(sink)
    assert usage["cost_usd"] == 0.4
    assert usage["cache_read_input_tokens"] == 60
    assert usage["prompt_tokens"] == 100
    assert usage["cache_creation_input_tokens"] is None


@pytest.mark.parametrize("model", ["glm-5.3-flash", "z-ai/GLM-5", "openrouter/z-ai/glm-5.3-flash", "claude-opus-5"])
def test_anthropic_model_defaults_preserve_manual_overrides(model):
    assert effective_protocol(None, model) == "anthropic"
    assert effective_protocol(_config(model).providers.custom, model) == "anthropic"
    for protocol in ("chat", "responses"):
        config = _config(model, override=protocol)
        assert effective_protocol(config.providers.custom, model) == protocol


def test_explicit_gemini_native_protocol_does_not_fall_back():
    from raven.providers.auth import MissingCredentialsError

    config = _provider_config("gemini", "gemini/gemini-2.5-pro", override="responses")
    with pytest.raises(MissingCredentialsError, match="requires an explicit API base"):
        make_provider(config)


@pytest.mark.parametrize("override", [None, "anthropic"])
def test_direct_glm_uses_vendor_anthropic_endpoint(override):
    config = _provider_config("zai", "zai/glm-4.6", override=override)
    provider = make_provider(config)
    assert isinstance(provider, AnthropicMessagesProvider)
    assert provider.api_base == "https://api.z.ai/api/anthropic"
    assert provider.api_protocol == effective_protocol(config.providers.get("zai"), "zai/glm-4.6")


def test_explicit_native_protocol_requires_vendor_address():
    from raven.providers.auth import MissingCredentialsError

    config = _provider_config("zai", "zai/glm-4.6", override="responses")
    with pytest.raises(MissingCredentialsError, match="requires an explicit API base"):
        make_provider(config)


def test_direct_glm_with_explicit_endpoint_uses_that_endpoint():
    config = _provider_config("zai", "zai/glm-4.6")
    config.providers.get("zai").api_base = "https://vendor.example/anthropic"
    provider = make_provider(config)
    assert isinstance(provider, AnthropicMessagesProvider)
    assert provider.api_base == "https://vendor.example/anthropic"


@pytest.mark.parametrize(
    "vendor,model,base",
    [
        ("deepseek", "deepseek/deepseek-v4-pro", "https://api.deepseek.com"),
        ("volcengine", "volcengine/doubao-seed-2-0-pro", "https://ark.cn-beijing.volces.com/api/v3"),
        ("minimax", "minimax/MiniMax-M3", "https://api.minimax.io/v1"),
        ("minimax_cn_api", "minimax-cn-api/MiniMax-M3", "https://api.minimax.cn/v1"),
    ],
)
def test_response_series_use_declared_vendor_addresses(vendor, model, base):
    provider = make_provider(_provider_config(vendor, model))
    assert isinstance(provider, OpenAIResponsesProvider)
    assert provider.api_base == base


@pytest.mark.parametrize(
    "vendor,model",
    [
        ("moonshot", "moonshot/kimi-k2-turbo-preview"),
        ("groq", "groq/qwen/qwen3-32b"),
        ("dashscope", "dashscope/qwen-plus"),
        ("nvidia_nim", "nvidia_nim/deepseek-ai/deepseek-v3.1"),
    ],
)
def test_an_inferred_native_protocol_steps_aside_where_the_provider_has_no_address_for_it(vendor, model):
    """The family name reads as responses, and these providers declare no
    responses address; the base tree served them over chat and the inference
    turned that into a refusal on 56 curated models of a key-only config. An
    inference is not a choice, so it steps aside; the bare inference (no
    provider named) still reads responses, which is what the tables show."""
    config = _provider_config(vendor, model)
    assert effective_protocol(None, model) == "responses"
    assert effective_protocol(config.providers.get(vendor), model, vendor) == "chat"
    provider = make_provider(config)
    assert isinstance(provider, LiteLLMProvider) and provider.api_protocol == "chat"


def test_a_chosen_native_protocol_without_an_address_is_still_refused():
    """The user's own selection is final: refusing is the right answer to an
    explicit responses on a provider with nowhere to send it."""
    from raven.providers.auth import MissingCredentialsError

    with pytest.raises(MissingCredentialsError, match="requires an explicit API base"):
        make_provider(_provider_config("moonshot", "moonshot/kimi-k2-turbo-preview", override="responses"))


def test_a_gateway_declaring_some_protocols_does_not_serve_the_others_at_its_default_address():
    """volcengine names a responses address only. The gateway fallback handed
    that address to an anthropic request as well, so a glm model on volcengine
    posted /v1/messages to a responses endpoint, and the picker could not warn
    because the fallback always answered. A gateway that declares nothing per
    protocol (openrouter) still serves every protocol at its one address."""
    from raven.providers.protocol import native_api_base

    assert native_api_base("volcengine", "responses") == "https://ark.cn-beijing.volces.com/api/v3"
    assert native_api_base("volcengine", "anthropic") is None
    assert native_api_base("openrouter", "responses") == "https://openrouter.ai/api/v1"
    assert native_api_base("openrouter", "anthropic") == "https://openrouter.ai/api/v1"
    config = _provider_config("volcengine", "volcengine/glm-4-7-251222")
    provider = make_provider(config)
    assert isinstance(provider, LiteLLMProvider) and provider.api_protocol == "chat"
    # The picker's half of the same-answer claim: it resolves through the same
    # call, provider named, so its entry says chat too and carries no warning.
    # Dropping the provider name at the picker alone would read anthropic here.
    entry = _picker_entry("volcengine", config)
    assert entry["protocols"]["volcengine/glm-4-7-251222"] == "chat" and entry["warning"] == ""


def _picker_entry(vendor: str, config: Config) -> dict:
    """The model picker's row for ``vendor``, built off the configured section."""
    from raven.rpc.methods.model import _build_provider_entry

    return _build_provider_entry(
        vendor,
        current_provider=None,
        providers={vendor: {"name": vendor, "configured": True}},
        section=config.providers.get(vendor),
    )


def _pool_config(vendor: str, model: str, endpoints: list[dict]) -> Config:
    return Config.model_validate(
        {
            "agents": {"defaults": {"model": model, "provider": vendor}},
            "providers": {vendor: {"endpoints": endpoints}},
        }
    )


def test_a_mixed_endpoint_pool_stays_on_chat_until_every_member_has_an_address():
    """One addressed endpoint is not permission to build a native client for the
    whole rotor: the key-only member has nowhere to send responses, and the
    rotor failed eagerly while constructing it. The inference steps aside
    unless every endpoint can be served."""
    from raven.providers.endpoint_rotor import EndpointRotorProvider

    model = "moonshot/kimi-k2-turbo-preview"
    mixed = _pool_config(
        "moonshot",
        model,
        [
            {"label": "bare", "apiKey": "k1"},
            {"label": "addressed", "apiKey": "k2", "apiBase": "https://kimi.example/v1"},
        ],
    )
    assert effective_protocol(mixed.providers.get("moonshot"), model, "moonshot") == "chat"
    assert _picker_entry("moonshot", mixed)["protocols"][model] == "chat"
    provider = make_provider(mixed)
    assert isinstance(provider, EndpointRotorProvider)

    addressed = _pool_config(
        "moonshot",
        model,
        [
            {"label": "a", "apiKey": "k1", "apiBase": "https://kimi-a.example/v1"},
            {"label": "b", "apiKey": "k2", "apiBase": "https://kimi-b.example/v1"},
        ],
    )
    # The picker and the factory read the same endpoint set, so what one shows
    # is what the other builds: responses here, from every member's address.
    assert effective_protocol(addressed.providers.get("moonshot"), model, "moonshot") == "responses"
    assert _picker_entry("moonshot", addressed)["protocols"][model] == "responses"
    assert isinstance(make_provider(addressed), EndpointRotorProvider)


def test_display_resolution_logs_nothing_and_construction_logs_once():
    """The model picker resolves every catalogue row through effective_protocol;
    an INFO line per fallback there was 86 records per all-provider row build.
    The runtime decision -- a client actually built over chat -- is logged once."""
    from loguru import logger

    records: list[str] = []
    sink = logger.add(lambda message: records.append(str(message)), level="INFO")
    try:
        for vendor, model in (("dashscope", "dashscope/qwen-plus"), ("moonshot", "moonshot/kimi-k2-turbo-preview")):
            section = _provider_config(vendor, model).providers.get(vendor)
            assert effective_protocol(section, model, vendor) == "chat"
        assert records == []
        make_provider(_provider_config("moonshot", "moonshot/kimi-k2-turbo-preview"))
        assert sum("using chat" in r for r in records) == 1
        # An operator's own chat choice is not a stepping-aside, and the provider
        # it names is not missing an address: nothing to report.
        make_provider(_provider_config("anthropic", "anthropic/claude-opus-5", override="chat"))
        assert sum("using chat" in r for r in records) == 1
    finally:
        logger.remove(sink)


def test_gemini_defaults_to_litellm_chat_without_an_endpoint():
    provider = make_provider(_provider_config("gemini", "gemini/gemini-2.5-flash"))
    assert isinstance(provider, LiteLLMProvider)
    assert provider.api_protocol == "chat"
