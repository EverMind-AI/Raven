"""Shape tests for OpenAICodexProvider (Responses API), no live call / no key.

Pins that this provider targets the OpenAI Responses endpoint and sends the
experimental Responses beta header — so a switch away from the Responses API
trips a test.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import oauth_cli_kit
import pytest

from raven.providers import openai_codex_provider as codex_module
from raven.providers.base import LLMProvider
from raven.providers.openai_codex_provider import (
    DEFAULT_CODEX_URL,
    OpenAICodexProvider,
    _build_headers,
    _consume_sse,
    _convert_messages,
    _convert_tool_output,
    _iter_sse,
)


def test_default_url_targets_codex_responses_endpoint():
    assert DEFAULT_CODEX_URL == "https://chatgpt.com/backend-api/codex/responses"
    assert DEFAULT_CODEX_URL.endswith("/codex/responses")


def test_headers_declare_experimental_responses_beta():
    headers = _build_headers(account_id="acct-123", token="tok-abc")
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["Authorization"] == "Bearer tok-abc"
    assert headers["chatgpt-account-id"] == "acct-123"
    assert headers["accept"] == "text/event-stream"


def test_provider_default_model_is_codex():
    provider = OpenAICodexProvider(default_model="openai-codex/gpt-5.1-codex")
    assert provider.get_default_model() == "openai-codex/gpt-5.1-codex"
    # OAuth-based: constructed without an API key.
    assert provider.api_key is None


class _FakeStreamResponse:
    """SSE response stand-in: emits complete events, then stalls forever."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        await asyncio.sleep(10)


def _sse_response(event: dict) -> _FakeStreamResponse:
    return _FakeStreamResponse([f"data: {json.dumps(event)}", ""])


_OVERLOADED_ERROR = {
    "type": "service_unavailable_error",
    "code": "server_is_overloaded",
    "message": "Our servers are currently overloaded. Please try again later.",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {"type": "error", "error": _OVERLOADED_ERROR},
        {"type": "response.failed", "response": {"error": _OVERLOADED_ERROR}},
    ],
)
async def test_consume_sse_preserves_structured_error_details(event: dict):
    with pytest.raises(RuntimeError) as raised:
        await _consume_sse(_sse_response(event), timeout=0.05)

    message = str(raised.value)
    assert "service_unavailable_error" in message
    assert "server_is_overloaded" in message
    assert _OVERLOADED_ERROR["message"] in message

    classification = LLMProvider.classify_error(raised.value)
    assert classification.category == "server"
    assert classification.retryable is True
    assert classification.should_fallback is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        {"type": "service_unavailable_error"},
        {"code": "server_is_overloaded"},
    ],
)
async def test_consume_sse_classifies_sparse_capacity_errors_as_retryable(error: dict):
    with pytest.raises(RuntimeError) as raised:
        await _consume_sse(_sse_response({"type": "error", "error": error}), timeout=0.05)

    classification = LLMProvider.classify_error(raised.value)
    assert classification.category == "server"
    assert classification.retryable is True
    assert classification.should_fallback is True


@pytest.mark.asyncio
async def test_consume_sse_bounds_error_fields_and_ignores_nested_values():
    event = {
        "type": "error",
        "error": {
            "type": {"unexpected": "nested"},
            "code": "x" * 1_000,
            "message": "line one\n\x1b[31m" + "m" * 2_000,
            "request": {"authorization": "must not be serialized"},
        },
    }

    with pytest.raises(RuntimeError) as raised:
        await _consume_sse(_sse_response(event), timeout=0.05)

    message = str(raised.value)
    assert "unexpected" not in message
    assert "authorization" not in message
    assert "\n" not in message
    assert "\x1b" not in message
    assert "x" * 100 in message
    assert "line one" in message
    assert len(message) < 1_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {"type": "error", "error": "unstructured failure"},
        {"type": "response.failed", "response": None},
    ],
)
async def test_consume_sse_uses_generic_message_for_malformed_error(event: dict):
    with pytest.raises(RuntimeError, match=r"^Codex response failed$"):
        await _consume_sse(_sse_response(event), timeout=0.05)


@pytest.mark.asyncio
async def test_consume_sse_keeps_unrelated_error_non_retryable():
    event = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_prompt",
            "message": "Invalid request: malformed input",
        },
    }

    with pytest.raises(RuntimeError) as raised:
        await _consume_sse(_sse_response(event), timeout=0.05)

    classification = LLMProvider.classify_error(raised.value)
    assert classification.category == "invalid_request"
    assert classification.retryable is False
    assert classification.should_fallback is False


def _patch_codex_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        oauth_cli_kit,
        "get_token",
        lambda: SimpleNamespace(account_id="acct-test", access="token-test"),
    )


@pytest.mark.asyncio
async def test_codex_sse_overload_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    _patch_codex_token(monkeypatch)
    calls = 0

    async def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await _consume_sse(
                _sse_response({"type": "response.failed", "response": {"error": _OVERLOADED_ERROR}}),
                timeout=0.05,
            )
        return "recovered", [], "stop"

    monkeypatch.setattr(codex_module, "_request_codex", request)
    provider = OpenAICodexProvider()
    provider._CHAT_RETRY_DELAYS = (0,)

    response = await provider.chat_with_retry(messages=[], model=provider.default_model)

    assert response.content == "recovered"
    assert response.finish_reason == "stop"
    assert calls == 2


@pytest.mark.asyncio
async def test_codex_sse_overload_exhausts_retry_ladder(monkeypatch: pytest.MonkeyPatch):
    _patch_codex_token(monkeypatch)
    calls = 0

    async def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await _consume_sse(
            _sse_response({"type": "error", "error": _OVERLOADED_ERROR}),
            timeout=0.05,
        )

    monkeypatch.setattr(codex_module, "_request_codex", request)
    provider = OpenAICodexProvider()
    provider._CHAT_RETRY_DELAYS = (0, 0, 0)

    response = await provider.chat_with_retry(messages=[], model=provider.default_model)

    assert response.finish_reason == "error"
    assert response.error_classification is not None
    assert response.error_classification.category == "server"
    assert response.error_classification.retryable is True
    assert "server_is_overloaded" in response.content
    assert calls == 4


@pytest.mark.asyncio
async def test_iter_sse_per_event_idle_timeout_raises():
    """A stream that stalls after a complete event trips the per-event idle cap
    instead of hanging (httpx's per-read timeout would reset on the trickle)."""
    resp = _FakeStreamResponse(['data: {"type": "ping"}', ""])
    events = []
    with pytest.raises(TimeoutError):
        async for event in _iter_sse(resp, timeout=0.05):
            events.append(event)
    assert events == [{"type": "ping"}]


_TINY_PNG_URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


def test_convert_tool_output_passes_plain_string_through():
    assert _convert_tool_output("file contents") == "file contents"


def test_convert_tool_output_joins_text_only_blocks_without_json():
    blocks = [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]
    assert _convert_tool_output(blocks) == "line one\nline two"


def test_convert_tool_output_emits_responses_array_when_an_image_is_present():
    blocks = [
        {"type": "text", "text": "screenshot of the dashboard"},
        {"type": "image_url", "image_url": {"url": _TINY_PNG_URI}},
    ]
    assert _convert_tool_output(blocks) == [
        {"type": "input_text", "text": "screenshot of the dashboard"},
        {"type": "input_image", "image_url": _TINY_PNG_URI, "detail": "auto"},
    ]


def test_convert_tool_output_never_serializes_base64_as_prose():
    """Regression: the old fallback json.dumps'd the block list, so the model got
    the image's base64 payload as text instead of a picture -- silently."""
    blocks = [{"type": "image_url", "image_url": {"url": _TINY_PNG_URI}}]
    out = _convert_tool_output(blocks)

    assert isinstance(out, list)
    assert not any("base64" in part.get("text", "") for part in out)
    assert out == [{"type": "input_image", "image_url": _TINY_PNG_URI, "detail": "auto"}]


def test_convert_tool_output_keeps_unknown_blocks_reaching_the_model():
    blocks = [{"type": "citation", "source": "docs"}]
    assert _convert_tool_output(blocks) == '{"type": "citation", "source": "docs"}'


def test_convert_messages_wires_tool_output_into_function_call_output():
    _, items = _convert_messages(
        [
            {
                "role": "tool",
                "tool_call_id": "call_7|fc_7",
                "content": [
                    {"type": "text", "text": "here it is"},
                    {"type": "image_url", "image_url": {"url": _TINY_PNG_URI}},
                ],
            }
        ]
    )

    assert len(items) == 1
    assert items[0]["type"] == "function_call_output"
    assert items[0]["call_id"] == "call_7"
    assert items[0]["output"][1]["type"] == "input_image"
