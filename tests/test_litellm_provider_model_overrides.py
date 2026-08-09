"""Per-model request-parameter overrides: config entries win over the registry.

Some models reject the usual defaults (Kimi K2.5 demands temperature=1.0). Those
quirks used to live only in the registry, so users could not adjust them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from raven.providers.base import GenerationSettings
from raven.providers.litellm_provider import LiteLLMProvider


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> Any:
        seen.append(kwargs)
        message = MagicMock(content="ok", tool_calls=None)
        return MagicMock(choices=[MagicMock(message=message, finish_reason="stop")], usage=None)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    return seen


def _provider(model: str, overrides: dict[str, dict[str, Any]] | None = None) -> LiteLLMProvider:
    provider = LiteLLMProvider(
        api_key="test-key",
        api_base="http://local/v1",
        default_model=model,
        provider_name="custom",
        model_overrides=overrides,
    )
    provider.generation = GenerationSettings(temperature=0.1)
    return provider


@pytest.mark.asyncio
async def test_config_override_applies_to_matching_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    p = _provider("my-model-v2", {"my-model": {"temperature": 1.0}})

    await p.chat(messages=[{"role": "user", "content": "hi"}], temperature=0.1)

    assert seen[0]["temperature"] == 1.0


@pytest.mark.asyncio
async def test_config_override_ignores_other_models(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    p = _provider("other-model", {"my-model": {"temperature": 1.0}})

    await p.chat(messages=[{"role": "user", "content": "hi"}], temperature=0.1)

    assert seen[0]["temperature"] == 0.1


@pytest.mark.asyncio
async def test_registry_default_still_applies_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # moonshot ships a kimi-k2.5 temperature default in its ProviderSpec.
    seen = _capture(monkeypatch)
    p = LiteLLMProvider(api_key="test-key", default_model="moonshot/kimi-k2.5")
    p.generation = GenerationSettings(temperature=0.1)

    await p.chat(messages=[{"role": "user", "content": "hi"}], temperature=0.1)

    assert seen[0]["temperature"] == 1.0


@pytest.mark.asyncio
async def test_registry_defaults_survive_gateway_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    # A gateway-routed id names the gateway and its upstream
    # ("openrouter/moonshotai/kimi-k2.5"), neither of which is a spec name -- but
    # Kimi still requires its temperature whichever door it is reached through.
    seen = _capture(monkeypatch)
    p = LiteLLMProvider(
        api_key="test-key",
        provider_name="openrouter",
        default_model="openrouter/moonshotai/kimi-k2.5",
    )
    p.generation = GenerationSettings(temperature=0.1)

    await p.chat(messages=[{"role": "user", "content": "hi"}], temperature=0.1)

    assert seen[0]["temperature"] == 1.0


@pytest.mark.asyncio
async def test_the_most_specific_pattern_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patterns match on substrings, so a broad one shadows a precise one unless
    # length decides -- and dict order must not be what picks the winner.
    seen = _capture(monkeypatch)
    # Broad pattern written last: a naive last-wins loop would pick 0.1.
    p = _provider("kimi-k2.5", {"kimi-k2.5": {"top_p": 0.9}, "kimi": {"top_p": 0.1}})

    await p.chat(messages=[{"role": "user", "content": "hi"}])

    assert seen[0]["top_p"] == 0.9


@pytest.mark.asyncio
async def test_config_override_layers_over_the_registry_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    # Setting one parameter must not discard the rest of the registry's entry:
    # Kimi's mandated temperature has to survive a config that only sets top_p.
    seen = _capture(monkeypatch)
    p = LiteLLMProvider(
        api_key="test-key",
        default_model="moonshot/kimi-k2.5",
        model_overrides={"kimi": {"top_p": 0.9}},
    )
    p.generation = GenerationSettings(temperature=0.1)

    await p.chat(messages=[{"role": "user", "content": "hi"}], temperature=0.1)

    assert seen[0]["temperature"] == 1.0
    assert seen[0]["top_p"] == 0.9


@pytest.mark.asyncio
async def test_config_override_wins_over_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    p = LiteLLMProvider(
        api_key="test-key",
        default_model="moonshot/kimi-k2.5",
        model_overrides={"kimi-k2.5": {"temperature": 0.5}},
    )
    p.generation = GenerationSettings(temperature=0.1)

    await p.chat(messages=[{"role": "user", "content": "hi"}], temperature=0.1)

    assert seen[0]["temperature"] == 0.5


@pytest.mark.asyncio
async def test_model_override_forwards_arbitrary_backend_param(monkeypatch: pytest.MonkeyPatch) -> None:
    # sglang's repetition_penalty has no dedicated kwarg in chat() -- it must
    # reach LiteLLM as a top-level kwarg so LiteLLM auto-forwards it into
    # extra_body for OpenAI-compatible backends.
    seen = _capture(monkeypatch)
    p = _provider("my-sglang-model", {"my-sglang-model": {"repetition_penalty": 1.05}})

    await p.chat(messages=[{"role": "user", "content": "hi"}])

    assert seen[0]["repetition_penalty"] == 1.05


@pytest.mark.asyncio
async def test_model_override_extra_body_merges_with_wire_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    # The user's Qwen3 chat_template_kwargs must survive alongside the
    # provider's own OpenRouter routing pin, not be clobbered by it.
    seen = _capture(monkeypatch)
    p = LiteLLMProvider(
        api_key="test-key",
        default_model="openrouter/qwen/qwen3",
        provider_name="openrouter",
        extra_body={"provider": {"order": ["Alibaba"]}},
        model_overrides={"qwen3": {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}},
    )
    p.generation = GenerationSettings(temperature=0.1)

    await p.chat(messages=[{"role": "user", "content": "hi"}])

    assert seen[0]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
        "provider": {"order": ["Alibaba"]},
    }


@pytest.mark.asyncio
async def test_model_override_extra_body_wire_key_wins_on_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    # On a colliding key, the provider's own wire-routing extra_body must win --
    # routing correctness over a user override that would misroute the request.
    seen = _capture(monkeypatch)
    p = LiteLLMProvider(
        api_key="test-key",
        default_model="openrouter/qwen/qwen3",
        provider_name="openrouter",
        extra_body={"provider": {"order": ["Anthropic"]}},
        model_overrides={"qwen3": {"extra_body": {"provider": {"order": ["Alibaba"]}}}},
    )
    p.generation = GenerationSettings(temperature=0.1)

    await p.chat(messages=[{"role": "user", "content": "hi"}])

    assert seen[0]["extra_body"] == {"provider": {"order": ["Anthropic"]}}


@pytest.mark.asyncio
async def test_extra_body_unchanged_without_user_override(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    p = LiteLLMProvider(
        api_key="test-key",
        default_model="openrouter/moonshotai/kimi-k2.5",
        provider_name="openrouter",
        extra_body={"provider": {"order": ["Anthropic"]}},
    )
    p.generation = GenerationSettings(temperature=0.1)

    await p.chat(messages=[{"role": "user", "content": "hi"}])

    assert seen[0]["extra_body"] == {"provider": {"order": ["Anthropic"]}}
