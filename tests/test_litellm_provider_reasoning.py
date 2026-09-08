"""The reasoning effort reaches an OpenRouter-routed model.

litellm's ``drop_params`` silently discards ``reasoning_effort`` for any model
it cannot map -- which is every newly released model behind a gateway -- so a
turn configured for deep reasoning would run with reasoning off and nothing
saying so. OpenRouter accepts the reasoning object natively; behind that
gateway the effort is mirrored into ``extra_body.reasoning``, which
``drop_params`` never touches. A reasoning entry already present keeps
priority, and other providers are left as they were.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from raven.contracts.llm_provider import GenerationSettings
from raven.providers.litellm_provider import LiteLLMProvider


def _capture_chat(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> Any:
        seen.append(kwargs)
        message = MagicMock(content="ok", tool_calls=None)
        return MagicMock(choices=[MagicMock(message=message, finish_reason="stop")], usage=None)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    return seen


@dataclass
class _Delta:
    content: str | None = None
    tool_calls: list[Any] | None = None


@dataclass
class _Choice:
    delta: _Delta
    finish_reason: str | None = None
    index: int = 0


@dataclass
class _Chunk:
    choices: list[_Choice]
    usage: Any | None = None


def _capture_stream(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> Any:
        seen.append(kwargs)

        async def gen():
            yield _Chunk([_Choice(_Delta(content="ok"), finish_reason="stop")])

        return gen()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    return seen


def _provider(provider_name: str, extra_body: dict[str, Any] | None = None) -> LiteLLMProvider:
    with patch("raven.providers.litellm_provider.LiteLLMProvider._setup_env"):
        provider = LiteLLMProvider(
            api_key="sk-test",
            api_base="https://openrouter.ai/api/v1" if provider_name == "openrouter" else None,
            default_model="anthropic/claude-opus-5",
            provider_name=provider_name,
            extra_body=extra_body,
        )
    provider.generation = GenerationSettings(temperature=1.0)
    return provider


@pytest.mark.asyncio
async def test_behind_openrouter_the_effort_is_mirrored_into_the_reasoning_object(monkeypatch):
    seen = _capture_chat(monkeypatch)
    await _provider("openrouter").chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="medium")
    assert seen[0]["reasoning_effort"] == "medium"
    assert seen[0]["extra_body"]["reasoning"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_the_streaming_path_mirrors_the_same_way(monkeypatch):
    seen = _capture_stream(monkeypatch)
    provider = _provider("openrouter")
    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}], reasoning_effort="max"):
        pass
    assert seen[0]["reasoning_effort"] == "max"
    assert seen[0]["extra_body"]["reasoning"] == {"effort": "max"}


@pytest.mark.asyncio
async def test_the_configured_default_effort_is_mirrored_too(monkeypatch):
    """The sentinel path: no per-call effort, the provider's own configured
    effort is what rides -- and it has to reach the gateway just the same."""
    seen = _capture_stream(monkeypatch)
    provider = _provider("openrouter")
    provider.generation = GenerationSettings(temperature=1.0, reasoning_effort="high")
    async for _ in provider.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        pass
    assert seen[0]["extra_body"]["reasoning"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_a_reasoning_entry_the_deployment_configured_keeps_priority(monkeypatch):
    seen = _capture_chat(monkeypatch)
    provider = _provider("openrouter", extra_body={"reasoning": {"enabled": False}})
    await provider.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="medium")
    assert seen[0]["extra_body"]["reasoning"] == {"enabled": False}


@pytest.mark.asyncio
async def test_no_effort_means_no_reasoning_object(monkeypatch):
    seen = _capture_chat(monkeypatch)
    await _provider("openrouter").chat(messages=[{"role": "user", "content": "hi"}])
    assert "reasoning" not in (seen[0].get("extra_body") or {})


@pytest.mark.asyncio
async def test_other_providers_are_left_as_they_were(monkeypatch):
    seen = _capture_chat(monkeypatch)
    await _provider("anthropic").chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="medium")
    assert seen[0]["reasoning_effort"] == "medium"
    assert "reasoning" not in (seen[0].get("extra_body") or {})
