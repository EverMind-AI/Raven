"""``model.*`` RPC handlers — backend for the TUI ``/model`` v1 picker.

Eight methods drive the picker:

* ``model.options`` — current model/provider + one row per provider.
* ``model.save_key`` — store an api_key (+ optional api_base) for a provider.
* ``model.disconnect`` — clear a provider's stored credentials.
* ``model.add_model`` / ``model.remove_model`` — edit a provider's curated
  model list.
* ``model.endpoints`` / ``model.add_endpoint`` / ``model.remove_endpoint`` —
  edit the several url/key groups one provider section can carry, each write
  answering with the refreshed (key-redacted) list.

All write helpers live in ``raven.config.update_providers`` (the single
write path for provider config); the handlers wrap the synchronous calls in
``asyncio.to_thread`` so the event loop is not blocked on disk IO. OAuth
providers cannot have keys written from the picker — that is gated to
``raven provider login`` and surfaced as -32012.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from raven.config.update_providers import (
    add_provider_endpoint,
    add_provider_model,
    get_provider_config,
    list_provider_endpoints,
    list_providers,
    remove_provider_endpoint,
    remove_provider_model,
    reset_provider,
    set_provider_fields,
)
from raven.providers.auth import credential_status
from raven.providers.common_models import common_models_for, litellm_models_for
from raven.providers.registry import (
    CRED_ENDPOINT,
    CRED_LOCAL,
    CRED_OAUTH,
    canonical_provider_name,
    credential_kind,
    find_by_model,
    find_by_name,
    split_model_id,
)
from raven.providers.wire import stored_model_id
from raven.rpc.errors import (
    ConfigValidationError,
    NotSupportedInV01Error,
)
from raven.rpc.models import (
    ModelAddEndpointParams,
    ModelAddModelParams,
    ModelDisconnectParams,
    ModelEndpointsParams,
    ModelOptionsParams,
    ModelRemoveEndpointParams,
    ModelRemoveModelParams,
    ModelSaveKeyParams,
)

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


def _parse(model_cls: type, params: dict) -> Any:
    try:
        return model_cls.model_validate(params)
    except ValidationError as exc:
        raise ConfigValidationError(
            f"invalid params for {model_cls.__name__}",
            data={"errors": exc.errors(include_url=False)},
        ) from exc


#: "No section was passed in" marker for the helpers below -- distinct from
#: ``None``, which is what a provider absent from the config resolves to.
_UNLOADED: Any = object()


def _provider_models(slug: str, *, configured: bool, section: Any = _UNLOADED) -> list[str]:
    if section is _UNLOADED:
        try:
            cfg = get_provider_config(slug, redact_secrets=False)
        except KeyError:
            cfg = {}
        from_config = cfg.get("models", [])
    else:
        from_config = getattr(section, "models", None) or []
    from_config = list(from_config) if isinstance(from_config, list) else []
    # Priority: what the user configured (manual entry via ``model.add_model``
    # writes here), then the curated shortlist, then LiteLLM's own catalogue,
    # then whatever the account itself reports. Curated before catalogue because
    # the shortlist is a few models worth recommending and the catalogue is
    # everything, deprecated snapshots included; catalogue after it because eleven
    # providers have no shortlist at all, which is why the picker used to offer
    # them nothing; the account last because only one provider can be asked and
    # asking costs a request (see ``_account_models``).
    from raven.providers.wire import merge_key

    out: list[str] = []
    seen: set[str] = set()
    chain = (
        *from_config,
        *common_models_for(slug),
        *litellm_models_for(slug),
        *_account_models(slug, configured=configured),
    )
    for candidate in chain:
        # By identity: a model reaching this list from two sources in two
        # spellings used to appear twice in the picker.
        key = merge_key(slug, candidate)
        if key not in seen:
            seen.add(key)
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


def _model_labels(slug: str, models: "list[str]", *, section: Any = _UNLOADED) -> dict[str, dict[str, Any]]:
    """Display facts for each offered id, skipping the ones nothing describes.

    What the user wrote under ``model_overlay`` wins: they are describing their
    own deployment, and for a model no catalogue carries they are the only
    source there is.
    """
    from raven.providers.catalog import describe

    overlays = _configured_overlays(slug, section=section)
    out: dict[str, dict[str, Any]] = {}
    for model in models:
        row = describe(slug, model, overlay=_overlay_for(overlays, slug, model))
        if not row.described:
            continue
        entry: dict[str, Any] = {"label": row.label}
        if row.description:
            entry["description"] = row.description
        out[model] = entry
    return out


def _configured_overlays(slug: str, *, section: Any = _UNLOADED) -> dict[str, Any]:
    """This provider's user-written model descriptions, keyed by merge key.

    Keyed by identity rather than by the string the user typed, so an overlay
    written against a bare id still matches the qualified id the picker offers.

    ``section`` lets a caller that already loaded the config hand the
    provider's section in (the all-rows path loads once instead of once per
    row); left unset, the config is read here.
    """
    from raven.providers.wire import merge_key

    if section is _UNLOADED:
        from raven.config.loader import load_config

        try:
            section = load_config().providers.get(slug)
        except Exception:
            return {}
    overlay = getattr(section, "model_overlay", None) or {}
    return {merge_key(slug, model): value for model, value in overlay.items()}


def _overlay_for(overlays: dict[str, Any], slug: str, model: str) -> Any:
    from raven.providers.wire import merge_key

    return overlays.get(merge_key(slug, model))


def _build_provider_entry(
    slug: str,
    *,
    current_provider: str | None,
    providers: dict[str, dict[str, Any]] | None = None,
    section: Any = _UNLOADED,
) -> dict[str, Any]:
    spec = find_by_name(slug)
    if providers is None:
        providers = {p["name"]: p for p in list_providers()}
    info = providers.get(slug, {})

    kind = credential_kind(slug)
    is_oauth = kind == CRED_OAUTH
    configured = bool(info.get("configured"))
    warning = ""
    if is_oauth and not configured:
        warning = f"run `raven provider login {slug.replace('_', '-')}` to authenticate"

    models = _provider_models(slug, configured=configured, section=section)
    return {
        # Names and one-liners for the ids above, so the picker shows what a
        # model is rather than only what it is called on the wire. Omitted for
        # ids no catalogue carries -- a local finetune, or a release newer than
        # the bundled snapshot -- and the picker falls back to the id for those.
        "model_labels": _model_labels(slug, models, section=section),
        "slug": slug,
        "name": info.get("display_name") or (spec.label if spec else slug),
        "authenticated": configured,
        "is_current": slug == current_provider,
        "auth_type": kind,
        "key_env": (spec.env_key or None) if spec else None,
        "models": models,
        "total_models": len(models),
        # "An address must be supplied" -- the gate's answer, not the shape's:
        # an endpoint-credential spec that ships a usable default (custom's
        # localhost gateway) runs on a bare key, and the picker must not
        # demand what the gate does not.
        "needs_api_base": kind == CRED_LOCAL or (kind == CRED_ENDPOINT and not (spec and spec.usable_default_api_base)),
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
    """Build every picker row in one thread hop rather than one hop per row.

    The config is also read once for all rows rather than once per row:
    ``_build_provider_entry`` re-derives the ``list_providers`` mapping and
    the provider's own section when called for a single row, and both are
    hoisted here for the all-rows case.
    """

    def _build() -> list[dict[str, Any]]:
        from raven.config.loader import load_config

        rows = list_providers()
        providers = {p["name"]: p for p in rows}
        try:
            sections = load_config().providers
        except Exception:
            sections = None
        return [
            _build_provider_entry(
                p["name"],
                current_provider=current_provider,
                providers=providers,
                section=sections.get(p["name"]) if sections is not None else _UNLOADED,
            )
            for p in rows
        ]

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


async def model_options(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Which models exist, and which one *this conversation* is on.

    The session matters: the model is per conversation now, so answering from
    ``agents.defaults`` would star the wrong row for every session that has
    switched -- the picker would disagree with the status bar it sits under.
    """
    parsed = _parse(ModelOptionsParams, params)
    current_model, current_provider = _current_selection()
    session_model = _session_model(agent_loop_factory, getattr(parsed, "session_id", None))
    if session_model:
        current_model = session_model
        spec = find_by_model(session_model)
        if spec is not None:
            current_provider = spec.name
        else:
            # No spec of ours -- a passthrough vendor (mistral, xai) that
            # ``ProvidersConfig`` supports and the picker does list. The stored
            # id still names its provider, because every switch writes it there
            # via ``stored_model_id``, so read the head rather than fall through
            # to ``agents.defaults.provider``: that would star another vendor's
            # row for a session running on this one's key, which is the exact
            # question a user reads the marked row to answer. An unknown head
            # stars nothing, which beats starring the wrong thing.
            head, _ = split_model_id(session_model)
            if head:
                current_provider = canonical_provider_name(head)
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
    if kind == CRED_LOCAL and parsed.api_key:
        # Said out loud rather than dropped: a local deployment writes no key, so
        # storing one silently would look like it had been accepted.
        raise ConfigValidationError(
            f"{label} is a local deployment and takes no api_key; send api_base instead",
            data={"slug": parsed.slug, "field": "api_key"},
        )
    # Whether the submission is complete is `providers.auth`'s answer, the same
    # one every other gate uses -- including which requirement a spec default
    # already covers (custom's shipped address). An address rule of this
    # handler's own is how the picker refused a submission the gate runs.
    submitted = {"api_key": parsed.api_key, "api_base": parsed.api_base}
    status = credential_status(parsed.slug, submitted)
    if not status.ok:
        labels = ", ".join(req.label for req in status.missing)
        field = next((f for req in status.missing for f in req.fields), "api_key")
        raise ConfigValidationError(
            f"{label} requires {labels}" if labels else f"{label} is missing credentials",
            data={"slug": parsed.slug, "field": field},
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
    """The id to store for a model the user typed. See ``providers.wire``.

    This used to prefix only the three providers whose own client strips the
    prefix back off, while the wizard prefixed nearly all of them -- so the same
    model picked in the two places was written two different ways into the same
    list.
    """
    return stored_model_id(slug, model)


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


async def _endpoints_off_loop(slug: str) -> list[dict[str, Any]]:
    """The provider's endpoint list, api_key redacted, off the event loop."""
    try:
        return await asyncio.to_thread(list_provider_endpoints, slug)
    except (KeyError, ValidationError) as exc:
        raise ConfigValidationError(str(exc), data={"slug": slug}) from exc


async def model_endpoints(params: dict) -> dict:
    parsed = _parse(ModelEndpointsParams, params)
    return {"endpoints": await _endpoints_off_loop(parsed.slug)}


async def model_add_endpoint(params: dict) -> dict:
    parsed = _parse(ModelAddEndpointParams, params)
    try:
        # extra_headers is deliberately not a parameter: the picker has no screen
        # that could collect one, and a field only `raven provider` can write is
        # not made reachable by declaring it here.
        await asyncio.to_thread(
            add_provider_endpoint,
            parsed.slug,
            label=parsed.label,
            api_key=parsed.api_key,
            api_base=parsed.api_base,
        )
    except (KeyError, ValidationError, RuntimeError) as exc:
        raise ConfigValidationError(str(exc), data={"slug": parsed.slug}) from exc
    # Re-read rather than redacting what the write returned, so the one place
    # deciding how a key is masked stays ``list_provider_endpoints``.
    return {"endpoints": await _endpoints_off_loop(parsed.slug)}


async def model_remove_endpoint(params: dict) -> dict:
    parsed = _parse(ModelRemoveEndpointParams, params)
    try:
        await asyncio.to_thread(remove_provider_endpoint, parsed.slug, parsed.label)
    except (KeyError, ValidationError) as exc:
        raise ConfigValidationError(str(exc), data={"slug": parsed.slug}) from exc
    return {"endpoints": await _endpoints_off_loop(parsed.slug)}


def _session_model(agent_loop_factory: "AgentLoopFactory | None", session_id: str | None) -> str | None:
    """This session's own model, or None when it never switched."""
    if not agent_loop_factory or not session_id:
        return None
    try:
        loop = agent_loop_factory()
    except Exception:
        return None
    # ``session_model`` falls back to the default, so it never answers None --
    # asking it alone would override a forced ``agents.defaults.provider`` for
    # every session, including the ones that never switched.
    has_own = getattr(loop, "has_session_binding", None)
    if not callable(has_own) or not has_own(session_id):
        return None
    reader = getattr(loop, "session_model", None)
    return reader(session_id) if callable(reader) else None


def register_model_methods(dispatcher: "Dispatcher", *, agent_loop_factory: "AgentLoopFactory | None" = None) -> None:
    """Register the eight ``model.*`` handlers on a dispatcher instance."""
    dispatcher.register("model.options", partial(model_options, agent_loop_factory=agent_loop_factory))
    dispatcher.register("model.save_key", model_save_key)
    dispatcher.register("model.disconnect", model_disconnect)
    dispatcher.register("model.add_model", model_add_model)
    dispatcher.register("model.remove_model", model_remove_model)
    dispatcher.register("model.endpoints", model_endpoints)
    dispatcher.register("model.add_endpoint", model_add_endpoint)
    dispatcher.register("model.remove_endpoint", model_remove_endpoint)


__all__ = [
    "model_options",
    "model_save_key",
    "model_disconnect",
    "model_add_model",
    "model_remove_model",
    "model_endpoints",
    "model_add_endpoint",
    "model_remove_endpoint",
    "register_model_methods",
    "_build_provider_entry",
]
