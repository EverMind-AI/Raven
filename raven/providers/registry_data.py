"""The bundled provider registry: three packaged files, one resolved row.

``providers/data`` carries what a person reads when choosing a model, split the
way the questions split (see ``scripts/refresh_provider_registry.py``, which
writes all three):

* ``models.json`` -- one row per canonical model. What Claude Opus 5 *is*, once,
  no matter who resells it.
* ``provider-models.json`` -- who serves it, under which id on the wire, at what
  price, and where it ranks in the curated shortlist. Two arrays: ``overrides``
  is regenerated wholesale, ``curated`` is hand-written and copied through.
* ``providers.json`` -- how a provider reads to a person, plus its links.

This module answers one question -- "what is known about (provider, wire id)?" --
by folding a provider's row onto the canonical model it names, and the curated
layer onto that. Nothing here shapes a request. The context window in particular
is absent by construction: it sizes trimming, so ``providers/rates.py`` owns it
and reads tables that also route. A stale figure here costs a wrong icon or an
inaccurate total, never a mis-sent request.

Never raises. A damaged or absent file costs labels, not startup, which is why
every loader folds failure into an empty answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

DATA = Path(__file__).parent / "data"
MODELS_FILE = DATA / "models.json"
PROVIDER_MODELS_FILE = DATA / "provider-models.json"
PROVIDERS_FILE = DATA / "providers.json"

#: The closed capability vocabulary, shared with the generator and with the two
#: UIs that draw an icon per name. Closed because an icon table cannot render a
#: string nobody has drawn: a tag outside this set is dropped on the way in
#: rather than reaching a surface that would silently skip it.
CAPABILITIES: tuple[str, ...] = (
    "function-call",
    "reasoning",
    "structured-output",
    "image-recognition",
    "audio-recognition",
    "video-recognition",
    "file-input",
    "image-generation",
    "audio-generation",
    "video-generation",
    "embedding",
    "rerank",
    "computer-use",
)

MODALITIES: tuple[str, ...] = ("text", "image", "video", "audio", "vector")

#: Fields a provider row or a curated row may restate about the model it names.
#: Anything else on such a row addresses the pairing, not the model.
_MODEL_FIELDS = (
    "name",
    "ownedBy",
    "description",
    "family",
    "capabilities",
    "inputModalities",
    "outputModalities",
    "maxOutputTokens",
    "openWeights",
    "pricing",
)


@dataclass(frozen=True)
class RegistryRow:
    """One (provider, wire id) pairing, with the canonical model folded in."""

    provider: str
    api_model_id: str
    model_id: str
    name: str = ""
    description: str = ""
    owned_by: str = ""
    family: str = ""
    capabilities: tuple[str, ...] = ()
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    max_output_tokens: int | None = None
    open_weights: bool = False
    #: Per million tokens, in USD, keyed ``input``/``output`` -- the shape
    #: ``catalog.model_cost`` has always returned, kept so its callers do not
    #: have to learn the registry's nested one.
    cost: dict[str, float] = field(default_factory=dict)
    #: Position in the curated shortlist, 1-based; None for everything else.
    rank: int | None = None
    #: The spelling a picker should offer, when it is not the one
    #: ``wire.stored_model_id`` derives. Two providers declare their own
    #: underscored prefix in ``skip_prefixes`` and LiteLLM's metadata table is
    #: keyed by it: ``github_copilot/gpt-4.1`` resolves a 128k window where the
    #: canonical ``github-copilot/gpt-4.1`` resolves none. Curated rows only.
    stored_id: str = ""


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - only on a damaged install
        logger.debug(f"provider registry file unavailable ({path.name}): {exc}")
        return {}


@lru_cache(maxsize=1)
def _models() -> dict[str, dict[str, Any]]:
    rows = _read(MODELS_FILE).get("models") or []
    return {str(row["id"]): row for row in rows if isinstance(row, dict) and row.get("id")}


@lru_cache(maxsize=1)
def _provider_models() -> dict[str, Any]:
    return _read(PROVIDER_MODELS_FILE)


@lru_cache(maxsize=1)
def _providers() -> dict[str, dict[str, Any]]:
    rows = _read(PROVIDERS_FILE).get("providers") or []
    return {str(row["id"]): row for row in rows if isinstance(row, dict) and row.get("id")}


def clean_tags(values: Any, allowed: tuple[str, ...]) -> tuple[str, ...]:
    """The listed names that are in the vocabulary, in the vocabulary's order.

    Ordered by the vocabulary rather than by the file so an icon row does not
    reshuffle between two models that carry the same tags.
    """
    if not isinstance(values, (list, tuple)):
        return ()
    present = {str(value) for value in values}
    return tuple(name for name in allowed if name in present)


def _flatten_pricing(pricing: Any) -> dict[str, float]:
    """The registry's nested price as the flat per-million dict callers expect."""
    if not isinstance(pricing, dict):
        return {}
    out: dict[str, float] = {}
    for side in ("input", "output"):
        entry = pricing.get(side)
        if isinstance(entry, dict) and isinstance(entry.get("perMillionTokens"), (int, float)):
            out[side] = float(entry["perMillionTokens"])
    return out


def _merge(base: dict[str, Any], *layers: dict[str, Any]) -> dict[str, Any]:
    merged = {key: value for key, value in base.items() if key in _MODEL_FIELDS}
    for layer in layers:
        merged.update({key: value for key, value in layer.items() if key in _MODEL_FIELDS})
    return merged


def _row(
    provider: str,
    api_model_id: str,
    model_id: str,
    merged: dict[str, Any],
    rank: int | None,
    stored_id: str = "",
) -> RegistryRow:
    output = merged.get("maxOutputTokens")
    return RegistryRow(
        provider=provider,
        api_model_id=api_model_id,
        model_id=model_id,
        stored_id=stored_id,
        name=str(merged.get("name") or ""),
        description=str(merged.get("description") or ""),
        owned_by=str(merged.get("ownedBy") or ""),
        family=str(merged.get("family") or ""),
        capabilities=clean_tags(merged.get("capabilities"), CAPABILITIES),
        input_modalities=clean_tags(merged.get("inputModalities"), MODALITIES),
        output_modalities=clean_tags(merged.get("outputModalities"), MODALITIES),
        max_output_tokens=output if isinstance(output, int) else None,
        open_weights=bool(merged.get("openWeights")),
        cost=_flatten_pricing(merged.get("pricing")),
        rank=rank,
    )


def _keyed(rows: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """One array of the file, keyed by the pairing it addresses."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        provider, api = str(raw.get("providerId") or ""), str(raw.get("apiModelId") or "")
        if provider and api:
            out[(provider, api)] = raw
    return out


@lru_cache(maxsize=1)
def _index() -> dict[tuple[str, str], RegistryRow]:
    """Every pairing the registry knows, keyed by provider and wire id.

    Built once for all three files together because the layers only mean
    anything stacked: the canonical row states what the model is, a generated
    row states what this provider charges and how its copy differs, and a
    curated row states what a person knows that no upstream publishes --
    including, for a Copilot seat or an Ollama pull, that the pairing exists at
    all.
    """
    models = _models()
    payload = _provider_models()
    generated = _keyed(payload.get("overrides"))
    curated = _keyed(payload.get("curated"))

    index: dict[tuple[str, str], RegistryRow] = {}
    for key in sorted(generated.keys() | curated.keys()):
        gen, cur = generated.get(key, {}), curated.get(key, {})
        # The curated link wins: a shortlist row for a model the provider's
        # upstream listing never carried is the only thing that names it.
        model_id = str(cur.get("modelId") or gen.get("modelId") or "")
        rank = cur.get("rank") if isinstance(cur.get("rank"), int) else None
        merged = _merge(models.get(model_id) or {}, gen, cur)
        index[key] = _row(key[0], key[1], model_id, merged, rank, str(cur.get("storedId") or ""))
    return index


#: Ids that name an embedding or a reranking model, for the one thing no
#: catalogue publishes.
#:
#: models.dev carries no flag for either: BGE M3 arrives with
#: ``modalities.output = ["text"]``, ``tool_call = false`` and nothing else, so
#: an entire class of model reached the surfaces with no tag at all and sat in
#: the Text bucket. Cherry Studio's registry has the answer as curated data --
#: 74 rows -- and curation cannot cover this case, because a live fetch returns
#: whatever the vendor has and a gateway like SiliconFlow serves hundreds of
#: these under names nobody wrote down.
#:
#: So it is read off the name, which is the only signal there is. Measured
#: against the whole bundled catalogue: 24 of 1392 models match, every one of
#: them an embedder or a reranker, and no chat model matches. The families are
#: named the same way everywhere because the weights are the same weights --
#: BAAI's bge, Alibaba's gte and Qwen3-Embedding, Cohere's embed, OpenAI's
#: text-embedding, and anything spelled "embed" or "rerank" outright.
#:
#: Read for display only. Being wrong here shows one wrong icon and files a
#: model under one wrong filter; it cannot shape a request.
_RERANK_NAME = re.compile(r"rerank", re.I)
_EMBEDDING_NAME = re.compile(r"(?:^|[-_/])(?:bge|gte|e5|m3e|text2vec|uae|jina-clip)(?:[-_.]|$)|embed", re.I)


def inferred_tags(model_id: str) -> tuple[str, ...]:
    """What this model's name says it is, when nothing else says anything.

    Reranking is asked first: a reranker is often named after the embedding
    family it reranks for (``bge-reranker-v2-m3``), so testing the other way
    round would file every one of them as an embedder.

    Only ever consulted where the catalogue is silent -- see
    ``catalog.describe``. A model the registry describes is described, and a
    guess from its name must not argue with it.
    """
    bare = model_id.rsplit("/", 1)[-1]
    if _RERANK_NAME.search(bare):
        return ("rerank",)
    if _EMBEDDING_NAME.search(bare):
        return ("embedding",)
    return ()


#: The buckets a model list is filtered by. One per model, derived rather than
#: stored: a model that answers in vectors is an embedding model whether or not
#: anything says so, and two sources for that would eventually disagree.
KINDS: tuple[str, ...] = ("text", "image", "embedding", "reranker", "audio", "video")


def _kind_from_outputs(outputs: "set[str]") -> str:
    if "vector" in outputs:
        return "embedding"
    for name in ("image", "audio", "video"):
        if name in outputs:
            return name
    return "text"


def kind_of(capabilities: "tuple[str, ...] | list[str]", outputs: "tuple[str, ...] | list[str]") -> str:
    """Which bucket this model belongs in, from what it can do and what it writes.

    Vision does not make a model an image model: reading pictures is something a
    text model does, and filtering "Image" to everything that can see would put
    most of a modern catalogue behind it. What a model *writes* is the question,
    so declared outputs outrank a derived ``*-generation`` capability.

    Embedding and rerank tags decide before outputs. The catalogue writes
    ``("text",)`` for all 24 of those rows, and a reranker's own output is text,
    so neither output can contradict the tag. Callers strip the tags a stronger
    source overrode before calling -- a declared output from a vendor or the
    user removes the bundled ones -- so a tag surviving here is one nobody
    contradicted.
    """
    caps, outs = set(capabilities), set(outputs)
    if "embedding" in caps:
        return "embedding"
    if "rerank" in caps:
        return "reranker"
    if outs:
        return _kind_from_outputs(outs)
    for name in ("image", "audio", "video"):
        if f"{name}-generation" in caps:
            return name
    return "text"


#: The spellings that differ between releases of one family, dropped before the
#: family lookup: a version, a date stamp, and the release words a vendor puts
#: in front of one.
_FAMILY_VERSION = re.compile(r"[\d.]+[a-z]?|v[\d.]+|\d{4,}|latest|preview|exp|experimental|beta|alpha|free")


def _family_tokens(model_id: str) -> tuple[str, ...]:
    """The words in a name that name its family, without the release.

    ``gpt-image-2.5-flare`` and ``gpt-image-1.5`` share ``gpt image``: the
    version is what changes between releases, so it is not part of the family.
    """
    bare = model_id.rsplit("/", 1)[-1].lower()
    return tuple(token for token in re.split(r"[-_:@ ]+", bare) if token and not _FAMILY_VERSION.fullmatch(token))


@lru_cache(maxsize=1)
def _family_kinds() -> dict[tuple[str, ...], frozenset[str]]:
    """Every kind the catalogue files a family of names under, keyed by tokens.

    One entry per full token tuple. A lookup walks up from the longest prefix,
    so a name carrying a suffix nobody wrote down still finds its family.
    """
    kinds: dict[tuple[str, ...], set[str]] = {}
    for row in _index().values():
        name = row.model_id or row.api_model_id
        if not name:
            continue
        tokens = _family_tokens(name)
        if tokens:
            kinds.setdefault(tokens, set()).add(kind_of(row.capabilities, row.output_modalities))
    return {tokens: frozenset(found) for tokens, found in kinds.items()}


#: The capability that states each kind, for the fallback below.
_KIND_TAGS: dict[str, tuple[str, ...]] = {
    "image": ("image-generation",),
    "audio": ("audio-generation",),
    "video": ("video-generation",),
    "embedding": ("embedding",),
    "reranker": ("rerank",),
}


def family_tags(model_id: str) -> tuple[str, ...]:
    """The kind the catalogue's own family agrees on, or nothing.

    A model newer than the snapshot carries no row; its family may. Asked from
    the longest token prefix down and answered only when every catalogue row in
    that family is the same kind: a prefix covering two kinds says nothing about
    a third name, and a wrong bucket is worse than no bucket.
    """
    tokens = _family_tokens(model_id)
    families = _family_kinds()
    for size in range(len(tokens), 0, -1):
        kinds = families.get(tokens[:size])
        if not kinds:
            continue
        if len(kinds) != 1:
            return ()
        return _KIND_TAGS.get(next(iter(kinds)), ())
    return ()


def row_for(provider: str, api_model_id: str) -> RegistryRow | None:
    """What the registry knows about this model under this provider, or None."""
    return _index().get((provider, api_model_id))


def curated_for(provider: str) -> tuple[RegistryRow, ...]:
    """The provider's curated shortlist, in the order it was written.

    Ordered by rank, not by file order: the array is sorted for a stable diff,
    and a shortlist reshuffled by a refresh would move the models a picker
    offers first.
    """
    rows = [row for (slug, _), row in _index().items() if slug == provider and row.rank is not None]
    return tuple(sorted(rows, key=lambda row: (row.rank or 0, row.api_model_id)))


def _normalized_name(model_id: str) -> str:
    """A model's name with the spelling differences between resellers removed.

    ``Qwen/Qwen-Image`` and ``nvidia/qwen/qwen-image`` are the same weights; so
    are ``FLUX.1-dev`` and ``flux_1-dev``. Only the last segment, because the
    segments before it name whoever is serving it.
    """
    return re.sub(r"[^a-z0-9]", "", model_id.rsplit("/", 1)[-1].lower())


@lru_cache(maxsize=1)
def _by_name() -> dict[str, RegistryRow]:
    """One row per model name, for a provider whose own listing is short.

    Upstream files models per provider, and its coverage is uneven: SiliconFlow
    gets twelve rows and none of them are its image models, though the same
    models are described in full under another provider. Keyed by name, a model
    the registry knows anywhere is a model it knows everywhere.

    Two guards. The richest row wins, because resellers describe the same model
    with different thoroughness and the fullest description is the one that is
    not missing anything. And a name whose rows disagree about what *kind* of
    model it is borrows nothing: seven of them do -- one reseller lists GPT-5.1
    as answering with images -- and filing a chat model under Image is a worse
    answer than filing it under nothing.
    """
    groups: dict[str, list[RegistryRow]] = {}
    for row in _index().values():
        if row.model_id:
            groups.setdefault(_normalized_name(row.model_id), []).append(row)

    out: dict[str, RegistryRow] = {}
    for key, rows in groups.items():
        if len({kind_of(r.capabilities, r.output_modalities) for r in rows}) > 1:
            continue
        out[key] = max(rows, key=lambda r: (len(r.capabilities), r.model_id))
    return out


def row_by_name(model_id: str) -> RegistryRow | None:
    """What the registry knows about this model under *any* provider, or None.

    A fallback, never a first answer: the provider's own row states its price
    and its wire id, and this one is somebody else's. Callers take display facts
    from it and nothing else -- see ``catalog.describe``, and note that
    ``catalog.model_cost`` deliberately does not consult it.
    """
    return _by_name().get(_normalized_name(model_id))


def catalogue_for(provider: str) -> tuple[str, ...]:
    """Every model the bundled registry files under this provider, by wire id.

    What a provider serves is knowable without asking it: the registry ships
    2000-odd (provider, model) pairs. So a catalogue list has something to show
    before a key is entered, and something to fall back on when the vendor
    cannot be reached -- an empty list would claim the provider serves nothing,
    which is never what a refused request means.
    """
    return tuple(sorted(api for (slug, api) in _index() if slug == provider))


def provider_metadata(provider: str) -> dict[str, Any]:
    """Display facts about the provider itself -- its name and its links.

    Deliberately not how to reach it: ``ProviderSpec`` owns base URLs, key
    variables and route names, and a second answer to any of those is a request
    sent somewhere the user did not configure.
    """
    return _providers().get(provider) or {}


def reset_cache() -> None:
    """Drop the parsed files. The tests' seam for a swapped data directory."""
    _models.cache_clear()
    _provider_models.cache_clear()
    _providers.cache_clear()
    _index.cache_clear()
    _family_kinds.cache_clear()
    _by_name.cache_clear()
