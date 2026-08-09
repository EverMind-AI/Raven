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


def test_a_configured_deployment_decides_the_url_path() -> None:
    """The deployment is a connection parameter, not part of the model id.

    It used to be read off the model id, which forced Azure's ids to be spelled
    without the prefix every other provider carries -- a connection detail
    dictating the shape of a stored model id.
    """
    provider = AzureOpenAIProvider(
        api_key="k",
        api_base="https://x.openai.azure.com",
        default_model="azure_openai/gpt-4o",
        deployment="my-prod-deployment",
    )
    url = provider._build_chat_url("azure_openai/gpt-4o")
    assert "/deployments/my-prod-deployment/chat/completions" in url
    assert "azure_openai" not in url


def test_without_a_deployment_the_model_id_still_names_it() -> None:
    """Configs written before the field exists must keep working unchanged."""
    provider = AzureOpenAIProvider(api_key="k", api_base="https://x.openai.azure.com")
    assert "/deployments/my-deployment/chat/completions" in provider._build_chat_url("my-deployment")


def test_the_api_version_comes_from_config_rather_than_the_client() -> None:
    """A tenant on another version had no way to say so while it was hardcoded."""
    provider = AzureOpenAIProvider(api_key="k", api_base="https://x.openai.azure.com", api_version="2025-01-01")
    assert provider._build_chat_url("d").endswith("?api-version=2025-01-01")
