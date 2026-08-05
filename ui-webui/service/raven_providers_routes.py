"""REST routes for raven-native LLM provider config (local ~/.raven/config.json).

Unlike raven_config_routes.py (gateway RPC), these import raven directly and
wrap raven.config.update_providers — the sanctioned config read/write API — to
reflect the provider registry and mutate the local config file. WebUI-only:
raven's core provider support is not modified here.

Mounted unconditionally by main.py (reads local config, no gateway needed).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import ValidationError

from raven.config import update_providers as up
from raven.providers.common_models import common_models_for
from raven.providers.registry import find_by_name


def _pick_list(name: str, default_model: str, configured: list[str]) -> list[str]:
    """Model ids the UI offers as chips, recommended first, deduplicated.

    default_model leads the curated shortlist rather than being assumed to sit
    inside it. The page badges whichever chip equals it as recommended, so a
    default missing from the list silently drops the badge -- and for a provider
    with no shortlist at all (OpenAI Codex bypasses LiteLLM, so no catalogue
    describes it) it is the only chip there is.

    Deliberately not unioning in ``litellm_models_for``, which is how the TUI
    picker widens the same shortlist. The TUI's is searchable; these are wrapped
    chips, and the catalogue would render 96 of them for OpenRouter and 77 for
    OpenAI. Every provider the catalogue can describe now has a curated head
    anyway, and the ones left with no chips are the ones it cannot describe: an
    arbitrary OpenAI-compatible endpoint, Azure's per-tenant deployment names, a
    local vLLM, and the two gateways whose route prefix names another vendor's
    driver.
    """
    candidates = [
        *([default_model] if default_model else []),
        *common_models_for(name),
        *configured,
    ]
    seen: set[str] = set()
    return [m for m in candidates if not (m in seen or seen.add(m))]


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    """Enrich a list_providers() row with registry defaults + configured models."""
    name = item["name"]
    spec = find_by_name(name)
    cfg = up.get_provider_config(name)  # redacted; models path is "models"
    models = cfg.get("models") or []
    common_models = _pick_list(name, getattr(spec, "default_model", "") if spec else "", models)
    return {
        "name": name,
        "displayName": item["display_name"],
        "isOauth": item["is_oauth"],
        "isLocal": item["is_local"],
        "isGateway": item["is_gateway"],
        "configured": item["configured"],
        "apiKeyRedacted": item["api_key_redacted"],
        "apiBase": item["api_base"],
        "defaultApiBase": getattr(spec, "default_api_base", "") if spec else "",
        "defaultModel": getattr(spec, "default_model", "") if spec else "",
        "envKey": getattr(spec, "env_key", "") if spec else "",
        "models": models,
        "commonModels": common_models,
        # True when the provider has no universal default endpoint and the user
        # must supply their own (e.g. Azure's per-tenant resource URL). Lets the
        # UI mark the base-url field required vs. safe-to-leave-blank.
        "requiresApiBase": bool(getattr(spec, "requires_api_base", False)) if spec else False,
    }


def build_raven_providers_router() -> APIRouter:
    router = APIRouter(prefix="/raven/providers", tags=["raven-providers"])

    @router.get("")
    def list_all() -> dict:
        return {"providers": [_summary(it) for it in up.list_providers()]}

    @router.get("/{name}")
    def get_one(name: str) -> dict:
        try:
            summaries = {it["name"]: it for it in up.list_providers()}
            if name not in summaries:
                raise KeyError(name)
            detail = _summary(summaries[name])
            detail["fields"] = {
                path: {
                    "type": spec["type"],
                    "default": spec["default"],
                    "isSecret": spec["is_secret"],
                    "description": spec["description"],
                }
                for path, spec in up.provider_field_specs(name).items()
            }
            return detail
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown provider '{name}'") from exc

    @router.put("/{name}")
    def update_one(name: str, body: dict = Body(...)) -> dict:
        fields: dict[str, Any] = {}
        if "apiKey" in body:
            fields["api_key"] = body["apiKey"]
        if "apiBase" in body:
            fields["api_base"] = body["apiBase"]
        if "models" in body:
            fields["models"] = body["models"] or []
        try:
            previous = up.set_provider_fields(name, fields)
            specs = up.provider_field_specs(name)
            redacted_previous = {}
            for path, value in previous.items():
                if specs.get(path, {}).get("is_secret"):
                    redacted_previous[path] = "(set)" if value else "(empty)"
                else:
                    redacted_previous[path] = value
            return {"ok": True, "previous": redacted_previous}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:  # OAuth provider rejects api_key
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/{name}/test")
    def test_one(name: str) -> dict:
        return {"result": up.test_provider(name)}

    @router.post("/{name}/reset")
    def reset_one(name: str) -> dict:
        try:
            up.reset_provider(name)
            return {"ok": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
