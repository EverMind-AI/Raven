"""``model.*`` RPC handlers — backend for the TUI ``/model`` v1 picker.

Five methods drive the picker:

* ``model.options`` — current model/provider + one row per provider.
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
from raven.providers.common_models import common_models_for, litellm_models_for
from raven.providers.registry import (
    CRED_ENDPOINT,
    CRED_LOCAL,
    CRED_OAUTH,
    canonical_provider_name,
    credential_kind,
    find_by_model,
    find_by_name,
    needs_public_model_prefix,
    public_model_prefix,
)
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


def _parse(model_cls: type, params: dict) -> Any:
    try:
        return model_cls.model_validate(params)
    except ValidationError as exc:
        raise ConfigValidationError(
            f"invalid params for {model_cls.__name__}",
            data={"errors": exc.errors(include_url=False)},
        ) from exc


def _provider_models(slug: str, *, configured: bool) -> list[str]:
    try:
        cfg = get_provider_config(slug, redact_secrets=False)
    except KeyError:
        cfg = {}
    from_config = cfg.get("models", [])
    from_config = list(from_config) if isinstance(from_config, list) else []
    # Priority: what the user configured (manual entry via ``model.add_model``
    # writes here), then the curated shortlist, then LiteLLM's own catalogue,
    # then whatever the account itself reports. Curated before catalogue because
    # the shortlist is a few models worth recommending and the catalogue is
    # everything, deprecated snapshots included; catalogue after it because eleven
    # providers have no shortlist at all, which is why the picker used to offer
    # them nothing; the account last because only one provider can be asked and
    # asking costs a request (see ``_account_models``).
    out: list[str] = []
    seen: set[str] = set()
    chain = (
        *from_config,
        *common_models_for(slug),
        *litellm_models_for(slug),
        *_account_models(slug, configured=configured),
    )
    for candidate in chain:
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    return out


def _account_models(slug: str, *, configured: bool) -> tuple[str, ...]:
    """Models the account itself reports, for a provider only it can answer for.

    Codex has no static list worth offering: the registry default is refused by
    the backend, and the entries LiteLLM's table carries are not the slugs an
    account is entitled to. Everyone else is served by the tiers above.

    Nobody signed in has nothing to report, so asking costs a request that can
    only fail -- and the failure is cached, which is how opening the picker while
    signed out left this provider empty for the half-minute after signing in.
    """
    if slug != "openai_codex" or not configured:
        return ()

    from raven.providers.codex_catalog import account_models

    return tuple(_stored_spelling(slug, model) for model in account_models())


def _build_provider_entry(slug: str, *, current_provider: str | None) -> dict[str, Any]:
    spec = find_by_name(slug)
    providers = {p["name"]: p for p in list_providers()}
    info = providers.get(slug, {})

    kind = credential_kind(slug)
    is_oauth = kind == CRED_OAUTH
    configured = bool(info.get("configured"))
    warning = ""
    if is_oauth and not configured:
        warning = f"run `raven provider login {slug.replace('_', '-')}` to authenticate"

    models = _provider_models(slug, configured=configured)
    return {
        "slug": slug,
        "name": info.get("display_name") or (spec.label if spec else slug),
        "authenticated": configured,
        "is_current": slug == current_provider,
        "auth_type": kind,
        "key_env": (spec.env_key or None) if spec else None,
        "models": models,
        "total_models": len(models),
        "needs_api_base": kind in (CRED_ENDPOINT, CRED_LOCAL),
        "warning": warning,
    }


async def _entry_off_loop(slug: str, current_provider: str | None) -> dict[str, Any]:
    """Build one picker row without blocking the event loop.

    Reading the candidate chain imports LiteLLM the first time, which takes
    seconds -- long enough to stall this session's token stream. Every handler
    that returns a row goes through here rather than warming the cache in one
    and reading it inline in the others: a first read that failed leaves nothing
    to reuse, and handler order is up to the client.
    """
    return await asyncio.to_thread(_build_provider_entry, slug, current_provider=current_provider)


async def _entries_off_loop(current_provider: str | None) -> list[dict[str, Any]]:
    """Build every picker row in one thread hop rather than one hop per row."""

    def _build() -> list[dict[str, Any]]:
        return [_build_provider_entry(p["name"], current_provider=current_provider) for p in list_providers()]

    return await asyncio.to_thread(_build)


def _current_selection() -> tuple[str, str | None]:
    from raven.cli._helpers import load_runtime_config

    config = load_runtime_config(None, None)
    current_model = config.agents.defaults.model
    provider = config.agents.defaults.provider
    if not provider or provider == "auto":
        spec = find_by_model(current_model) if current_model else None
        provider = spec.name if spec else None
    else:
        # The picker keys its rows by the current name; a config written before
        # a rename would match none of them.
        provider = canonical_provider_name(provider)
    return current_model, provider


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def model_options(params: dict) -> dict:
    _parse(ModelOptionsParams, params)
    current_model, current_provider = _current_selection()
    entries = await _entries_off_loop(current_provider)
    return {
        "model": current_model,
        "provider": current_provider or "",
        "providers": entries,
    }


async def model_save_key(params: dict) -> dict:
    parsed = _parse(ModelSaveKeyParams, params)

    # No spec of our own is not a reason to refuse: the picker lists such a
    # provider once it is configured, and the write path below is what decides
    # whether the name is usable. Rejecting here while the other four handlers
    # accepted it is how an orphan section got created.
    spec = find_by_name(parsed.slug)
    label = spec.label if spec else parsed.slug
    if spec and spec.is_oauth:
        raise NotSupportedInV01Error(
            f"{label} uses OAuth; run `raven provider login {parsed.slug.replace('_', '-')}`",
            data={"slug": parsed.slug},
        )
    kind = credential_kind(parsed.slug)
    if kind in (CRED_ENDPOINT, CRED_LOCAL) and not parsed.api_base:
        raise ConfigValidationError(
            f"{label} requires an api_base",
            data={"slug": parsed.slug, "field": "api_base"},
        )
    if kind == CRED_LOCAL and parsed.api_key:
        # Said out loud rather than dropped: a local deployment writes no key, so
        # storing one silently would look like it had been accepted.
        raise ConfigValidationError(
            f"{label} is a local deployment and takes no api_key; send api_base instead",
            data={"slug": parsed.slug, "field": "api_key"},
        )
    if kind != CRED_LOCAL and not parsed.api_key:
        raise ConfigValidationError(
            f"{label} requires an api_key",
            data={"slug": parsed.slug, "field": "api_key"},
        )

    # A local deployment is reached by address and has no key, said explicitly
    # rather than by omission: leaving the field alone kept whatever was there,
    # so a section that once held a key would still be sending it to the user's
    # own server.
    fields: dict[str, Any] = {"api_key": "" if kind == CRED_LOCAL else parsed.api_key}
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
        "provider": await _entry_off_loop(parsed.slug, current_provider),
    }


async def model_disconnect(params: dict) -> dict:
    parsed = _parse(ModelDisconnectParams, params)
    try:
        await asyncio.to_thread(reset_provider, parsed.slug)
    except KeyError as exc:
        raise ConfigValidationError(str(exc), data={"slug": parsed.slug}) from exc
    return {"disconnected": True}


def _stored_spelling(slug: str, model: str) -> str:
    """The id to store for a model the user typed, for the provider they typed it under.

    A bare id is claimed by keyword matching rather than by the provider it was
    entered for: "gpt-5.6-sol" resolves to OpenAI, so the request leaves for a
    provider that does not serve it. The listed models already carry the prefix,
    and a typed one has to end up spelled the same way.
    """
    spec = find_by_name(slug)
    if not needs_public_model_prefix(spec) or not model:
        return model

    prefix = public_model_prefix(spec)  # type: ignore[arg-type]

    return model if model.startswith(f"{prefix}/") else f"{prefix}/{model.split('/')[-1]}"


async def model_add_model(params: dict) -> dict:
    parsed = _parse(ModelAddModelParams, params)
    try:
        await asyncio.to_thread(add_provider_model, parsed.slug, _stored_spelling(parsed.slug, parsed.model))
    except KeyError as exc:
        raise ConfigValidationError(str(exc), data={"slug": parsed.slug}) from exc
    _, current_provider = _current_selection()
    return {
        "provider": await _entry_off_loop(parsed.slug, current_provider),
    }


async def model_remove_model(params: dict) -> dict:
    parsed = _parse(ModelRemoveModelParams, params)
    try:
        await asyncio.to_thread(remove_provider_model, parsed.slug, parsed.model)
    except KeyError as exc:
        raise ConfigValidationError(str(exc), data={"slug": parsed.slug}) from exc
    _, current_provider = _current_selection()
    return {
        "provider": await _entry_off_loop(parsed.slug, current_provider),
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
