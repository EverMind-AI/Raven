"""Copilot ids that only speak /responses are sent through LiteLLM's Responses bridge.

GitHub Copilot answers ``/chat/completions`` for its newest GPT ids with HTTP 400
(``model "gpt-5.6-terra" is not accessible via the /chat/completions endpoint``).
LiteLLM's Copilot driver implements the Responses API, and a ``responses/`` segment
after the provider prefix is what routes a call through it.
"""

from __future__ import annotations

import pytest

from raven.providers.wire import wire_model


@pytest.mark.parametrize(
    "stored, expected",
    [
        ("github_copilot/gpt-5.6-terra", "github_copilot/responses/gpt-5.6-terra"),
        ("github-copilot/gpt-5.6-terra", "github_copilot/responses/gpt-5.6-terra"),
        ("github_copilot/gpt-5.6-sol", "github_copilot/responses/gpt-5.6-sol"),
        ("github_copilot/gpt-5.6-luna", "github_copilot/responses/gpt-5.6-luna"),
        ("github_copilot/gpt-5.5", "github_copilot/responses/gpt-5.5"),
        ("github_copilot/gpt-6-luna", "github_copilot/responses/gpt-6-luna"),
        ("github_copilot/gpt-6.1-terra", "github_copilot/responses/gpt-6.1-terra"),
        ("github_copilot/gpt-6.1-sol", "github_copilot/responses/gpt-6.1-sol"),
        ("github_copilot/gpt-5.3-codex", "github_copilot/responses/gpt-5.3-codex"),
    ],
)
def test_responses_only_copilot_ids_take_the_responses_bridge(stored: str, expected: str) -> None:
    assert wire_model(stored) == expected


@pytest.mark.parametrize(
    "stored, expected",
    [
        # Chat-capable Copilot ids keep the plain chat route.
        ("github_copilot/gpt-4o", "github_copilot/gpt-4o"),
        ("github_copilot/gpt-4.1", "github_copilot/gpt-4.1"),
        ("github_copilot/gpt-5-mini", "github_copilot/gpt-5-mini"),
        ("github_copilot/gpt-5.4", "github_copilot/gpt-5.4"),
        ("github_copilot/claude-sonnet-5.5", "github_copilot/claude-sonnet-5.5"),
        ("github_copilot/claude-haiku-5.5", "github_copilot/claude-haiku-5.5"),
        ("github_copilot/gemini-4-argon", "github_copilot/gemini-4-argon"),
    ],
)
def test_chat_capable_copilot_ids_stay_on_chat(stored: str, expected: str) -> None:
    assert wire_model(stored) == expected


@pytest.mark.parametrize(
    "stored",
    [
        "openai/gpt-5.6-terra",
        "gpt-5.6-terra",
        "openrouter/openai/gpt-5.6-terra",
    ],
)
def test_other_providers_are_not_given_the_responses_segment(stored: str) -> None:
    assert "/responses/" not in wire_model(stored)


def test_the_responses_segment_is_not_added_twice() -> None:
    once = wire_model("github_copilot/gpt-5.6-terra")
    assert wire_model(once) == once


@pytest.mark.parametrize("model", ["github_copilot/gpt-6-luna", "github_copilot/gpt-6.1-terra", "github_copilot/gpt-6.1-sol"])
def test_copilot_gpt6_is_sent_without_temperature(model: str) -> None:
    """Copilot answers 400 "Unsupported parameter: 'temperature'" for gpt-6.x."""
    from raven.providers.litellm_provider import LiteLLMProvider

    kwargs: dict = {"model": model, "temperature": 0.7, "top_p": 0.9}
    LiteLLMProvider(provider_name="github_copilot", default_model=model)._apply_model_overrides(model, kwargs)
    assert "temperature" not in kwargs
    assert kwargs["top_p"] == 0.9


def test_copilot_chat_models_keep_their_temperature() -> None:
    from raven.providers.litellm_provider import LiteLLMProvider

    model = "github_copilot/claude-sonnet-5.5"
    kwargs: dict = {"model": model, "temperature": 0.7}
    LiteLLMProvider(provider_name="github_copilot", default_model=model)._apply_model_overrides(model, kwargs)
    assert kwargs["temperature"] == 0.7
