"""Automatic model-resolution tests for the OpenAI Codex provider."""

from __future__ import annotations

from types import SimpleNamespace

import oauth_cli_kit
import pytest

from raven.providers import openai_codex_provider as codex_module
from raven.providers.openai_codex_catalog import CodexModelCatalogError
from raven.providers.openai_codex_provider import OpenAICodexProvider


@pytest.mark.asyncio
async def test_provider_resolves_and_caches_auto_model(monkeypatch: pytest.MonkeyPatch):
    token = SimpleNamespace(access="oauth-token", account_id="acct-123")
    monkeypatch.setattr(oauth_cli_kit, "get_token", lambda: token)
    catalog_calls = 0
    requested_models: list[str] = []

    def fetch_models(received_token, **kwargs):
        nonlocal catalog_calls
        catalog_calls += 1
        assert received_token is token
        return ["account-default", "account-second"]

    async def request(url, headers, body, verify, timeout):
        requested_models.append(body["model"])
        return "ok", [], "stop"

    monkeypatch.setattr(codex_module, "fetch_codex_models", fetch_models)
    monkeypatch.setattr(codex_module, "_request_codex", request)
    provider = OpenAICodexProvider()

    first = await provider.chat(messages=[])
    second = await provider.chat(messages=[])

    assert first.content == second.content == "ok"
    assert catalog_calls == 1
    assert requested_models == ["account-default", "account-default"]


@pytest.mark.asyncio
async def test_provider_auto_model_cache_is_account_scoped(monkeypatch: pytest.MonkeyPatch):
    catalog_accounts: list[str] = []

    def fetch_models(token, **kwargs):
        catalog_accounts.append(token.account_id)
        return [f"model-for-{token.account_id}"]

    monkeypatch.setattr(codex_module, "fetch_codex_models", fetch_models)
    provider = OpenAICodexProvider()

    first = await provider._resolve_auto_model(SimpleNamespace(access="token-a", account_id="account-a"))
    second = await provider._resolve_auto_model(SimpleNamespace(access="token-b", account_id="account-b"))

    assert first == "model-for-account-a"
    assert second == "model-for-account-b"
    assert catalog_accounts == ["account-a", "account-b"]


@pytest.mark.asyncio
async def test_provider_auto_model_cache_expires(monkeypatch: pytest.MonkeyPatch):
    catalog_results = iter([["model-before-refresh"], ["model-after-refresh"]])
    monkeypatch.setattr(codex_module, "fetch_codex_models", lambda token, **kwargs: next(catalog_results))
    provider = OpenAICodexProvider()
    token = SimpleNamespace(access="token-a", account_id="account-a")

    assert await provider._resolve_auto_model(token) == "model-before-refresh"
    provider._resolved_auto_model_expires_at = 0.0
    assert await provider._resolve_auto_model(token) == "model-after-refresh"


@pytest.mark.asyncio
async def test_provider_preserves_explicit_model_without_catalog(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        oauth_cli_kit,
        "get_token",
        lambda: SimpleNamespace(access="oauth-token", account_id="acct-123"),
    )
    requested_models: list[str] = []

    def unexpected_catalog(*args, **kwargs):
        raise AssertionError("explicit models must not fetch the catalog")

    async def request(url, headers, body, verify, timeout):
        requested_models.append(body["model"])
        return "ok", [], "stop"

    monkeypatch.setattr(codex_module, "fetch_codex_models", unexpected_catalog)
    monkeypatch.setattr(codex_module, "_request_codex", request)
    provider = OpenAICodexProvider(default_model="openai-codex/user-selected")

    response = await provider.chat(messages=[])

    assert response.content == "ok"
    assert requested_models == ["user-selected"]


@pytest.mark.asyncio
async def test_provider_does_not_post_when_auto_catalog_is_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        oauth_cli_kit,
        "get_token",
        lambda: SimpleNamespace(access="oauth-token", account_id="acct-123"),
    )
    request_called = False

    def no_models(*args, **kwargs):
        raise CodexModelCatalogError("Codex model catalog contains no visible models")

    async def request(*args, **kwargs):
        nonlocal request_called
        request_called = True
        return "unexpected", [], "stop"

    monkeypatch.setattr(codex_module, "fetch_codex_models", no_models)
    monkeypatch.setattr(codex_module, "_request_codex", request)

    response = await OpenAICodexProvider().chat(messages=[])

    assert response.finish_reason == "error"
    assert "no visible models" in response.content
    assert request_called is False
