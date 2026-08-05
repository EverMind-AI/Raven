"""Timeout behavior for AzureOpenAIProvider (issue #150).

The Azure path uses a raw httpx client with a per-read timeout, which cannot
bound a backend that trickles bytes. A wall-clock cap wraps the awaited POST so
a stalled endpoint yields a structured, retryable error instead of hanging.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from raven.providers.azure_openai_provider import AzureOpenAIProvider
from raven.providers.base import GenerationSettings


class _HangingClient:
    """httpx.AsyncClient stand-in whose POST never returns."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_HangingClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(10)


def _make_provider(timeout: float) -> AzureOpenAIProvider:
    provider = AzureOpenAIProvider(
        api_key="test-key",
        api_base="https://example.openai.azure.com",
        default_model="gpt-4o",
    )
    provider.generation = GenerationSettings(timeout=timeout)
    return provider


@pytest.mark.asyncio
async def test_chat_wall_clock_cap_returns_classified_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "raven.providers.azure_openai_provider.httpx.AsyncClient",
        _HangingClient,
    )
    provider = _make_provider(timeout=0.05)
    resp = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="gpt-4o")
    assert resp.finish_reason == "error"
    assert resp.error_classification is not None
    assert resp.error_classification.category == "network"
    assert resp.error_classification.retryable is True
