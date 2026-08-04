"""Shape tests for OpenAICodexProvider (Responses API), no live call / no key.

Pins that this provider targets the OpenAI Responses endpoint and sends the
experimental Responses beta header — so a switch away from the Responses API
trips a test.
"""

from __future__ import annotations

import asyncio

import pytest

from raven.providers.openai_codex_provider import (
    DEFAULT_CODEX_URL,
    OpenAICodexProvider,
    _build_headers,
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


def test_provider_default_model_is_auto():
    provider = OpenAICodexProvider()
    assert provider.get_default_model() == "openai-codex/auto"
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
