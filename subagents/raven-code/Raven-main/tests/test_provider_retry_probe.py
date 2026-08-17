"""Backend health probing between LLM retries.

Covers:
- retry ladder waits for a probe to answer before re-sending after a
  hang ("network") / 5xx ("server") failure
- probing is off by default (probe_timeout=0) and never runs for
  rate_limit failures
- probe budget exhaustion lets the retry proceed anyway
- LiteLLMProvider._probe_backend sends a 1-token request bounded by
  probe_timeout and maps answer/hang to True/False
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from raven.providers.base import (
    ErrorClassification,
    GenerationSettings,
    LLMProvider,
    LLMResponse,
)
from raven.providers.litellm_provider import LiteLLMProvider


class _FlakyProvider(LLMProvider):
    """chat() fails `fail_times` times with a fixed classification, then succeeds."""

    _CHAT_RETRY_DELAYS = (0, 0, 0)
    _PROBE_INTERVAL = 0.01

    def __init__(self, fail_times: int, category: str = "server") -> None:
        super().__init__(api_key="test")
        self.fail_times = fail_times
        self.category = category
        self.chat_calls = 0
        self.probe_calls = 0
        self.probe_healthy_after = 0
        self.events: list[str] = []

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.chat_calls += 1
        self.events.append("chat")
        if self.chat_calls <= self.fail_times:
            return LLMResponse(
                content="Error calling LLM: boom",
                finish_reason="error",
                error_classification=ErrorClassification(self.category, retryable=True, should_fallback=True),
            )
        return LLMResponse(content="ok", finish_reason="stop")

    async def _probe_backend(self, model: str | None) -> bool:
        self.probe_calls += 1
        self.events.append("probe")
        return self.probe_calls > self.probe_healthy_after

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_probe_runs_between_retries_and_waits_for_recovery() -> None:
    provider = _FlakyProvider(fail_times=1)
    provider.probe_healthy_after = 2
    provider.generation = GenerationSettings(probe_timeout=1.0, probe_budget=5.0)

    resp = await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="m")

    assert resp.finish_reason == "stop"
    assert provider.chat_calls == 2
    assert provider.probe_calls == 3
    assert provider.events == ["chat", "probe", "probe", "probe", "chat"]


@pytest.mark.asyncio
async def test_probe_disabled_by_default() -> None:
    provider = _FlakyProvider(fail_times=1)
    resp = await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="m")
    assert resp.finish_reason == "stop"
    assert provider.probe_calls == 0


@pytest.mark.asyncio
async def test_probe_skipped_for_rate_limit() -> None:
    provider = _FlakyProvider(fail_times=1, category="rate_limit")
    provider.generation = GenerationSettings(probe_timeout=1.0, probe_budget=5.0)
    resp = await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="m")
    assert resp.finish_reason == "stop"
    assert provider.probe_calls == 0


@pytest.mark.asyncio
async def test_probe_budget_exhaustion_still_retries() -> None:
    provider = _FlakyProvider(fail_times=1)
    provider.probe_healthy_after = 10_000
    provider.generation = GenerationSettings(probe_timeout=1.0, probe_budget=0.03)

    resp = await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="m")

    assert resp.finish_reason == "stop"
    assert provider.chat_calls == 2
    assert provider.probe_calls >= 1


def _make_litellm_provider(probe_timeout: float = 0.05) -> LiteLLMProvider:
    provider = LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")
    provider.generation = GenerationSettings(probe_timeout=probe_timeout)
    return provider


@pytest.mark.asyncio
async def test_litellm_probe_sends_one_token_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    provider = _make_litellm_provider(probe_timeout=7.0)
    assert await provider._probe_backend("openai/gpt-4o") is True
    assert captured["max_tokens"] == 1
    assert captured["timeout"] == 7.0


@pytest.mark.asyncio
async def test_litellm_probe_hang_reports_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hanging_acompletion(**_kwargs: Any):
        await asyncio.sleep(10)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", hanging_acompletion)
    provider = _make_litellm_provider(probe_timeout=0.05)
    assert await provider._probe_backend(None) is False


@pytest.mark.asyncio
async def test_litellm_probe_error_reports_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_acompletion(**_kwargs: Any):
        raise RuntimeError("502 Bad Gateway")

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", failing_acompletion)
    provider = _make_litellm_provider()
    assert await provider._probe_backend(None) is False
