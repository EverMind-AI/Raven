"""Shape tests for OpenAICodexProvider (Responses API), no live call / no key.

Pins that this provider targets the OpenAI Responses endpoint and sends the
experimental Responses beta header — so a switch away from the Responses API
trips a test.
"""

from __future__ import annotations

from raven.providers.openai_codex_provider import (
    DEFAULT_CODEX_URL,
    OpenAICodexProvider,
    _build_headers,
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
