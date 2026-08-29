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
        if value is None and not spec.get("required"):
            # None on an optional key is "unset", not a type violation: the
            # central models spell optionality as `str | None`, and the door
            # must not turn an explicit unset into a rejection or a default.
            continue
        if want == "object" and hasattr(value, "model_dump"):
            # Transition form: while the central typed models still exist, an
            # object-declared key may arrive as a nested model. It dumps to a
            # mapping, which is the shape the declaration promises.
            continue
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

    Typed values -- central model instances during the transition, or
    anything else with its own attributes -- pass through untouched.
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
    field -- falls back to the central section, so the pilot changes where a
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


def dispense_channel_config(spec: Any, section: Any, *, channel: str) -> Any:
    """Route a channel section through the admission door before its factory.

    A channel with no declaration keeps the verbatim section -- same rule as
    plugin slices. The slice fed to the door is the declared subset of the
    section's values, so central defaults and declared defaults meet the
    consistency guard, not each other's shadow.
    """
    schema = getattr(spec, "config_schema", None) or {}
    if not schema:
        return section
    from raven.config.loader import channel_cargo_slice

    raw = {k: v for k, v in channel_cargo_slice(channel).items() if k in schema}
    for key, decl in schema.items():
        if decl.get("required") and key not in raw and hasattr(section, key):
            # Zero-drift transition: an unset required key keeps today's
            # semantics (the adapter fails at connect, not the door at start)
            # by falling back to the central section until that model retires.
            raw[key] = getattr(section, key)
    cargo = admit_slice(schema, raw, plugin_id=f"channel:{channel}")
    for key, decl in schema.items():
        if key not in cargo and "default" not in decl and hasattr(section, key):
            # No file value and no declared default: the central model still
            # owns this key's default until the write-side schema retires.
            cargo[key] = getattr(section, key)
    return DispensedSlice(section, cargo)
