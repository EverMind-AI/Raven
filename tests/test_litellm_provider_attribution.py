"""Tests for OpenRouter app attribution headers injected by LiteLLMProvider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from raven.providers.litellm_provider import _ANTHROPIC_EXTRA_KEYS, LiteLLMProvider


def _make_provider(
    provider_name: str,
    extra_headers: dict | None = None,
    unparsed_reasoning: bool | None = None,
) -> LiteLLMProvider:
    with (
        patch("raven.providers.litellm_provider.litellm"),
        patch("raven.providers.litellm_provider.LiteLLMProvider._setup_env"),
    ):
        return LiteLLMProvider(
            api_key="sk-test",
            provider_name=provider_name,
            extra_headers=extra_headers,
            unparsed_reasoning=unparsed_reasoning,
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


# --- orphan <think> recovery in _parse_response (keyless, no live call) ---
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


def test_parse_response_leaves_bare_close_tag_alone_for_an_unresolved_identity():
    """An identity that resolves to no spec says nothing about the backend.

    The proactive planner and the evolver both build direct big-vendor
    connections with no provider_name at all; reading "no spec" as
    "self-hosted" re-opened the ordinary-content cut on exactly those
    constructors, so the gate answers False there -- same reading as
    can_serve's.
    """
    provider = _make_provider("fireworks")
    response = _fake_response("discussing the </think> tag in my answer")

    result = provider._parse_response(response)

    assert result.reasoning_content is None
    assert result.content == "discussing the </think> tag in my answer"


# --- explicit unparsed_reasoning override (Should-fix 9) ---
# "custom" is one name for two things: the generic self-hosted inference server
# this normalization exists for, and (per_model_provider._endpoint_provider) the
# api_base/api_key shape a knn-routed endpoint borrows without any claim about
# what backend sits behind it. `unparsed_reasoning=None` (the default) keeps
# deriving the answer from the spec exactly as before; an explicit bool wins
# outright, which is the seam that lets the two meanings of "custom" diverge.


def test_unparsed_reasoning_defaults_to_the_spec_derived_guess():
    provider = _make_provider("custom")
    assert provider.emits_unparsed_reasoning() is True


def test_unparsed_reasoning_explicit_false_overrides_a_true_guess():
    provider = _make_provider("custom", unparsed_reasoning=False)
    assert provider.emits_unparsed_reasoning() is False


def test_unparsed_reasoning_explicit_true_overrides_a_false_guess():
    provider = _make_provider("anthropic", unparsed_reasoning=True)
    assert provider.emits_unparsed_reasoning() is True


def test_parse_response_respects_an_explicit_false_override():
    provider = _make_provider("custom", unparsed_reasoning=False)
    response = _fake_response("discussing the </think> tag in my answer")

    result = provider._parse_response(response)

    assert result.reasoning_content is None
    assert result.content == "discussing the </think> tag in my answer"


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

# --- cache-control gate: the address is the wire, not the model id ---


def _make_base_only_provider(api_base: str | None) -> LiteLLMProvider:
    """A constructor shape several production callers use: api_base, no name.

    The evolver's launch models and the sentinel planner both build this way,
    so the gate must answer from the auto-detected gateway, not the model id.
    """
    with (
        patch("raven.providers.litellm_provider.litellm"),
        patch("raven.providers.litellm_provider.LiteLLMProvider._setup_env"),
    ):
        return LiteLLMProvider(api_key="sk-test", api_base=api_base)


def test_cache_gate_asks_the_detected_gateway_when_no_name_was_given():
    """An anthropic model id through a caching-less gateway must not carry
    cache_control: the id alone reads as Anthropic's wire, but the request
    travels on whatever the api_base names."""
    p = _make_base_only_provider("https://aihubmix.com/v1")
    assert p._supports_cache_control("anthropic/claude-sonnet-4-20250514") is False


def test_cache_gate_still_allows_a_caching_gateway_and_direct_anthropic():
    direct = _make_base_only_provider(None)
    routed = _make_base_only_provider("https://openrouter.ai/api/v1")
    assert direct._supports_cache_control("anthropic/claude-sonnet-4-20250514") is True
    assert routed._supports_cache_control("anthropic/claude-sonnet-4-20250514") is True
