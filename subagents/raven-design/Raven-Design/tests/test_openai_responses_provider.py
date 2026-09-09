"""Contract tests for the API-key OpenAI Responses transport."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from raven.providers._responses import consume_response_events
from raven.providers.capabilities import supports_image_tool_result
from raven.providers.openai_responses_provider import OpenAIResponsesProvider

_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


class _Stream:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = iter(events)
        self.closed = False

    def __aiter__(self) -> "_Stream":
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def close(self) -> None:
        self.closed = True


class _Responses:
    def __init__(self, steps: list[Any]) -> None:
        self.steps = list(steps)
        self.bodies: list[dict[str, Any]] = []
        self.streams: list[_Stream] = []

    async def create(self, **body: Any) -> Any:
        self.bodies.append(deepcopy(body))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, str):
            return step
        stream = _Stream(step)
        self.streams.append(stream)
        return stream


def _provider(*steps: Any, model: str = "openai/gpt-5.5") -> tuple[OpenAIResponsesProvider, _Responses]:
    responses = _Responses(list(steps))
    provider = OpenAIResponsesProvider(
        api_key="sk-test",
        api_base="https://example.test/v1",
        default_model=model,
        extra_headers={"X-Test": "yes"},
        provider_name="openai",
    )
    provider._client = SimpleNamespace(responses=responses)  # type: ignore[assignment]
    return provider, responses


def _completed(
    response_id: str,
    *,
    output: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "status": "completed",
            "output": output or [],
            "usage": usage,
        },
    }


def _text_events(response_id: str, text: str = "done") -> list[dict[str, Any]]:
    return [
        {"type": "response.output_text.delta", "delta": text},
        _completed(response_id),
    ]


def _tool_events(response_id: str) -> list[dict[str, Any]]:
    item_id = "fc_123456789012345"
    call_id = "call_123"
    return [
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": item_id,
                "call_id": call_id,
                "name": "preview_file",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": item_id,
            "delta": '{"path":"report.pdf"}',
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": item_id,
                "call_id": call_id,
                "name": "preview_file",
                "arguments": '{"path":"report.pdf"}',
            },
        },
        _completed(
            response_id,
            usage={
                "input_tokens": 30,
                "output_tokens": 8,
                "total_tokens": 38,
                "input_tokens_details": {"cached_tokens": 20},
            },
        ),
    ]


@pytest.mark.asyncio
async def test_standard_response_collects_text_reasoning_usage_and_closes_stream() -> None:
    events = [
        {"type": "response.reasoning_summary_text.delta", "delta": "checked"},
        {"type": "response.output_text.delta", "delta": "hello"},
        _completed(
            "resp_1",
            usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        ),
    ]
    provider, transport = _provider(events)

    response = await provider.chat([{"role": "user", "content": "hi"}])

    assert response.content == "hello"
    assert response.reasoning_content == "checked"
    assert response.response_id == "resp_1"
    assert response.usage == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    assert transport.streams[0].closed is True


@pytest.mark.asyncio
async def test_refusal_text_is_not_dropped_from_stream_or_terminal_response() -> None:
    terminal = _completed(
        "resp_refusal",
        output=[
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "cannot comply"}],
            }
        ],
    )
    provider, _ = _provider(
        [
            {"type": "response.refusal.delta", "delta": "cannot comply"},
            terminal,
        ],
        [terminal],
    )

    streamed = await provider.chat([{"role": "user", "content": "unsafe request"}])
    terminal_only = await provider.chat([{"role": "user", "content": "unsafe request"}])

    assert streamed.content == "cannot comply"
    assert terminal_only.content == "cannot comply"


@pytest.mark.asyncio
async def test_tool_result_continues_by_response_id_and_keeps_the_image() -> None:
    final_output = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "the preview is blue"}],
        }
    ]
    provider, transport = _provider(_tool_events("resp_tool"), [_completed("resp_final", output=final_output)])
    continuation = provider.create_response_continuation()
    initial = [
        {"role": "system", "content": "inspect the rendered file"},
        {"role": "user", "content": "open report.pdf"},
    ]

    with provider.response_continuation(continuation):
        first = await provider.chat(initial)
        messages = [
            *initial,
            {
                "role": "assistant",
                "content": first.content,
                "tool_calls": [call.to_openai_tool_call() for call in first.tool_calls],
            },
            {
                "role": "tool",
                "tool_call_id": first.tool_calls[0].id,
                "content": [
                    {"type": "text", "text": "rendered preview"},
                    {"type": "image_url", "image_url": {"url": _PNG}},
                ],
            },
        ]
        second = await provider.chat(messages)

    assert first.finish_reason == "tool_calls"
    assert first.usage["cache_read_input_tokens"] == 20
    assert second.content == "the preview is blue"
    assert transport.bodies[1]["previous_response_id"] == "resp_tool"
    assert transport.bodies[1]["instructions"] == "inspect the rendered file"
    assert transport.bodies[1]["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": [
                {"type": "input_text", "text": "rendered preview"},
                {"type": "input_image", "image_url": _PNG, "detail": "auto"},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_rejected_continuation_retries_once_with_full_input() -> None:
    provider, transport = _provider(
        _tool_events("resp_tool"),
        RuntimeError("previous_response_id is not supported"),
        _text_events("resp_final"),
    )
    state = provider.create_response_continuation()
    initial = [{"role": "user", "content": "use a tool"}]

    with provider.response_continuation(state):
        first = await provider.chat(initial)
        messages = [
            *initial,
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [call.to_openai_tool_call() for call in first.tool_calls],
            },
            {"role": "tool", "tool_call_id": first.tool_calls[0].id, "content": "ok"},
        ]
        response = await provider.chat(messages)

    assert response.content == "done"
    assert "previous_response_id" in transport.bodies[1]
    assert "previous_response_id" not in transport.bodies[2]
    assert len(transport.bodies[2]["input"]) == 3
    assert state.enabled is False


@pytest.mark.asyncio
async def test_gpt56_omits_sampling_and_output_limit_but_converts_tool_choice() -> None:
    provider, transport = _provider(_text_events("resp_56"), model="openai/gpt-5.6-sol")
    tool = {
        "type": "function",
        "function": {"name": "echo", "description": "echo", "parameters": {"type": "object"}},
    }

    await provider.chat(
        [{"role": "user", "content": "echo"}],
        tools=[tool],
        max_tokens=99,
        temperature=0.2,
        tool_choice={"type": "function", "function": {"name": "echo"}},
    )

    body = transport.bodies[0]
    assert body["model"] == "gpt-5.6-sol"
    assert "temperature" not in body
    assert "max_output_tokens" not in body
    assert body["tool_choice"] == {"type": "function", "name": "echo"}


@pytest.mark.asyncio
async def test_older_model_keeps_sampling_and_explicit_output_limit() -> None:
    provider, transport = _provider(_text_events("resp_old"), model="openai/gpt-5.5")

    await provider.chat([{"role": "user", "content": "hi"}], max_tokens=77, temperature=0.3)

    assert transport.bodies[0]["model"] == "gpt-5.5"
    assert transport.bodies[0]["temperature"] == 0.3
    assert transport.bodies[0]["max_output_tokens"] == 77


@pytest.mark.asyncio
async def test_openai_sdk_posts_the_responses_wire_shape() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["extra_header"] = request.headers.get("x-test")
        captured["body"] = json.loads(request.content)
        stream = (
            'data: {"type":"response.output_text.delta","delta":"wire ok"}\n\n'
            'data: {"type":"response.completed","response":'
            '{"id":"resp_wire","status":"completed","output":[],"usage":null}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    provider = OpenAIResponsesProvider(
        api_key="sk-wire",
        api_base="https://example.test/v1",
        default_model="openai/gpt-5.5",
        extra_headers={"X-Test": "yes"},
        provider_name="openai",
    )
    await provider._client.close()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider._client = AsyncOpenAI(
        api_key="sk-wire",
        base_url="https://example.test/v1",
        default_headers={"X-Test": "yes"},
        http_client=http_client,
    )

    try:
        response = await provider.chat(
            [{"role": "system", "content": "be concise"}, {"role": "user", "content": "ping"}]
        )
    finally:
        await provider._client.close()

    assert response.content == "wire ok"
    assert response.response_id == "resp_wire"
    assert captured["url"] == "https://example.test/v1/responses"
    assert captured["authorization"] == "Bearer sk-wire"
    assert captured["extra_header"] == "yes"
    assert captured["body"]["model"] == "gpt-5.5"
    assert captured["body"]["instructions"] == "be concise"
    assert captured["body"]["stream"] is True


@pytest.mark.asyncio
async def test_repaired_tool_arguments_are_marked_for_safe_dispatch() -> None:
    async def events():
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_123456789012345",
                "call_id": "call_1",
                "name": "write_file",
                "arguments": '{"path":"a.py","content":"cut',
            },
        }
        yield _completed("resp_cut")

    response = await consume_response_events(events())

    assert response.tool_calls[0].arguments["content"] == "cut"
    assert response.tool_calls[0].run_meta is not None
    assert response.tool_calls[0].run_meta.arguments_repaired is True


def test_responses_transport_reuses_codex_message_conversion() -> None:
    from raven.providers.openai_codex_provider import _convert_messages, _convert_tools, _split_tool_call_id
    from raven.providers.openai_responses_provider import convert_messages, convert_tools, split_tool_call_id

    assert convert_messages is _convert_messages
    assert convert_tools is _convert_tools
    assert split_tool_call_id is _split_tool_call_id


def test_responses_transport_declares_native_tool_result_images() -> None:
    provider, _ = _provider(_text_events("unused"))

    assert supports_image_tool_result(provider, "openai/gpt-5.5") is True


def test_default_responses_provider_is_scoped_to_openai_models() -> None:
    provider = OpenAIResponsesProvider()

    try:
        assert provider.provider_name == "openai"
        assert provider.can_serve("openai/gpt-5.5") is True
        assert provider.can_serve("anthropic/claude-sonnet-5") is False
    finally:
        asyncio.run(provider._client.close())


@pytest.mark.parametrize(
    ("provider_name", "stored_model", "wire_model"),
    [
        ("openai", "openai/gpt-5.5", "gpt-5.5"),
        ("custom", "custom/acme/model", "acme/model"),
        ("openrouter", "openrouter/openai/gpt-5.5", "openai/gpt-5.5"),
        ("acme_cloud", "acme-cloud/team/model", "team/model"),
    ],
)
def test_responses_transport_uses_the_central_wire_model_rule(
    provider_name: str,
    stored_model: str,
    wire_model: str,
) -> None:
    provider = OpenAIResponsesProvider(provider_name=provider_name)

    assert provider.wire_model_id(stored_model) == wire_model
