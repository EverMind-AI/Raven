"""``model.*`` RPC handlers — backend for the TUI ``/model`` v1 picker.

Five methods drive the picker:

* ``model.options`` — current model/provider + one row per provider (no network).
* ``model.save_key`` — store an api_key (+ optional api_base) for a provider.
* ``model.disconnect`` — clear a provider's stored credentials.
* ``model.add_model`` / ``model.remove_model`` — edit a provider's curated
  model list.

All write helpers live in ``raven.config.update_providers`` (the single
write path for provider config); the handlers wrap the synchronous calls in
``asyncio.to_thread`` so the event loop is not blocked on disk IO. OAuth
providers cannot have keys written from the picker — that is gated to
``raven provider login`` and surfaced as -32012.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from raven.config.update_providers import (
    add_provider_model,
    get_provider_config,
    list_providers,
    remove_provider_model,
    reset_provider,
    set_provider_fields,
)
from raven.providers.common_models import common_models_for
from raven.providers.registry import find_by_model, find_by_name
from raven.tui_rpc.errors import (
    ConfigValidationError,
    NotSupportedInV01Error,
)
from raven.tui_rpc.models import (
    ModelAddModelParams,
    ModelDisconnectParams,
    ModelOptionsParams,
    ModelRemoveModelParams,
    ModelSaveKeyParams,
)

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher


_NEEDS_API_BASE = {"custom", "azure_openai"}


def _parse(model_cls: type, params: dict) -> Any:
    try:
        return model_cls.model_validate(params)
    except ValidationError as exc:
        raise ConfigValidationError(
            f"invalid params for {model_cls.__name__}",
            data={"errors": exc.errors(include_url=False)},
        ) from exc


def _provider_models(slug: str) -> list[str]:
    try:
        cfg = get_provider_config(slug, redact_secrets=False)
    except KeyError:
        cfg = {}
    configured = cfg.get("models", [])
    configured = list(configured) if isinstance(configured, list) else []
    # Priority: the user's configured models first (manual entry via
    # ``model.add_model`` writes here), then our curated "common" shortlist,
    # deduped. Keeps the picker useful out of the box without a network call.
    seen = set(configured)
    return configured + [m for m in common_models_for(slug) if m not in seen]


def _build_provider_entry(slug: str, *, current_provider: str | None) -> dict[str, Any]:
    spec = find_by_name(slug)
    providers = {p["name"]: p for p in list_providers()}
    info = providers.get(slug, {})

    is_oauth = bool(spec and spec.is_oauth)
    configured = bool(info.get("configured"))
    warning = ""
    if is_oauth and not configured:
        warning = f"run `raven provider login {slug.replace('_', '-')}` to authenticate"

    models = _provider_models(slug)
    return {
        "slug": slug,
        "name": info.get("display_name") or (spec.label if spec else slug),
        "authenticated": configured,
        "is_current": slug == current_provider,
        "auth_type": "oauth" if is_oauth else "api_key",
        "key_env": (spec.env_key or None) if spec else None,
        "models": models,
        "total_models": len(models),
        "needs_api_base": slug in _NEEDS_API_BASE,
        "warning": warning,
    }


def _current_selection() -> tuple[str, str | None]:
    from raven.cli._helpers import load_runtime_config

    config = load_runtime_config(None, None)
    current_model = config.agents.defaults.model
    provider = config.agents.defaults.provider
    if not provider or provider == "auto":
        spec = find_by_model(current_model) if current_model else None
        provider = spec.name if spec else None
    return current_model, provider


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def model_options(params: dict) -> dict:
    _parse(ModelOptionsParams, params)
    current_model, current_provider = _current_selection()
    entries = [_build_provider_entry(p["name"], current_provider=current_provider) for p in list_providers()]
    return {
        "model": current_model,
        "provider": current_provider or "",
        "providers": entries,
    }


async def model_save_key(params: dict) -> dict:
    parsed = _parse(ModelSaveKeyParams, params)

    spec = find_by_name(parsed.slug)
    if spec is None:
        raise ConfigValidationError(
            f"unknown provider '{parsed.slug}'",
            data={"slug": parsed.slug},
        )
    if spec.is_oauth:
        raise NotSupportedInV01Error(
            f"{spec.label} uses OAuth; run `raven provider login {parsed.slug.replace('_', '-')}`",
            data={"slug": parsed.slug},
        )
    if parsed.slug in _NEEDS_API_BASE and not parsed.api_base:
        raise ConfigValidationError(
            f"{spec.label} requires an api_base",
            data={"slug": parsed.slug, "field": "api_base"},
        )

    fields: dict[str, Any] = {"api_key": parsed.api_key}
    if parsed.api_base:
        fields["api_base"] = parsed.api_base

    try:
        await asyncio.to_thread(set_provider_fields, parsed.slug, fields)
    except RuntimeError as exc:
        raise NotSupportedInV01Error(str(exc), data={"slug": parsed.slug}) from exc
    except KeyError as exc:
        raise ConfigValidationError(str(exc), data={"slug": parsed.slug}) from exc

    _, current_provider = _current_selection()
    return {
        "provider": _build_provider_entry(parsed.slug, current_provider=current_provider),
    }


async def model_disconnect(params: dict) -> dict:
    parsed = _parse(ModelDisconnectParams, params)
    try:
        await asyncio.to_thread(reset_provider, parsed.slug)
    except KeyError as exc:
        raise ConfigValidationError(str(exc), data={"slug": parsed.slug}) from exc
    return {"disconnected": True}


async def model_add_model(params: dict) -> dict:
    parsed = _parse(ModelAddModelParams, params)
    try:
        await asyncio.to_thread(add_provider_model, parsed.slug, parsed.model)
    except KeyError as exc:
        raise ConfigValidationError(str(exc), data={"slug": parsed.slug}) from exc
    _, current_provider = _current_selection()
    return {
        "provider": _build_provider_entry(parsed.slug, current_provider=current_provider),
    }


async def model_remove_model(params: dict) -> dict:
    parsed = _parse(ModelRemoveModelParams, params)
    try:
        await asyncio.to_thread(remove_provider_model, parsed.slug, parsed.model)
    except KeyError as exc:
        raise ConfigValidationError(str(exc), data={"slug": parsed.slug}) from exc
    _, current_provider = _current_selection()
    return {
        "provider": _build_provider_entry(parsed.slug, current_provider=current_provider),
    }


def register_model_methods(dispatcher: "Dispatcher") -> None:
    """Register the five ``model.*`` handlers on a dispatcher instance."""
    dispatcher.register("model.options", model_options)
    dispatcher.register("model.save_key", model_save_key)
    dispatcher.register("model.disconnect", model_disconnect)
    dispatcher.register("model.add_model", model_add_model)
    dispatcher.register("model.remove_model", model_remove_model)


__all__ = [
    "model_options",
    "model_save_key",
    "model_disconnect",
    "model_add_model",
    "model_remove_model",
    "register_model_methods",
    "_build_provider_entry",
]
