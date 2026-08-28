"""Config-slice admission: validate and apply declared defaults at dispensing.

EM-2 completion (ruled 2026-08-27, config-with-cargo end state): a manifest
may declare ``config_schema`` — a flat mapping of key -> {type, default?,
required?} — and the registry admits the user's slice against it right
before the factory sees it. An empty declaration keeps today's verbatim
pass-through, so every plugin that declares nothing is untouched.

Tolerance rules (migration requirement #1): a missing slice reads as ``{}``;
admission consults nothing outside the slice itself, so breakage elsewhere
in the config cannot take the plugin path down; failures name the plugin
and the key, because "which cargo refused to board and why" is the whole
point of failing loudly at the door instead of deep inside a turn.
"""

from __future__ import annotations

import logging
from typing import Any

_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    # Container shapes are type-checked at the door but not descended into:
    # element and member validation stays with whoever consumes the value
    # (today the central pydantic model; after the storage handover, the
    # cargo's own factory).
    "array": list,
    "object": dict,
}


class PluginConfigError(Exception):
    """A config slice violates its plugin's declared ``config_schema``."""


def admit_slice(
    schema: dict[str, Any],
    slice_: dict[str, Any] | None,
    *,
    plugin_id: str,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Return the admitted slice: defaults applied, declared keys type-checked.

    Unknown keys pass through with a warning rather than failing: an operator
    ahead of their plugin version must not brick it. Declared keys are the
    contract half that bites.
    """
    admitted = dict(slice_ or {})
    if not schema:
        return admitted
    log = logger or logging.getLogger(f"raven.plugins.{plugin_id}")
    for key, spec in schema.items():
        if not isinstance(spec, dict):
            raise PluginConfigError(
                f"plugin {plugin_id!r}: config_schema[{key!r}] must be a table, "
                f"got {type(spec).__name__}"
            )
        if key not in admitted:
            if "default" in spec:
                admitted[key] = spec["default"]
            elif spec.get("required"):
                raise PluginConfigError(
                    f"plugin {plugin_id!r}: config key {key!r} is required and missing"
                )
            continue
        want = spec.get("type")
        if want is None:
            continue
        expected = _TYPES.get(want)
        if expected is None:
            raise PluginConfigError(
                f"plugin {plugin_id!r}: config_schema[{key!r}] names unknown type {want!r}"
            )
        value = admitted[key]
        # bool subclasses int in Python; a bare isinstance check would admit
        # ``true`` where an integer is declared, which is never what the
        # operator meant.
        if isinstance(value, bool) and want in ("integer", "number"):
            raise PluginConfigError(
                f"plugin {plugin_id!r}: config key {key!r} must be {want}, got boolean"
            )
        if not isinstance(value, expected):
            raise PluginConfigError(
                f"plugin {plugin_id!r}: config key {key!r} must be {want}, "
                f"got {type(value).__name__}"
            )
    for key in admitted:
        if key not in schema:
            log.warning(
                "plugin %s: config key %r is not in its config_schema; passing through",
                plugin_id,
                key,
            )
    return admitted
