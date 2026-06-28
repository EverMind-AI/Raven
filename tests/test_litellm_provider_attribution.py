"""Tests for OpenRouter app attribution headers injected by LiteLLMProvider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from raven.providers.litellm_provider import _OPENROUTER_ATTRIBUTION, LiteLLMProvider


def _make_provider(provider_name: str, extra_headers: dict | None = None) -> LiteLLMProvider:
    with patch("raven.providers.litellm_provider.litellm"), patch(
        "raven.providers.litellm_provider.LiteLLMProvider._setup_env"
    ):
        return LiteLLMProvider(
            api_key="sk-test",
            provider_name=provider_name,
            extra_headers=extra_headers,
        )


def test_openrouter_injects_all_attribution_headers():
    provider = _make_provider("openrouter")
    assert provider.extra_headers["HTTP-Referer"] == "https://raven.evermind.ai"
    assert provider.extra_headers["X-Title"] == "Raven Agent"
    assert provider.extra_headers["X-OpenRouter-Title"] == "Raven Agent"
    assert provider.extra_headers["X-OpenRouter-Categories"] == "cli-agent,personal-agent"


def test_openrouter_user_headers_override_defaults():
    provider = _make_provider("openrouter", extra_headers={"X-OpenRouter-Title": "Custom"})
    assert provider.extra_headers["X-OpenRouter-Title"] == "Custom"
    assert provider.extra_headers["HTTP-Referer"] == "https://raven.evermind.ai"


def test_non_openrouter_provider_has_no_attribution():
    provider = _make_provider("anthropic")
    assert "X-OpenRouter-Title" not in provider.extra_headers
    assert "HTTP-Referer" not in provider.extra_headers
    assert "X-OpenRouter-Categories" not in provider.extra_headers
