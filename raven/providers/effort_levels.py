"""Which reasoning-effort levels a model declares, from LiteLLM's static table
or the Codex account catalogue.

LiteLLM's table is read offline from ``import_litellm().model_cost``;
``get_model_info`` is deliberately not used because it may query a provider's
endpoint. LiteLLM's model-name resolver is asked about bare ids only: it
resolves the provider through ``get_llm_provider``, which for LiteLLM's OAuth
spellings (``github_copilot/``, ``chatgpt/``) starts an interactive login, and
a prefixed id already names its provider. The Codex account catalogue is the one provider-owned source: its
``supported_reasoning_levels`` are the account's own declaration for models no
static table carries. It is network data, so the retry descent reads only the
cache ``codex_catalog`` keeps; ``refresh`` lets a caller warm that cache. A
level the account states outside this build's ladder (``ultra``) is not
offered.

The answer is tri-state for the static table: a tuple of levels when the model
or the user declares them, ``()`` when the model declares none, and ``None``
when the table itself cannot be read (an internal failure, not a model answer).
The Codex account source never answers ``None``: an expired answer is kept as
last-good, and a cold cache has no levels to offer (``()``), so a stored pick is
never sent against an unreadable account. Level semantics mirror LiteLLM's own
validator: medium/high are the baseline, minimal/low are opt-out, xhigh/max are
opt-in. ``none`` is ignored: Raven's None means "provider default", not
"reasoning off".
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from functools import lru_cache
from typing import Any, Sequence

from raven.contracts.llm_provider import REASONING_EFFORT_LADDER

#: The per-level flags, in the order the derivation asks about them.
EFFORT_FLAGS = (
    "supports_minimal_reasoning_effort",
    "supports_low_reasoning_effort",
    "supports_xhigh_reasoning_effort",
    "supports_max_reasoning_effort",
)

#: The public order, least first. A declared overlay is normalized to it, and
#: the ladder is the only place the words are stated.
LEVEL_ORDER: tuple[str, ...] = tuple(reversed(REASONING_EFFORT_LADDER))


def levels_for(model: str, declared: Sequence[str] = (), *, refresh: bool = False) -> tuple[str, ...] | None:
    """The levels this model declares, or ``()``/``None`` per the module docstring.

    ``refresh`` lets a caller ask the Codex account catalogue, off the event
    loop; the retry descent leaves it false and reads the cache only, where an
    expired answer is still its last-good one.
    """
    if declared:
        known = set(declared)
        levels = tuple(name for name in LEVEL_ORDER if name in known)
        if levels:
            return levels
        # A hand-edited overlay with only words outside the ladder states no
        # level: fall through to the table, as an empty declaration does.
    from raven.providers.registry import canonical_provider_name, split_model_id

    provider, rest = split_model_id(model)
    if canonical_provider_name(provider) == "openai_codex":
        from raven.providers import codex_catalog

        return codex_catalog.account_efforts(rest, refresh=refresh)
    index = _index()
    if index is None:
        return None
    by_key, by_name = index
    route = _route_row(model, by_key)
    if route is not None and route.get("supports_reasoning") is False:
        return ()
    authority = _authority_row(model, by_name)
    if route is not None:
        merged = _merged(route, authority)
        return _derive(merged) if merged.get("supports_reasoning") is True else ()
    if authority is not None and authority.get("supports_reasoning") is True:
        return _derive(authority)
    return ()


def reset() -> None:
    """Drop the cached table/index; tests and a LiteLLM reload use it."""
    _index.cache_clear()


def downgrade(value: str | None, levels: tuple[str, ...] | None) -> str | None:
    """The value where the model declares it, else the highest level below it.

    For a fallback hop: the primary's word is not evidence about this model,
    and dropping straight to None would make the recovery call a different
    request from the one just attempted. A model declaring nothing at or below
    the word gets None; an unreadable table keeps the value (fail open).
    """
    if levels is None or value in levels:
        return value
    if value not in LEVEL_ORDER:
        return None
    rank = LEVEL_ORDER.index(value)
    below = [name for name in levels if name in LEVEL_ORDER and LEVEL_ORDER.index(name) < rank]
    return max(below, key=LEVEL_ORDER.index) if below else None


def _table() -> dict[str, dict[str, Any]] | None:
    try:
        from raven.providers.litellm_setup import import_litellm

        table = getattr(import_litellm(), "model_cost", None)
    except Exception:
        return None
    if not isinstance(table, dict) or not table:
        return None
    return {key: row for key, row in table.items() if isinstance(row, dict)}


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, dict[str, Any]], dict[str, tuple[tuple[str, dict[str, Any]], ...]]] | None:
    table = _table()
    if not table:
        return None
    by_name: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for key, row in table.items():
        by_name[_normalized_name(key)].append((key, row))
    return table, {key: tuple(rows) for key, rows in by_name.items()}


def _candidates(model: str) -> Iterator[tuple[str, bool]]:
    """Table keys to try, each with whether a bare key needs provider agreement.

    A generator on purpose: ``_route_row`` stops at the first key that matches,
    so LiteLLM's name resolver is never asked about a model Raven's own
    spellings already found. That resolver calls ``get_llm_provider``, which
    starts an interactive login for LiteLLM's OAuth spellings
    (``github_copilot/``, ``chatgpt/``) -- and this runs on the turn's thread.
    A prefixed id already names its provider, so the resolver is left to bare
    ids, where the provider lookup is a table read.
    """
    from raven.providers.registry import metadata_model_id, split_model_id

    provider, rest = split_model_id(model)
    out: list[tuple[str, bool]] = [(model, False)]
    metadata = metadata_model_id(model)
    if metadata:
        out.append((metadata, False))
    raw_head = (model or "").partition("/")[0]
    if provider and rest and raw_head and raw_head != provider:
        out.append((f"{provider}/{rest}", False))
    from raven.providers.wire import metadata_candidates

    for candidate in metadata_candidates(model):
        out.append((candidate, "/" not in candidate))

    seen: set[str] = set()
    for candidate, needs_agreement in out:
        if candidate in seen:
            continue
        seen.add(candidate)
        yield candidate, needs_agreement

    if provider:
        return

    try:
        from litellm.utils import _get_potential_model_names

        names = _get_potential_model_names(model=model, custom_llm_provider=None)
        for name in (
            "combined_model_name",
            "stripped_model_name",
            "combined_stripped_model_name",
            "split_model",
        ):
            value = names.get(name)
            if isinstance(value, str) and value and value not in seen:
                seen.add(value)
                yield value, "/" not in value
    except Exception:
        pass


def _provider_names(provider: str) -> set[str]:
    from raven.providers.registry import canonical_provider_name, find_by_name

    spec = find_by_name(provider) if provider else None
    names = set(spec.route_names) if spec is not None else set()
    if spec is not None:
        metadata_prefix = getattr(spec, "metadata_prefix", None)
        if metadata_prefix:
            names.add(metadata_prefix)
    if provider:
        names.add(canonical_provider_name(provider))
    return {canonical_provider_name(name) for name in names if name}


def _provider_agrees(row_provider: Any, model: str) -> bool:
    from raven.providers.registry import canonical_provider_name, find_by_model, split_model_id

    if not isinstance(row_provider, str) or not row_provider:
        return False
    provider, _rest = split_model_id(model)
    if not provider:
        spec = find_by_model(model)
        provider = getattr(spec, "name", "") if spec else ""
    return canonical_provider_name(row_provider) in _provider_names(provider)


def _route_row(model: str, by_key: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for candidate, needs_agreement in _candidates(model):
        row = by_key.get(candidate)
        if row is None:
            continue
        if needs_agreement and not _provider_agrees(row.get("litellm_provider"), model):
            continue
        return row
    return None


def _derive(row: dict[str, Any]) -> tuple[str, ...]:
    levels: list[str] = []
    if row.get("supports_minimal_reasoning_effort") is not False:
        levels.append("minimal")
    if row.get("supports_low_reasoning_effort") is not False:
        levels.append("low")
    levels += ["medium", "high"]
    if row.get("supports_xhigh_reasoning_effort") is True:
        levels.append("xhigh")
    if row.get("supports_max_reasoning_effort") is True:
        levels.append("max")
    return tuple(levels)


def _canonical_vendor(model: str) -> str:
    from raven.providers.catalog import _vendor_id
    from raven.providers.registry import canonical_provider_name, find_by_model, split_model_id
    from raven.providers.registry_data import row_for

    provider, _rest = split_model_id(model)
    if not provider:
        spec = find_by_model(model)
        provider = getattr(spec, "name", "") if spec else ""
    if not provider:
        return ""
    row = row_for(canonical_provider_name(provider), _vendor_id(provider, model))
    return split_model_id(row.model_id)[0] if row is not None else ""


def _row_provider_names(row: dict[str, Any]) -> set[str]:
    name = row.get("litellm_provider")
    return _provider_names(name) if isinstance(name, str) and name else set()


def _authority_row(
    model: str,
    by_name: dict[str, tuple[tuple[str, dict[str, Any]], ...]],
) -> dict[str, Any] | None:
    from raven.providers.registry import split_model_id

    route, rest = split_model_id(model)
    rows = by_name.get(_normalized_name(rest or model), ())
    declaring = [(key, row) for key, row in rows if row.get("supports_reasoning") is True]
    if not declaring:
        return None
    vendor = _canonical_vendor(model)
    vendor_names = _provider_names(vendor) if vendor else set()
    accepted = vendor_names | _provider_names(route)
    matching = [(key, row) for key, row in declaring if _row_provider_names(row) & accepted]
    if not matching:
        return None
    vendor_rows = [pair for pair in matching if _row_provider_names(pair[1]) & vendor_names]
    pool = vendor_rows or matching
    return max(
        pool,
        key=lambda pair: (
            sum(1 for flag in EFFORT_FLAGS if pair[1].get(flag) is not None),
            pair[0],
        ),
    )[1]


def _merged(route: dict[str, Any], authority: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(route)
    if authority is None:
        return merged
    for flag in ("supports_reasoning", *EFFORT_FLAGS):
        if merged.get(flag) is None:
            merged[flag] = authority.get(flag)
    return merged


def _normalized_name(model_id: str) -> str:
    from raven.providers.registry_data import _normalized_name as normalize

    return normalize(model_id)
