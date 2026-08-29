"""Config-slice admission: validate and apply declared defaults at dispensing.

Config-with-cargo: a manifest
may declare ``config_schema`` — a flat mapping of key -> {type, default?,
required?} — and the registry admits the user's slice against it right
before the factory sees it. An empty declaration keeps today's verbatim
pass-through, so every plugin that declares nothing is untouched.

Tolerance rules: a missing slice reads as ``{}``;
admission consults nothing outside the slice itself, so breakage elsewhere
in the config cannot take the plugin path down; failures name the plugin
and the key, because "which cargo refused to board and why" is the whole
point of failing loudly at the door instead of deep inside a turn.
"""

from __future__ import annotations

import copy
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
                f"plugin {plugin_id!r}: config_schema[{key!r}] must be a table, got {type(spec).__name__}"
            )
        if key not in admitted:
            if "default" in spec:
                # Copied, not shared: a mutable default (list, table) handed
                # to two dispensings must not alias one object across cargo.
                admitted[key] = copy.deepcopy(spec["default"])
            elif isinstance(spec.get("fields"), dict):
                # A declared sub-table materializes from its own defaults, so
                # an absent [channels.slack.dm] still answers dm.policy.
                admitted[key] = admit_slice(spec["fields"], {}, plugin_id=f"{plugin_id}.{key}", logger=log)
            elif spec.get("required"):
                raise PluginConfigError(f"plugin {plugin_id!r}: config key {key!r} is required and missing")
            continue
        want = spec.get("type")
        if want is None:
            continue
        expected = _TYPES.get(want)
        if expected is None:
            raise PluginConfigError(f"plugin {plugin_id!r}: config_schema[{key!r}] names unknown type {want!r}")
        value = admitted[key]
        # bool subclasses int in Python; a bare isinstance check would admit
        # ``true`` where an integer is declared, which is never what the
        # operator meant.
        if isinstance(value, bool) and want in ("integer", "number"):
            raise PluginConfigError(f"plugin {plugin_id!r}: config key {key!r} must be {want}, got boolean")
        if value is None and not spec.get("required"):
            # None on an optional key is "unset", not a type violation: the
            # central models spell optionality as `str | None`, and the door
            # must not turn an explicit unset into a rejection or a default.
            continue
        if not isinstance(value, expected):
            raise PluginConfigError(
                f"plugin {plugin_id!r}: config key {key!r} must be {want}, got {type(value).__name__}"
            )
        choices = spec.get("choices")
        if choices is not None and value is not None and value not in choices:
            raise PluginConfigError(
                f"plugin {plugin_id!r}: config key {key!r} must be one of {choices!r}, got {value!r}"
            )
        if want == "object" and isinstance(spec.get("fields"), dict):
            # A fixed sub-table declares its own fields and admits
            # recursively: sub-defaults apply, sub-types bite, exactly the
            # door rules. Field-less objects stay opaque (member validation
            # is the consumer's).
            admitted[key] = admit_slice(spec["fields"], value, plugin_id=f"{plugin_id}.{key}", logger=log)
    for key in admitted:
        if key not in schema:
            log.warning(
                "plugin %s: config key %r is not in its config_schema; passing through",
                plugin_id,
                key,
            )
    return admitted


class _NestedView:
    """Frozen attribute-and-mapping view over one nested cargo table.

    A file-set object key arrives at the door as a plain mapping, but the
    adapters read nested cargo the way they read everything else -- by
    attribute (``config.mention.require_in_groups``) -- and some also by
    mapping (``config.groups.get(chat_id)``). This view serves both without
    caring which pydantic sub-model used to back the shape.
    """

    __slots__ = ("_table",)

    def __init__(self, table: dict[str, Any]) -> None:
        object.__setattr__(self, "_table", table)

    def __getattr__(self, name: str) -> Any:
        table = object.__getattribute__(self, "_table")
        try:
            return _view(table[name])
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"admitted slice is frozen (tried to set {name!r})")

    def __getitem__(self, key: str) -> Any:
        return _view(object.__getattribute__(self, "_table")[key])

    def get(self, key: str, default: Any = None) -> Any:
        table = object.__getattribute__(self, "_table")
        return _view(table[key]) if key in table else default

    def __contains__(self, key: object) -> bool:
        return key in object.__getattribute__(self, "_table")

    def __iter__(self):
        return iter(object.__getattribute__(self, "_table"))

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_table"))

    def __bool__(self) -> bool:
        return bool(object.__getattribute__(self, "_table"))

    def keys(self):
        return object.__getattribute__(self, "_table").keys()

    def values(self):
        return [_view(v) for v in object.__getattribute__(self, "_table").values()]

    def items(self):
        return [(k, _view(v)) for k, v in object.__getattribute__(self, "_table").items()]

    def __eq__(self, other: object) -> bool:
        table = object.__getattribute__(self, "_table")
        if isinstance(other, _NestedView):
            return table == object.__getattribute__(other, "_table")
        return table == other

    def __repr__(self) -> str:
        return f"_NestedView({object.__getattribute__(self, '_table')!r})"


def _view(value: Any) -> Any:
    """Wrap plain mappings (and mappings inside lists) for attribute access.

    Values that carry their own attributes pass through untouched.
    """
    if isinstance(value, dict):
        return _NestedView(value)
    if isinstance(value, list):
        return [_view(v) for v in value]
    return value


class DispensedSlice:
    """The frozen view a factory receives once its cargo passed the door.

    Declared keys answer from the admitted slice (defaults applied, types
    checked); anything else -- the socket fields and any not-yet-declared
    field -- falls back to the central section, so admission changes where a
    value travels, never what it is. Nested tables come back as frozen
    views, so a file-set object key reads exactly like the sub-model it
    replaces.
    """

    __slots__ = ("_cargo", "_section")

    def __init__(self, section: Any, cargo: dict[str, Any]) -> None:
        object.__setattr__(self, "_section", section)
        object.__setattr__(self, "_cargo", dict(cargo))

    def __getattr__(self, name: str) -> Any:
        cargo = object.__getattribute__(self, "_cargo")
        if name in cargo:
            return _view(cargo[name])
        return getattr(object.__getattribute__(self, "_section"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"admitted slice is frozen (tried to set {name!r})")


def _snake(key: str) -> str:
    import re

    return re.sub(r"(?<=[a-z0-9])([A-Z])", lambda m: "_" + m.group(1).lower(), key)


def normalize_slice_keys(schema: dict[str, Any], table: dict[str, Any]) -> dict[str, Any]:
    """Snake-case the field keys of a raw file section, guided by the schema.

    Config files hold camelCase (the pydantic-era writers dumped by alias);
    declarations speak snake_case. Only keys that resolve to a declared field
    are renamed, and only declared sub-tables are descended into -- map keys
    (mochat group room ids) pass verbatim.
    """
    out: dict[str, Any] = {}
    for key, value in table.items():
        resolved = key if key in schema else _snake(key)
        if resolved not in schema:
            out[key] = value
            continue
        sub = schema[resolved].get("fields")
        if isinstance(sub, dict) and isinstance(value, dict):
            out[resolved] = normalize_slice_keys(sub, value)
        else:
            out[resolved] = value
    return out


def dispense_channel_config(spec: Any, section: Any, *, channel: str) -> Any:
    """Route a channel section through the admission door before its factory.

    A channel with no declaration keeps the verbatim section -- same rule as
    plugin slices. The slice fed to the door is the declared subset of the
    file's values; everything else materializes from the declaration's own
    defaults, which are the only defaults there are.
    """
    schema = getattr(spec, "config_schema", None) or {}
    if not schema:
        return section
    from raven.config.loader import channel_cargo_slice

    slice_ = normalize_slice_keys(schema, channel_cargo_slice(channel))
    raw = {k: v for k, v in slice_.items() if k in schema}
    cargo = admit_slice(schema, raw, plugin_id=f"channel:{channel}")
    return DispensedSlice(section, cargo)
