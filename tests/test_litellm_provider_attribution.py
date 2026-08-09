"""Tests for OpenRouter app attribution headers injected by LiteLLMProvider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from raven.providers.litellm_provider import _ANTHROPIC_EXTRA_KEYS, LiteLLMProvider


def _make_provider(provider_name: str, extra_headers: dict | None = None) -> LiteLLMProvider:
    with (
        patch("raven.providers.litellm_provider.litellm"),
        patch("raven.providers.litellm_provider.LiteLLMProvider._setup_env"),
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


# --- anthropic wire-format branch of _extra_msg_keys (keyless, no live call) ---
# Pins the branch at litellm_provider.py `_extra_msg_keys`: the anthropic
# spec / a "claude" model / an "anthropic/"-prefixed resolved model preserves
# the `thinking_blocks` message key; everything else preserves nothing.


def test_extra_msg_keys_anthropic_spec_preserves_thinking_blocks():
    keys = LiteLLMProvider._extra_msg_keys("anthropic/claude-opus-4-5", "anthropic/claude-opus-4-5")
    assert keys == _ANTHROPIC_EXTRA_KEYS
    assert keys == frozenset({"thinking_blocks"})


def test_extra_msg_keys_matches_on_claude_in_original_model():
    assert LiteLLMProvider._extra_msg_keys("claude-3", "openai/claude-3") == _ANTHROPIC_EXTRA_KEYS


def test_extra_msg_keys_matches_on_resolved_anthropic_prefix():
    assert LiteLLMProvider._extra_msg_keys("some-alias", "anthropic/foo") == _ANTHROPIC_EXTRA_KEYS


def test_extra_msg_keys_non_anthropic_preserves_nothing():
    assert LiteLLMProvider._extra_msg_keys("gpt-4o", "gpt-4o") == frozenset()


# --- orphan <think> recovery in _parse_response (issue #152, keyless, no live call) ---
# A backend run without a reasoning parser swallows the opening tag into its
# prompt template and returns bare reasoning text + a lone `</think>`. That
# shape only comes from a self-hosted inference server (hosted_vllm / custom /
# no spec at all) -- see `LiteLLMProvider.emits_unparsed_reasoning` -- so the
# split-fires cases below are built under one of those identities. A direct
# connection to a known hosted vendor (anthropic) or a real network gateway
# (openrouter) never gets normalized: a bare `</think>` in their content is
# just content, not a leaked prompt template.


def _fake_response(content: str, reasoning_content: str | None = None) -> MagicMock:
    message = MagicMock(content=content, tool_calls=None, reasoning_content=reasoning_content, thinking_blocks=None)
    choice = MagicMock(message=message, finish_reason="stop")
    return MagicMock(choices=[choice], usage=None)


def test_parse_response_splits_orphan_think_into_reasoning():
    provider = _make_provider("hosted_vllm")
    response = _fake_response("raw reasoning text</think>\nfinal answer")

    result = provider._parse_response(response)

    assert result.reasoning_content == "raw reasoning text"
    assert result.content == "final answer"


def test_parse_response_splits_orphan_think_for_custom_endpoint():
    provider = _make_provider("custom")
    response = _fake_response("raw reasoning text</think>\nfinal answer")

    result = provider._parse_response(response)

    assert result.reasoning_content == "raw reasoning text"
    assert result.content == "final answer"


def test_parse_response_leaves_structured_reasoning_content_alone():
    provider = _make_provider("hosted_vllm")
    response = _fake_response("visible</think>\nanswer", reasoning_content="already structured")

    result = provider._parse_response(response)

    assert result.reasoning_content == "already structured"
    assert result.content == "visible</think>\nanswer"


def test_parse_response_leaves_bare_close_tag_alone_for_direct_anthropic():
    provider = _make_provider("anthropic")
    response = _fake_response("discussing the </think> tag in my answer")

    result = provider._parse_response(response)

    assert result.reasoning_content is None
    assert result.content == "discussing the </think> tag in my answer"


def test_parse_response_leaves_bare_close_tag_alone_behind_a_gateway():
    provider = _make_provider("openrouter")
    response = _fake_response("discussing the </think> tag in my answer")

    result = provider._parse_response(response)

    assert result.reasoning_content is None
    assert result.content == "discussing the </think> tag in my answer"
