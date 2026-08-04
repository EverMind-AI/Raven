"""Account-scoped model catalog for the OpenAI Codex OAuth provider."""

from __future__ import annotations

from typing import Any

import httpx

AUTO_CODEX_MODEL = "openai-codex/auto"
CODEX_CATALOG_URL = "https://chatgpt.com/backend-api/codex/models"
# This version declares the Codex protocol Raven currently implements. It is
# intentionally independent of Raven's package version.
CODEX_CATALOG_CLIENT_VERSION = "0.146.0"
CODEX_CATALOG_TIMEOUT = 5.0
CODEX_CATALOG_CACHE_TTL = 300.0


class CodexModelCatalogError(RuntimeError):
    """The Codex account catalog cannot provide a usable model."""


def is_auto_codex_model(model: str) -> bool:
    return model in {"auto", AUTO_CODEX_MODEL, "openai_codex/auto"}


def _catalog_headers(token: Any) -> dict[str, str]:
    access = getattr(token, "access", None)
    if not isinstance(access, str) or not access:
        raise CodexModelCatalogError("Codex OAuth access token is missing")

    headers = {
        "Authorization": f"Bearer {access}",
        "User-Agent": "raven (python)",
        "accept": "application/json",
    }
    account_id = getattr(token, "account_id", None)
    if isinstance(account_id, str) and account_id:
        headers["chatgpt-account-id"] = account_id
    return headers


def _visible_models(payload: Any) -> list[str]:
    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        raise CodexModelCatalogError("Codex model catalog contains no visible models")

    candidates: list[tuple[int, int, str]] = []
    for index, item in enumerate(raw_models):
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        priority = item.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            priority = 2**31 - 1
        candidates.append((priority, index, slug.strip()))

    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    models = list(dict.fromkeys(candidate[2] for candidate in candidates))
    if not models:
        raise CodexModelCatalogError("Codex model catalog contains no visible models")
    return models


def fetch_codex_models(
    token: Any,
    *,
    timeout: float = CODEX_CATALOG_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    """Fetch visible account models in the server's priority order."""
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if transport is not None:
        client_kwargs["transport"] = transport

    with httpx.Client(**client_kwargs) as client:
        response = client.get(
            CODEX_CATALOG_URL,
            params={"client_version": CODEX_CATALOG_CLIENT_VERSION},
            headers=_catalog_headers(token),
        )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise CodexModelCatalogError("Codex model catalog returned invalid JSON") from exc
    return _visible_models(payload)
