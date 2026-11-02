"""Which reasoning-effort levels a model declares, from LiteLLM's static table.

The table is read offline from ``import_litellm().model_cost``; ``get_model_info``
is deliberately not used because it may query a provider's endpoint.

The answer is tri-state: a tuple of levels when the model or the user declares
them, ``()`` when the model declares none, and ``None`` when the table itself
cannot be read (an internal failure, not a model answer). Level semantics
mirror LiteLLM's own validator: medium/high are the baseline, minimal/low are
opt-out, xhigh/max are opt-in. ``none`` is ignored: Raven's None means "provider
default", not "reasoning off".
"""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from typing import Any, Sequence

#: The per-level flags, in the order the derivation asks about them.
EFFORT_FLAGS = (
    "supports_minimal_reasoning_effort",
    "supports_low_reasoning_effort",
    "supports_xhigh_reasoning_effort",
    "supports_max_reasoning_effort",
)

#: The public order, least first. A declared overlay is normalized to it.
LEVEL_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")


def levels_for(model: str, declared: Sequence[str] = ()) -> tuple[str, ...] | None:
    """The levels this model declares, or ``()``/``None`` per the module docstring."""
    if declared:
        known = set(declared)
        return tuple(name for name in LEVEL_ORDER if name in known)
    index = _index()
    if index is None:
        return None
    by_key, _by_name = index
    route = _route_row(model, by_key)
    if route is None or route.get("supports_reasoning") is not True:
        return ()
    return _derive(route)


def reset() -> None:
    """Drop the cached table/index; tests and a LiteLLM reload use it."""
    _index.cache_clear()


def _table() -> dict[str, dict[str, Any]] | None:
    try:
        from raven.providers.litellm_setup import import_litellm

        table = getattr(import_litellm(), "model_cost", None)
    except Exception:
        return None
    if not isinstance(table, dict):
        return None
    return {key: row for key, row in table.items() if isinstance(row, dict)}


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, dict[str, Any]], dict[str, tuple[tuple[str, dict[str, Any]], ...]]] | None:
    table = _table()
    if table is None:
        return None
    by_name: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for key, row in table.items():
        by_name[_normalized_name(key)].append((key, row))
    return table, {key: tuple(rows) for key, rows in by_name.items()}


def _candidates(model: str) -> list[tuple[str, bool]]:
    """Table keys to try, each with whether a bare key needs provider agreement."""
    from raven.providers.registry import metadata_model_id, split_model_id

    provider, rest = split_model_id(model)
    out: list[tuple[str, bool]] = [(model, False)]
    metadata = metadata_model_id(model)
    if metadata:
        out.append((metadata, False))
    alternate = provider.replace("-", "_")
    if alternate and alternate != provider and rest:
        out.append((f"{alternate}/{rest}", False))
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
            if isinstance(value, str) and value:
                out.append((value, "/" not in value))
    except Exception:
        pass
    seen: set[str] = set()
    unique: list[tuple[str, bool]] = []
    for candidate, needs_agreement in out:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append((candidate, needs_agreement))
    return unique


def _provider_names(provider: str) -> set[str]:
    from raven.providers.registry import canonical_provider_name, find_by_name

    spec = find_by_name(provider) if provider else None
    names = {canonical_provider_name(provider)} if provider else set()
    for name in (
        getattr(spec, "name", ""),
        getattr(spec, "via_driver", ""),
        *(getattr(spec, "name_aliases", ()) or ()),
    ):
        if name:
            names.add(canonical_provider_name(name))
    return {name for name in names if name}


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


def _normalized_name(model_id: str) -> str:
    from raven.providers.registry_data import _normalized_name as normalize

    return normalize(model_id)
