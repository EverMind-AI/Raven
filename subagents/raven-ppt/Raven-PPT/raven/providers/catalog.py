"""What a model is called and what it is for, for the surfaces people read.

A picker showing `anthropic/claude-sonnet-4-6` is showing an identifier. What a
person choosing a model wants is its name, roughly what it is good at, how much
context it takes and how recent it is -- none of which LiteLLM's table carries,
because that table exists to price and route.

So there are two catalogue sources and they answer different questions:

* LiteLLM's own table decides prices and limits used in a request. It ships with
  the dependency and needs no network. It also carries capability flags, which
  Raven deliberately does not read: its `supports_prompt_caching` asks whether a
  model caches at all, while what a request needs to know is whether the provider
  accepts `cache_control` blocks -- `ProviderSpec`'s field of the same name.
* the models.dev snapshot decides labels, and prices a finished call. It carries
  a name, a one-line description, and the vendor's published cost per model.
  Context windows and capability flags are deliberately absent: those shape the
  *next* request -- a window sizes trimming, a flag picks a wire shape -- and a
  second source for them is a second answer.

Keeping the split is the point rather than an implementation detail. The snapshot
is community-maintained data; if it goes stale, wrong, or missing, the cost is a
model shown by its id instead of its name, or a total that is off. It can never
cause a wrong request, because nothing that shapes one reads it.

The snapshot ships with Raven so a fresh install labels models offline and tests
never reach the network. Regenerate with
``scripts/refresh_models_dev_snapshot.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from raven.config.schema import ModelOverlay

SNAPSHOT = Path(__file__).parent / "data" / "models_dev.json"

#: Where a row's facts came from, kept on the row so a surface can tell a
#: label it can trust from an id it is falling back to.
SOURCE_SNAPSHOT = "snapshot"
SOURCE_ID_ONLY = "id-only"
#: The user described it themselves, which beats any catalogue.
SOURCE_OVERLAY = "overlay"


@dataclass(frozen=True)
class ModelRow:
    """One model, as a person reads it."""

    ref: str
    provider: str
    label: str
    source: str
    description: str = ""

    @property
    def described(self) -> bool:
        return self.source != SOURCE_ID_ONLY


@lru_cache(maxsize=1)
def _snapshot() -> dict[str, dict]:
    """The bundled labels, or nothing when they cannot be read.

    Never raises: a missing or corrupt snapshot must cost labels, not startup.
    """
    try:
        return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - only on a damaged install
        logger.debug(f"model label snapshot unavailable: {exc}")
        return {}


def describe(provider: str, model: str, *, overlay: "ModelOverlay | None" = None) -> ModelRow:
    """Everything known about this model for display purposes.

    Falls back to the id as its own label, so a caller can render the result
    unconditionally: a model the snapshot has never heard of -- one released
    since the last refresh, or served by a local deployment -- still comes back
    as a row rather than as nothing to show.
    """
    from raven.providers.registry import canonical_provider_name
    from raven.providers.wire import split_model_id, stored_model_id

    provider = canonical_provider_name(provider)
    ref = stored_model_id(provider, model)
    entry = _snapshot().get(provider, {}).get("models", {}).get(_vendor_id(provider, model))

    if entry:
        row = ModelRow(
            ref=ref,
            provider=provider,
            label=entry.get("name") or ref,
            source=SOURCE_SNAPSHOT,
            description=entry.get("description") or "",
        )
    else:
        row = ModelRow(ref=ref, provider=provider, label=split_model_id(ref)[1] or ref, source=SOURCE_ID_ONLY)

    return _with_overlay(row, overlay)


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
    entry = _snapshot().get(canonical_provider_name(provider), {}).get("models", {}).get(_vendor_id(provider, model))
    cost = entry.get("cost") if isinstance(entry, dict) else None
    return cost if isinstance(cost, dict) else None


def _with_overlay(row: ModelRow, overlay: "ModelOverlay | None") -> ModelRow:
    """Let what the user stated beat what a catalogue guessed.

    The user is describing their own deployment, so they are the authority on
    it -- and for a model no catalogue carries, they are the only one. Fields
    left unset in the overlay keep the catalogue's answer rather than blanking
    it, so stating one fact does not erase the rest.
    """
    if overlay is None:
        return row

    changed = {
        "label": overlay.label or row.label,
        "description": overlay.description or row.description,
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
