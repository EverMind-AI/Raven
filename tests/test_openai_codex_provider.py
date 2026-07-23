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
