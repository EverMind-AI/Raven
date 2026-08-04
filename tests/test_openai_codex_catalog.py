"""Contract tests for the account-scoped OpenAI Codex model catalog."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from raven.providers.openai_codex_catalog import (
    CODEX_CATALOG_CLIENT_VERSION,
    CodexModelCatalogError,
    fetch_codex_models,
)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_fetch_codex_models_uses_account_catalog_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/backend-api/codex/models"
        assert request.url.params["client_version"] == CODEX_CATALOG_CLIENT_VERSION
        assert request.headers["Authorization"] == "Bearer oauth-token"
        assert request.headers["chatgpt-account-id"] == "acct-123"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"slug": "low-priority", "visibility": "list", "priority": 20},
                    {"slug": "hidden", "visibility": "hide", "priority": 1},
                    {"slug": "default-model", "visibility": "list", "priority": 2},
                    {"slug": "default-model", "visibility": "list", "priority": 3},
                    {"slug": "", "visibility": "list", "priority": 0},
                    {"slug": {"nested": True}, "visibility": "list", "priority": 0},
                    "malformed",
                ]
            },
        )

    token = SimpleNamespace(access="oauth-token", account_id="acct-123")
    models = fetch_codex_models(token, transport=_transport(handler))

    assert models == ["default-model", "low-priority"]


def test_fetch_codex_models_omits_absent_optional_account_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "chatgpt-account-id" not in request.headers
        return httpx.Response(
            200,
            json={"models": [{"slug": "model-1", "visibility": "list", "priority": 1}]},
        )

    token = SimpleNamespace(access="oauth-token", account_id=None)
    assert fetch_codex_models(token, transport=_transport(handler)) == ["model-1"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"models": None},
        {"models": []},
        {"models": [{"slug": "hidden", "visibility": "hide", "priority": 1}]},
    ],
)
def test_fetch_codex_models_rejects_catalog_without_visible_models(payload: object) -> None:
    token = SimpleNamespace(access="oauth-token", account_id="acct-123")
    transport = _transport(lambda _: httpx.Response(200, json=payload))

    with pytest.raises(CodexModelCatalogError, match="no visible models"):
        fetch_codex_models(token, transport=transport)


def test_fetch_codex_models_preserves_http_failure() -> None:
    token = SimpleNamespace(access="oauth-token", account_id="acct-123")
    transport = _transport(lambda _: httpx.Response(403, json={"detail": "forbidden"}))

    with pytest.raises(httpx.HTTPStatusError) as raised:
        fetch_codex_models(token, transport=transport)

    assert raised.value.response.status_code == 403


def test_fetch_codex_models_rejects_invalid_json_without_leaking_body() -> None:
    token = SimpleNamespace(access="oauth-token", account_id="acct-123")
    transport = _transport(lambda _: httpx.Response(200, content=b"not-json"))

    with pytest.raises(CodexModelCatalogError, match="invalid JSON") as raised:
        fetch_codex_models(token, transport=transport)

    assert "not-json" not in str(raised.value)
