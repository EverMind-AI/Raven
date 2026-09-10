"""What a model is called, what it can do, and what it is for.

A picker showing `anthropic/claude-sonnet-4-6` is showing an identifier. What a
person choosing a model wants is its name, roughly what it is good at, what it
can take in and hand back, and how recent it is -- none of which LiteLLM's table
carries, because that table exists to price and route.

So there are two catalogue sources and they answer different questions:

* LiteLLM's own table decides prices and limits used in a request. It ships with
  the dependency and needs no network. It also carries capability flags, which
  Raven deliberately does not read: its `supports_prompt_caching` asks whether a
  model caches at all, while what a request needs to know is whether the provider
  accepts `cache_control` blocks -- `ProviderSpec`'s field of the same name.
* the bundled provider registry (`providers/registry_data.py`, three packaged
  files) decides labels and display tags, and prices a finished call. It carries
  a name, a one-line description, what the model reads and writes, and the
  vendor's published cost. The context window is deliberately absent: it sizes
  trimming, which shapes the *next* request, so `providers/rates.py` answers it
  from the tables that also route.

Keeping the split is the point rather than an implementation detail. The registry
is community-maintained data; if it goes stale, wrong, or missing, the cost is a
model shown by its id instead of its name, a missing icon, or a total that is
off. It can never cause a wrong request, because nothing that shapes one reads it.

A capability tag is display only, and specifically not the answer to "may this
request carry an image". That question is `capabilities.supports_vision`, which
reads a catalogue Raven fetches for rates, and `ProviderSpec`, which knows what
the wire can hold -- a model that can see is still not reachable with a picture
over a transport with nowhere to put one.

The registry ships with Raven so a fresh install labels models offline and tests
never reach the network. Regenerate with
``scripts/refresh_provider_registry.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.config.schema import ModelOverlay

#: Where a row's facts came from, kept on the row so a surface can tell a
#: label it can trust from an id it is falling back to.
SOURCE_SNAPSHOT = "snapshot"
SOURCE_ID_ONLY = "id-only"
#: The user described it themselves, which beats any catalogue.
SOURCE_OVERLAY = "overlay"


@dataclass(frozen=True)
class ModelRow:
    """One model, as a person reads it.

    The tags are the registry's answer and are rendered as icons; they are empty
    for a model nothing describes, which a surface must read as "unknown" rather
    than as "cannot". Being wrong the confident way would hide a capability the
    model has; being wrong this way shows one fewer icon.
    """

    ref: str
    provider: str
    label: str
    source: str
    description: str = ""
    capabilities: tuple[str, ...] = ()
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()

    @property
    def described(self) -> bool:
        return self.source != SOURCE_ID_ONLY

    @property
    def tagged(self) -> bool:
        """Whether there is anything to draw. A row can be tagged and unlabelled:
        a gateway lists a model the vendor rows describe without naming it."""
        return bool(self.capabilities or self.input_modalities or self.output_modalities)


def describe(provider: str, model: str, *, overlay: "ModelOverlay | None" = None) -> ModelRow:
    """Everything known about this model for display purposes.

    Falls back to the id as its own label, so a caller can render the result
    unconditionally: a model the snapshot has never heard of -- one released
    since the last refresh, or served by a local deployment -- still comes back
    as a row rather than as nothing to show.
    """
    from raven.providers.registry import canonical_provider_name
    from raven.providers.registry_data import inferred_tags, row_by_name, row_for
    from raven.providers.wire import split_model_id, stored_model_id

    provider = canonical_provider_name(provider)
    ref = stored_model_id(provider, model)
    vendor_id = _vendor_id(provider, model)
    entry = row_for(provider, vendor_id)
    if entry is None:
        # The same model under whoever else lists it. Upstream files models per
        # provider and its coverage is uneven -- SiliconFlow gets twelve rows
        # and none of them are the image models it actually serves, though
        # those models are described in full elsewhere. Display facts only: the
        # price on a borrowed row is the other provider's, which is why
        # `model_cost` below does not take this path.
        entry = row_by_name(vendor_id)

    if entry is not None:
        row = ModelRow(
            ref=ref,
            provider=provider,
            # A row without a name is still a row: the tags it carries are worth
            # drawing, and the id is what the picker showed before either way.
            label=entry.name or split_model_id(ref)[1] or ref,
            source=SOURCE_SNAPSHOT if entry.name else SOURCE_ID_ONLY,
            description=entry.description,
            capabilities=_with_inferred(ref, entry.capabilities),
            input_modalities=entry.input_modalities,
            output_modalities=entry.output_modalities,
        )
    else:
        # A model no catalogue carries -- a live fetch from a gateway serving
        # hundreds of them -- still has a name, and for one whole class of
        # model the name is the only thing that says what it is.
        row = ModelRow(
            ref=ref,
            provider=provider,
            label=split_model_id(ref)[1] or ref,
            source=SOURCE_ID_ONLY,
            capabilities=inferred_tags(ref),
        )

    return _with_overlay(row, overlay)


def _with_inferred(ref: str, capabilities: tuple[str, ...]) -> tuple[str, ...]:
    """The catalogue's tags, plus what its silence leaves the name to answer.

    Added rather than substituted, and only where the catalogue has claimed
    neither: a multimodal embedder really does read images, so
    ``cohere-embed-v4`` keeps ``image-recognition`` and gains ``embedding``. A
    model the catalogue already calls an embedder is left exactly as it is.
    """
    from raven.providers.registry_data import CAPABILITIES, inferred_tags

    if {"embedding", "rerank"} & set(capabilities):
        return capabilities
    extra = inferred_tags(ref)
    if not extra:
        return capabilities
    merged = set(capabilities) | set(extra)
    return tuple(name for name in CAPABILITIES if name in merged)


def model_cost(model: str) -> dict | None:
    """The vendor's own published rates for this model, or None.

    Keyed by provider, which is the point: reading a price out of a flat
    cross-vendor table answers a self-hosted deployment with a hosted vendor's
    figure. ``model`` is a stored id, so the provider it names is the one asked.

    Prices are the one runtime number this file carries, and only because they
    are reported after a call rather than used to shape one -- see the module
    docstring for where that line is drawn.
    """
    from raven.providers.registry import canonical_provider_name, find_by_model, split_model_id

    # The id has to name its provider. `find_by_model` falls back to keyword
    # matching for a bare id, which reads across vendors: "qwen3-32b" matched
    # DashScope and was priced at DashScope's rate whoever was actually serving
    # it. That is the same borrowing the openrouter tier was gated to stop, one
    # tier down.
    #
    # A bare id left by an older version therefore prices as unknown. That is not
    # worth a compatibility path: both surfaces that write a model now store it
    # qualified, so picking the model once restores the figure.
    if not split_model_id(model)[0]:
        return None
    spec = find_by_model(model)
    provider = spec.name if spec else split_model_id(model)[0]
    if not provider:
        return None
    from raven.providers.registry_data import row_for

    entry = row_for(canonical_provider_name(provider), _vendor_id(provider, model))
    return dict(entry.cost) if entry is not None and entry.cost else None


def _with_overlay(row: ModelRow, overlay: "ModelOverlay | None") -> ModelRow:
    """Let what the user stated beat what a catalogue guessed.

    The user is describing their own deployment, so they are the authority on
    it -- and for a model no catalogue carries, they are the only one. Fields
    left unset in the overlay keep the catalogue's answer rather than blanking
    it, so stating one fact does not erase the rest.
    """
    if overlay is None:
        return row

    from raven.providers.registry_data import CAPABILITIES, MODALITIES, clean_tags

    changed = {
        "label": overlay.label or row.label,
        "description": overlay.description or row.description,
        # Stated tags replace the catalogue's rather than joining them: a person
        # correcting a row that says "vision" is saying it does not, and a union
        # would make that correction unsayable.
        "capabilities": clean_tags(overlay.capabilities, CAPABILITIES) or row.capabilities,
        "input_modalities": clean_tags(overlay.input_modalities, MODALITIES) or row.input_modalities,
        "output_modalities": clean_tags(overlay.output_modalities, MODALITIES) or row.output_modalities,
    }
    described = row.described or bool(overlay.label or overlay.description)
    return replace(row, **changed, source=SOURCE_OVERLAY if described else row.source)


def _vendor_id(provider: str, model: str) -> str:
    """The vendor's own id, which is how the snapshot is keyed.

    A stored id names its provider and the snapshot does not repeat that, so the
    prefix comes off before the lookup -- including a gateway's, whose rows are
    filed under the upstream vendor's id.

    ``head`` is always normalized (``split_model_id`` runs it through
    ``normalize_provider_name``), so ``provider`` must be too before the
    fallback comparison -- the same normalization ``wire.merge_key`` applies to
    both sides of its own identity check. Comparing raw missed a provider
    Raven carries no spec for whenever it was spelled differently from its
    model prefix, e.g. hyphenated ``provider`` against an underscored prefix.
    """
    from raven.providers.registry import find_by_name, normalize_provider_name
    from raven.providers.wire import split_model_id

    spec = find_by_name(provider)
    head, rest = split_model_id(model or "")
    if head and spec and head in spec.route_names:
        return rest
    if head and head == normalize_provider_name(provider):
        return rest
    return model or ""
