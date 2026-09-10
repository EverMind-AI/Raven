"""``model.*`` RPC handlers — backend for the TUI ``/model`` v1 picker.

Nine methods drive the picker:

* ``model.options`` — current model/provider + one row per provider.
* ``model.save_key`` — store an api_key (+ optional api_base) for a provider.
* ``model.disconnect`` — clear a provider's stored credentials.
* ``model.add_model`` / ``model.remove_model`` — edit a provider's curated
  model list.
* ``model.endpoints`` / ``model.add_endpoint`` / ``model.remove_endpoint`` —
  edit the several url/key groups one provider section can carry, each write
  answering with the refreshed (key-redacted) list.
* ``model.set_protocol`` — set or clear one model's API wire override.

All write helpers live in ``raven.config.update_providers`` (the single
write path for provider config); the handlers wrap the synchronous calls in
``asyncio.to_thread`` so the event loop is not blocked on disk IO. OAuth
providers cannot have keys written from the picker — that is gated to
``raven provider login`` and surfaced as -32012.
"""

from __future__ import annotations

import asyncio
import time
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
    SHAPE_ENDPOINT,
    SHAPE_LOCAL,
    SHAPE_OAUTH,
    auth_shape,
    canonical_provider_name,
    find_by_model,
    find_by_name,
    split_model_id,
)
from raven.providers.wire import stored_model_id
from raven.rpc.errors import (
    ConfigValidationError,
    NotSupportedError,
)
from raven.rpc.models import (
    ModelAddEndpointParams,
    ModelAddModelParams,
    ModelDisconnectParams,
    ModelEndpointsParams,
    ModelFetchModelsParams,
    ModelOptionsParams,
    ModelRemoveEndpointParams,
    ModelRemoveModelParams,
    ModelSaveKeyParams,
    ModelSetProtocolParams,
)

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


#: How long a catalogue fetch may take. Longer than the picker's own probes
#: because a person pressed a button and is watching: an aggregator listing
#: three hundred models over a slow link is a wait worth sitting through, where
#: a background refresh that took this long would be a stall nobody asked for.
_FETCH_TIMEOUT_S = 20


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

_LIVE_MODEL_CACHE_TTL_SECONDS = 5.0
_LIVE_MODEL_CACHE: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}


def _accepts_api_key(slug: str, kind: str) -> bool:
    """Whether this provider has a key field at all.

    Everything but an OAuth flow and a local deployment does. A local one is
    reached by address,
    except for the servers that can be put behind a token -- Ollama's remote
    mode and LM Studio's server setting both can be, and a deployment on
    someone else's machine usually is. Which those are is declared on the spec
    (``accepts_optional_api_key``) rather than matched by name here: the
    settings pane, the wizard and the save handler all ask this question, and
    three copies of one list is how the second such provider gets the field in
    one place and not another.
    """
    if kind == SHAPE_OAUTH:
        return False
    if kind != SHAPE_LOCAL:
        return True
    spec = find_by_name(canonical_provider_name(slug))
    return bool(spec and spec.accepts_optional_api_key)


def _configured_models(slug: str, *, section: Any = _UNLOADED) -> list[str]:
    """Only what this provider's config section lists, in the order it lists it.

    Distinct from ``_provider_models`` below, which is the picker's offer: that
    one adds a curated shortlist and a catalogue on top, so a provider nobody
    has configured still has something to choose from. A settings page asking
    "which models have I added here" must not be answered with those -- it read
    as seven models already added to a provider with no key.
    """
    if section is _UNLOADED:
        try:
            cfg = get_provider_config(slug, redact_secrets=False)
        except KeyError:
            cfg = {}
        models = cfg.get("models", [])
    else:
        models = getattr(section, "models", None) or []
    return list(models) if isinstance(models, list) else []


def _provider_models(slug: str, *, configured: bool, section: Any = _UNLOADED) -> list[str]:
    from_config = _configured_models(slug, section=section)
    # Priority: what the user configured (manual entry via ``model.add_model``
    # writes here), then the curated shortlist, then LiteLLM's own catalogue,
    # then whatever the configured runtime reports. Curated before catalogue because
    # the shortlist is a few models worth recommending and the catalogue is
    # everything, deprecated snapshots included; catalogue after it because eleven
    # providers have no shortlist at all, which is why the picker used to offer
    # them nothing; the live source is last because asking may cost a request
    # (see ``_runtime_models``).
    from raven.providers.wire import merge_key

    out: list[str] = []
    seen: set[str] = set()
    chain = (
        *from_config,
        *common_models_for(slug),
        *litellm_models_for(slug),
        *_runtime_models(slug, configured=configured, section=section),
    )
    for candidate in chain:
        # By identity: a model reaching this list from two sources in two
        # spellings used to appear twice in the picker.
        key = merge_key(slug, candidate)
        if key not in seen:
            seen.add(key)
            out.append(candidate)

    return out


def _runtime_models(slug: str, *, configured: bool, section: Any = _UNLOADED) -> tuple[str, ...]:
    """Models reported by a configured runtime rather than a static catalogue.

    Codex has no static list worth offering: the registry default is refused by
    the backend, and the entries LiteLLM's table carries are not the slugs an
    account is entitled to. LM Studio serves whichever models are loaded in the
    local process, so its ``/v1/models`` response is likewise authoritative.

    An unconfigured provider has nothing to report. LM Studio probes are cached
    briefly so repeated picker refreshes do not turn into repeated local HTTP
    calls, while a model loaded after the picker opens appears within seconds.
    """
    if not configured:
        return ()

    if slug == "openai_codex":
        from raven.providers.codex_catalog import account_models

        return tuple(_stored_spelling(slug, model) for model in account_models())

    if slug != "lm_studio":
        return ()

    if section is _UNLOADED:
        try:
            cfg = get_provider_config(slug, redact_secrets=False)
        except KeyError:
            cfg = {}
        api_base = str(cfg.get("api_base") or "")
    else:
        api_base = str(getattr(section, "api_base", None) or "")

    cache_key = (slug, api_base)
    now = time.monotonic()
    cached = _LIVE_MODEL_CACHE.get(cache_key)
    if cached and now - cached[0] < _LIVE_MODEL_CACHE_TTL_SECONDS:
        return cached[1]

    from raven.config.update_providers import test_provider

    result = test_provider(slug, timeout_s=2)
    raw_models = result.get("model_ids") if result.get("ok") else None
    models = tuple(_stored_spelling(slug, model) for model in (raw_models or []) if isinstance(model, str) and model)
    _LIVE_MODEL_CACHE[cache_key] = (now, models)
    return models


def _model_labels(slug: str, models: "list[str]", *, section: Any = _UNLOADED) -> dict[str, dict[str, Any]]:
    """Display facts for each offered id, skipping the ones nothing knows about.

    What the user wrote under ``model_overlay`` wins: they are describing their
    own deployment, and for a model no catalogue carries they are the only
    source there is.

    An entry is emitted for anything showable, not only for a described one: a
    gateway lists models the vendor rows tag without naming, and skipping those
    would drop the icon row along with the label it does not have.

    The context window comes from ``rates`` rather than from the registry the
    rest of this row reads, and never fetches: a window sizes trimming, so the
    figure a picker shows must be the one a request is sized with, and a UI call
    is no place to reach the network. A cold cache answers None, which the
    surfaces render as no badge rather than as a zero.
    """
    from raven.providers.catalog import describe
    from raven.providers.rates import resolve_context_window

    overlays = _configured_overlays(slug, section=section)
    out: dict[str, dict[str, Any]] = {}
    for model in models:
        row = describe(slug, model, overlay=_overlay_for(overlays, slug, model))
        window = resolve_context_window(model, allow_fetch=False)
        if not (row.described or row.tagged or window):
            continue
        entry: dict[str, Any] = {"label": row.label}
        if row.description:
            entry["description"] = row.description
        if row.capabilities:
            entry["capabilities"] = list(row.capabilities)
        if row.input_modalities:
            entry["input_modalities"] = list(row.input_modalities)
        if row.output_modalities:
            entry["output_modalities"] = list(row.output_modalities)
        if window:
            entry["context_window"] = window
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


def _docs_url(slug: str) -> str | None:
    """The provider's model documentation, or None when the registry has none."""
    from raven.providers.registry_data import provider_metadata

    website = provider_metadata(slug).get("metadata") or {}
    docs = (website.get("website") or {}).get("docs") if isinstance(website, dict) else None
    return docs if isinstance(docs, str) and docs else None


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

    kind = auth_shape(slug)
    is_oauth = kind == SHAPE_OAUTH
    configured = bool(info.get("configured"))
    # `configured` is the whole answer, local or not: the credential gate already
    # knows that a local deployment is reached by address, and it does not count
    # the spec's shipped default as configuration -- an untouched install reads
    # False for every one of them.
    #
    # A `local_key` term used to sit here, requiring a bearer token before a
    # local provider counted as authenticated. It made the ordinary way to run
    # Ollama -- an address and no key -- report as unauthenticated, which the
    # TUI picker files under "not set up" and then offers a sign-in for a
    # provider that needs no key. It also read a field only one of the two
    # callers passes, so `save_key` and `model.options` answered differently
    # about the state the save had just written.
    authenticated = configured
    warning = ""
    if is_oauth and not configured:
        warning = f"run `raven provider login {slug.replace('_', '-')}` to authenticate"

    models = _provider_models(slug, configured=configured, section=section)
    from raven.providers.protocol import effective_protocol, native_api_base

    loaded = section if section is not _UNLOADED else None
    protocols = {model: effective_protocol(loaded, model, slug) for model in models}
    if configured and not (spec and spec.client):
        from raven.providers.endpoints import provider_endpoints

        endpoints = provider_endpoints(section) if section is not _UNLOADED else []
        missing = sorted(
            {
                protocol
                for protocol in protocols.values()
                if protocol != "chat"
                and (not endpoints or any(not native_api_base(slug, protocol, ep.api_base) for ep in endpoints))
            }
        )
        if missing:
            warning = (
                "Explicit API base required for " + ", ".join(missing) + "; select a compatible endpoint or protocol."
            )
    overrides = {}
    if section is not _UNLOADED:
        raw_overrides = getattr(section, "model_protocols", {}) or {}
        if isinstance(raw_overrides, dict):
            overrides = {str(model): str(protocol) for model, protocol in raw_overrides.items()}
    return {
        # Names and one-liners for the ids above, so the picker shows what a
        # model is rather than only what it is called on the wire. Omitted for
        # ids no catalogue carries -- a local finetune, or a release newer than
        # the bundled snapshot -- and the picker falls back to the id for those.
        "model_labels": _model_labels(slug, models, section=section),
        "slug": slug,
        "name": info.get("display_name") or (spec.label if spec else slug),
        "homepage": (spec.homepage or None) if spec else None,
        # Where this vendor documents its models, as the registry files it. The
        # homepage is not that link: a settings page asking "which model do I
        # put here" wants the model index, not a marketing front page.
        "docs": _docs_url(slug),
        "authenticated": authenticated,
        "is_current": slug == current_provider,
        "auth_type": kind,
        "key_env": (spec.env_key or None) if spec else None,
        "api_base": info.get("api_base"),
        "default_api_base": (spec.default_api_base or None) if spec else None,
        "models": models,
        # What this section actually lists, which is a different question from
        # the offer above: the settings page manages a list, the picker offers
        # one, and the offer includes a shortlist nobody added.
        "configured_models": _configured_models(slug, section=section),
        "protocols": protocols,
        "protocol_overrides": overrides,
        "total_models": len(models),
        # "An address must be supplied" -- the gate's answer, not the shape's:
        # an endpoint-credential spec that ships a usable default (custom's
        # localhost gateway) runs on a bare key, and the picker must not
        # demand what the gate does not.
        "needs_api_base": kind == SHAPE_LOCAL
        or (kind == SHAPE_ENDPOINT and not (spec and spec.usable_default_api_base)),
        # Whether to draw a key field. Answered here so the settings pane and
        # the wizard cannot disagree about it.
        "accepts_api_key": _accepts_api_key(slug, kind),
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
    from raven.core.config_stack import load_runtime_config

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


async def model_set_protocol(params: dict) -> dict:
    parsed = _parse(ModelSetProtocolParams, params)
    from raven.config.update_providers import get_provider_config

    section = get_provider_config(parsed.slug, redact_secrets=False)
    overrides = dict(section.get("model_protocols") or {})
    if parsed.protocol == "auto":
        overrides.pop(parsed.model, None)
    else:
        overrides[parsed.model] = parsed.protocol
    try:
        await asyncio.to_thread(set_provider_fields, parsed.slug, {"model_protocols": overrides})
    except (KeyError, ValidationError, RuntimeError) as exc:
        raise ConfigValidationError(str(exc), data={"slug": parsed.slug, "model": parsed.model}) from exc
    current_model, current_provider = _current_selection()
    entries = await _entries_off_loop(current_provider)
    provider = next((entry for entry in entries if entry["slug"] == parsed.slug), None)
    if provider is None:
        raise ConfigValidationError(f"unknown provider {parsed.slug!r}")
    return {"provider": provider}


async def model_save_key(params: dict) -> dict:
    parsed = _parse(ModelSaveKeyParams, params)

    # No spec of our own is not a reason to refuse: the picker lists such a
    # provider once it is configured, and the write path below is what decides
    # whether the name is usable. Rejecting here while the other four handlers
    # accepted it is how an orphan section got created.
    spec = find_by_name(parsed.slug)
    label = spec.label if spec else parsed.slug
    if spec and spec.is_oauth:
        raise NotSupportedError(
            f"{label} uses OAuth; run `raven provider login {parsed.slug.replace('_', '-')}`",
            data={"slug": parsed.slug},
        )
    kind = auth_shape(parsed.slug)
    accepts_optional_local_key = _accepts_api_key(parsed.slug, kind)
    if kind == SHAPE_LOCAL and parsed.api_key and not accepts_optional_local_key:
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

    # A local server that declares it takes a token keeps what was sent; the
    # rest are address-only and clear any stale key, said explicitly rather than
    # by omission -- leaving the field alone kept whatever was there, so a
    # section that once held a key would go on sending it to a local machine.
    fields: dict[str, Any] = {
        "api_key": parsed.api_key if kind != SHAPE_LOCAL or (accepts_optional_local_key and parsed.api_key) else ""
    }
    if accepts_optional_local_key and not parsed.api_key:
        fields.pop("api_key")
    if parsed.api_base:
        fields["api_base"] = parsed.api_base

    try:
        await asyncio.to_thread(set_provider_fields, parsed.slug, fields)
    except RuntimeError as exc:
        raise NotSupportedError(str(exc), data={"slug": parsed.slug}) from exc
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


def _stated_overlay(parsed: "ModelAddModelParams") -> dict[str, object]:
    """What the caller said about this model, in the shape config stores.

    Empty when nothing was stated, which is the difference between "add this id"
    and "add this id and here is what it can do" -- the first must not write an
    overlay of empty lists over a description a person put there by hand.
    """
    from raven.providers.registry_data import CAPABILITIES, MODALITIES, clean_tags

    stated: dict[str, object] = {}
    if parsed.label:
        stated["label"] = parsed.label
    for field, values, allowed in (
        ("capabilities", parsed.capabilities, CAPABILITIES),
        ("inputModalities", parsed.input_modalities, MODALITIES),
        ("outputModalities", parsed.output_modalities, MODALITIES),
    ):
        if values is None:
            continue
        cleaned = clean_tags(values, allowed)
        if len(cleaned) != len(set(values)):
            unknown = sorted(set(values) - set(cleaned))
            raise ConfigValidationError(f"unknown {field}: {', '.join(unknown)}", data={"field": field})
        if cleaned:
            stated[field] = list(cleaned)
    return stated


async def model_fetch_models(params: dict) -> dict:
    """What this provider can serve: the bundled catalogue, plus whatever it says.

    Two sources, unioned, because they answer the same question with different
    weaknesses. The registry ships with Raven and needs no credential, so there
    is a list to pick from before a key is entered -- and something to show when
    the vendor cannot be reached, where an empty list would claim it serves
    nothing. The live call is current, and carries the models a registry refresh
    has not caught up with.

    The asking is ``config.update_providers.test_provider``, which is already
    the one place that knows where each vendor's catalogue lives and what opens
    it -- a Codex account's entitlements, a Copilot seat's exchanged token, an
    OAuth resource URL, Anthropic's and Google's own header shapes, and the
    OpenAI-compatible GET everything else answers. A second implementation here
    would be a second set of addresses to keep current.

    Not reaching the vendor is not an error to this method: no key configured is
    the ordinary state of a provider a person is still setting up, and the
    catalogue is exactly what they want to see. ``status`` says what happened so
    a surface can word its note, and every row says which source it came from.

    A read throughout -- nothing is written until a row is added.
    """
    from raven.config.loader import load_config
    from raven.config.update_providers import test_provider
    from raven.providers.catalog import describe
    from raven.providers.rates import resolve_context_window
    from raven.providers.registry_data import catalogue_for, kind_of
    from raven.providers.wire import merge_key

    parsed = _parse(ModelFetchModelsParams, params)
    slug = canonical_provider_name(parsed.slug)

    # Off-thread: this is a network round trip to somebody else's server, and
    # the gateway's loop is carrying a token stream while it happens. Cheap when
    # there is no credential to send -- the probe refuses before any socket.
    probe = await asyncio.to_thread(test_provider, slug, timeout_s=_FETCH_TIMEOUT_S, full_catalogue=True)
    asked = bool(probe.get("ok"))
    live = [m for m in (probe.get("model_ids") or []) if isinstance(m, str) and m]

    try:
        section = load_config().providers.get(slug)
    except Exception:
        section = None
    configured = {merge_key(slug, m) for m in (getattr(section, "models", None) or [])}
    overlays = _configured_overlays(slug, section=section)

    live_keys = {merge_key(slug, m) for m in live}
    # What the endpoint a model was listed at proves about it, which is better
    # evidence than its name: nothing in "voyageai/voyage-code-4" says
    # embedding, and being served from `/embeddings/models` says it outright.
    implied = probe.get("implied_capabilities") or {}
    rows: dict[str, dict[str, Any]] = {}
    for raw in [*live, *catalogue_for(slug)]:
        stored = _stored_spelling(slug, raw)
        # Keyed by identity: the vendor's spelling and the registry's are the
        # same model, and listing it twice is how a person adds it twice.
        key = merge_key(slug, stored)
        if key in rows:
            continue
        row = describe(slug, stored, overlay=_overlay_for(overlays, slug, stored))
        capabilities = row.capabilities
        proven = implied.get(raw)
        if proven and proven not in capabilities:
            from raven.providers.registry_data import CAPABILITIES

            merged = {*capabilities, proven}
            capabilities = tuple(name for name in CAPABILITIES if name in merged)
        entry: dict[str, Any] = {
            "id": stored,
            "label": row.label,
            "kind": kind_of(capabilities, row.output_modalities),
            "added": key in configured,
            "source": "live" if key in live_keys else "registry",
        }
        if row.description:
            entry["description"] = row.description
        if capabilities:
            entry["capabilities"] = list(capabilities)
        if row.input_modalities:
            entry["input_modalities"] = list(row.input_modalities)
        if row.output_modalities:
            entry["output_modalities"] = list(row.output_modalities)
        if window := resolve_context_window(stored, allow_fetch=False):
            entry["context_window"] = window
        rows[key] = entry

    # By name, and stably: the order a vendor lists its catalogue in is not an
    # order anybody reads, and it changes between calls for some of them.
    ordered = sorted(rows.values(), key=lambda r: (r["label"] or r["id"]).lower())
    return {
        "models": ordered,
        "status": "ok" if asked else str(probe.get("status") or "not_asked"),
        "error": None if asked else (str(probe.get("error") or "") or None),
    }


async def model_add_model(params: dict) -> dict:
    parsed = _parse(ModelAddModelParams, params)
    try:
        await asyncio.to_thread(
            add_provider_model,
            parsed.slug,
            _stored_spelling(parsed.slug, parsed.model),
            overlay=_stated_overlay(parsed) or None,
        )
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
    """Register the ten ``model.*`` handlers on a dispatcher instance."""
    dispatcher.register("model.options", partial(model_options, agent_loop_factory=agent_loop_factory))
    dispatcher.register("model.set_protocol", model_set_protocol)
    dispatcher.register("model.save_key", model_save_key)
    dispatcher.register("model.disconnect", model_disconnect)
    dispatcher.register("model.fetch_models", model_fetch_models)
    dispatcher.register("model.add_model", model_add_model)
    dispatcher.register("model.remove_model", model_remove_model)
    dispatcher.register("model.endpoints", model_endpoints)
    dispatcher.register("model.add_endpoint", model_add_endpoint)
    dispatcher.register("model.remove_endpoint", model_remove_endpoint)


__all__ = [
    "model_options",
    "model_set_protocol",
    "model_save_key",
    "model_disconnect",
    "model_fetch_models",
    "model_add_model",
    "model_remove_model",
    "model_endpoints",
    "model_add_endpoint",
    "model_remove_endpoint",
    "register_model_methods",
    "_build_provider_entry",
]
