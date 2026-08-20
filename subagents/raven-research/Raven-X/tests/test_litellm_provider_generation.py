"""Generation-settings plumbing into LiteLLM request kwargs.

``repetition_penalty`` rides the request body (``extra_body``) because
only OpenAI-compatible self-hosted backends (sglang / vLLM) accept it;
``None`` must keep it entirely off the wire so hosted APIs that reject
unknown body fields are unaffected. An explicit provider-level
``extra_body`` wins over the generation default and is never mutated.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from raven.providers.base import GenerationSettings
from raven.providers.litellm_provider import LiteLLMProvider


def _ok_response(content: str = "ok"):
    message = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=None,
        thinking_blocks=None,
        provider_specific_fields=None,
    )
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model="stub",
    )


async def _captured_chat_kwargs(provider) -> dict:
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _ok_response()

    with patch("raven.providers.litellm_provider.acompletion", fake_acompletion):
        await provider.chat(messages=[{"role": "user", "content": "hi"}], model="openai/stub")
    return captured


@pytest.mark.asyncio
async def test_repetition_penalty_rides_extra_body():
    provider = LiteLLMProvider(api_key="k", default_model="openai/stub")
    provider.generation = GenerationSettings(repetition_penalty=1.05)

    kwargs = await _captured_chat_kwargs(provider)

    assert kwargs["extra_body"] == {"repetition_penalty": 1.05}


@pytest.mark.asyncio
async def test_none_repetition_penalty_stays_off_the_wire():
    provider = LiteLLMProvider(api_key="k", default_model="openai/stub")

    kwargs = await _captured_chat_kwargs(provider)

    assert "extra_body" not in kwargs


@pytest.mark.asyncio
async def test_explicit_extra_body_wins_and_is_not_mutated():
    explicit = {"repetition_penalty": 1.2, "other": True}
    provider = LiteLLMProvider(api_key="k", default_model="openai/stub", extra_body=explicit)
    provider.generation = GenerationSettings(repetition_penalty=1.05)

    kwargs = await _captured_chat_kwargs(provider)

    assert kwargs["extra_body"] == {"repetition_penalty": 1.2, "other": True}
    assert explicit == {"repetition_penalty": 1.2, "other": True}
    assert kwargs["extra_body"] is not provider.extra_body


@pytest.mark.asyncio
async def test_request_timeout_default_rides_kwargs():
    provider = LiteLLMProvider(api_key="k", default_model="openai/stub")

    kwargs = await _captured_chat_kwargs(provider)

    assert kwargs["timeout"] == 600.0


@pytest.mark.asyncio
async def test_request_timeout_configured_overrides_default():
    # Batch runs against congested endpoints lower this so one stalled call
    # (x4 retry ladder) cannot eat the whole wall-clock budget.
    provider = LiteLLMProvider(api_key="k", default_model="openai/stub")
    provider.generation = GenerationSettings(timeout=180.0)

    kwargs = await _captured_chat_kwargs(provider)

    assert kwargs["timeout"] == 180.0
