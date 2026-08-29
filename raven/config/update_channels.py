"""Atomic operations for channel config sections.

This module is the ONLY write path for channel configuration. All entry
points (CLI commands, future wizard, future WebUI, future REPL slash)
must call functions defined here. Direct load_config / save_config on
the channels section is forbidden: this module is the only write path.

Field truth lives with the cargo: every question this module answers --
which channels exist, which fields they carry, types, defaults, secrecy,
requiredness, nesting -- is answered from the adapter specs'
``config_schema`` plus the host's socket fields, not from central
pydantic classes. Validation is the same admission door the gateway
dispenses through, so the writer and the reader cannot disagree.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

from raven.config.loader import get_config_path, read_raw_or_raise
from raven.utils.atomic_io import atomic_update

# The socket: what the host plugs every channel into, whatever the
# transport. Uniform across the adapters; declared here and pinned by tests.
_SOCKET_SCHEMA: dict[str, dict[str, Any]] = {
    "enabled": {"type": "boolean", "default": False},
    "allow_from": {"type": "array", "default": ["*"]},
    "workspace": {"type": "string", "default": ""},
}

# Display names for the CLI table, mapping schema types onto the pythonic
# spellings the pydantic-era table used.
_TYPE_DISPLAY = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _specs() -> dict[str, Any]:
    from raven.channels.registry import discover_specs

    return discover_specs()


def _channel_schema(name: str) -> dict[str, dict[str, Any]]:
    """Socket fields plus the adapter's declared cargo schema."""
    specs = _specs()
    if name not in specs:
        raise KeyError(f"Unknown channel '{name}'. Available channels: {sorted(specs)}")
    return {**_SOCKET_SCHEMA, **(getattr(specs[name], "config_schema", None) or {})}


def _snake(key: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", lambda m: "_" + m.group(1).lower(), key)


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part.title() for part in rest)


def _camelize_keys(schema: dict[str, Any], table: dict[str, Any]) -> dict[str, Any]:
    """The write-side inverse of :func:`_normalize_keys`.

    Sections are serialized with camelCase field keys so this writer stays
    byte-compatible with ``save_config`` (which still dumps by alias);
    undeclared keys and map keys pass verbatim.
    """
    out: dict[str, Any] = {}
    for key, value in table.items():
        if key not in schema:
            out[key] = value
            continue
        sub = schema[key].get("fields")
        if isinstance(sub, dict) and isinstance(value, dict):
            out[_camel(key)] = _camelize_keys(sub, value)
        else:
            out[_camel(key)] = value
    return out


def _materialize(schema: dict[str, Any], section: dict[str, Any]) -> dict[str, Any]:
    """The effective values: file section over declared defaults, recursively."""
    out: dict[str, Any] = {}
    for key, decl in schema.items():
        sub = decl.get("fields")
        if isinstance(sub, dict):
            value = section.get(key)
            out[key] = _materialize(sub, value if isinstance(value, dict) else {})
        elif key in section:
            out[key] = copy.deepcopy(section[key])
        elif "default" in decl:
            out[key] = copy.deepcopy(decl["default"])
        else:
            out[key] = None
    for key, value in section.items():
        if key not in schema:
            out[key] = copy.deepcopy(value)
    return out


def _flatten_schema(schema: dict[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    """Flat ``dotted-path -> spec`` map for CLI parsers and redaction."""
    out: dict[str, dict[str, Any]] = {}
    for key, decl in schema.items():
        path = f"{prefix}{key}"
        sub = decl.get("fields")
        if isinstance(sub, dict):
            out.update(_flatten_schema(sub, prefix=f"{path}."))
            continue
        description = decl.get("description", "")
        if not description and decl.get("choices"):
            description = "Choices: " + ", ".join(str(c) for c in decl["choices"])
        type_display = _TYPE_DISPLAY.get(decl.get("type"), decl.get("type") or "str")
        if decl.get("choices"):
            type_display = "Literal"
        out[path] = {
            "type": type_display,
            "default": copy.deepcopy(decl.get("default")),
            "is_secret": decl.get("secret") is True,
            "required": decl.get("required") is True,
            "description": description,
        }
    return out


def _flatten_values(schema: dict[str, Any], values: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, decl in schema.items():
        path = f"{prefix}{key}"
        sub = decl.get("fields")
        value = values.get(key)
        if isinstance(sub, dict):
            out.update(_flatten_values(sub, value if isinstance(value, dict) else {}, prefix=f"{path}."))
        else:
            out[path] = value
    return out


def _walk_schema_path(schema: dict[str, Any], dotted_key: str) -> dict[str, Any]:
    """Walk ``a.b.c`` through declared sub-tables to the leaf declaration."""
    segs = dotted_key.split(".")
    cursor = schema
    for seg in segs[:-1]:
        decl = cursor.get(seg)
        sub = decl.get("fields") if isinstance(decl, dict) else None
        if not isinstance(sub, dict):
            raise KeyError(f"Field '{seg}' in '{dotted_key}' is not a nested table")
        cursor = sub
    leaf = segs[-1]
    if leaf not in cursor:
        raise KeyError(f"Unknown field '{leaf}' in '{dotted_key}'")
    return cursor[leaf]


def _coerce_value(value: Any, decl: dict[str, Any]) -> Any:
    """Pre-admission coercion for CLI string inputs.

    - ``"true"/"false"/"1"/"0"`` -> bool
    - ``"a,b,c"`` -> ``["a","b","c"]`` (when the field is an array)
    - ``'["a","b"]'`` / ``'{"k":"v"}'`` -> parsed JSON forms
    - everything else -> leave as-is and let the admission door report
    """
    if not isinstance(value, str):
        return value
    want = decl.get("type")

    if want == "boolean":
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
        return value

    if want == "integer":
        try:
            return int(value)
        except ValueError:
            return value

    if want == "number":
        try:
            return float(value)
        except ValueError:
            return value

    if want == "array":
        v = value.strip()
        if v.startswith("[") and v.endswith("]"):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in value.split(",") if item.strip()]

    if want == "object":
        v = value.strip()
        if v.startswith("{") and v.endswith("}"):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                pass
        return value

    return value


def _set_nested(dotted_key: str, value: Any, target: dict[str, Any]) -> Any:
    """Set ``target[a][b][...][leaf] = value``, returning the previous value (or None)."""
    segs = dotted_key.split(".")
    cursor = target
    for seg in segs[:-1]:
        nxt = cursor.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[seg] = nxt
        cursor = nxt
    prev = cursor.get(segs[-1])
    cursor[segs[-1]] = value
    return prev


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def channel_names() -> list[str]:
    """Public: every adapter the spec registry discovers (sorted)."""
    return sorted(_specs())


def channel_field_specs(name: str) -> dict[str, dict[str, Any]]:
    """Reflect a channel's schema into a flat ``dotted-path -> spec`` map.

    Each entry has keys: ``type``, ``default``, ``is_secret``, ``required``, ``description``.
    Used by CLI parsers, the ``channels help`` command, and ``get_channel_config``
    to know which fields exist and which to redact.
    """
    return _flatten_schema(_channel_schema(name))


def enable_channel(
    name: str,
    fields: dict[str, Any] | None = None,
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Set ``channels.<name>.enabled = True`` and optionally patch credential fields.

    Atomic: all fields are validated before anything is written. Returns the
    map of previous values for the patched fields (for caller logging).

    Raises:
        KeyError: unknown channel name or unknown field path.
        PluginConfigError: a field value violates the channel's declared schema.
    """
    payload = dict(fields or {})
    payload["enabled"] = True
    return _patch_channel(name, payload, config_path)


def disable_channel(
    name: str,
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Set ``channels.<name>.enabled = False``. Credential fields are preserved."""
    return _patch_channel(name, {"enabled": False}, config_path)


def set_channel_fields(
    name: str,
    fields: dict[str, Any],
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Patch specific fields on a channel.

    Returns ``{field_path: previous_value}`` for caller logging.

    Atomic: same validation contract as :func:`enable_channel`.
    """
    if not fields:
        return {}
    return _patch_channel(name, dict(fields), config_path)


def get_channel_config(
    name: str,
    *,
    redact_secrets: bool = True,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Return current channel configuration as a flat ``dotted-path -> value`` dict.

    Secret fields are redacted by default:

    - non-empty value renders as ``'****set****'``
    - empty / None renders as ``'(empty)'``
    """
    from raven.config.admission import normalize_slice_keys

    schema = _channel_schema(name)
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    raw_section = (data.get("channels") or {}).get(name) or {}
    values = _materialize(schema, normalize_slice_keys(schema, raw_section))

    specs = _flatten_schema(schema)
    flat = _flatten_values(schema, values)
    out: dict[str, Any] = {}
    for path_key, spec in specs.items():
        val = flat.get(path_key)
        if redact_secrets and spec["is_secret"]:
            if val in (None, "", [], {}):
                out[path_key] = "(empty)"
            else:
                out[path_key] = "****set****"
        else:
            out[path_key] = val
    return out


def reset_channel(
    name: str,
    *,
    config_path: Path | None = None,
) -> None:
    """Reset ``channels.<name>`` to declared defaults.

    The section's key is preserved so that downstream discovery still sees
    the channel; only field values revert.
    """
    schema = _channel_schema(name)
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, None]:
        data = read_raw_or_raise(path)
        data.setdefault("channels", {})
        defaults = _materialize(schema, {})
        data["channels"][name] = _camelize_keys(schema, defaults)
        return json.dumps(data, indent=2, ensure_ascii=False), None

    atomic_update(path, _apply)
    logger.info("update_channels: {} reset to defaults", name)


# ---------------------------------------------------------------------------
# Internal: shared write path
# ---------------------------------------------------------------------------


def _patch_channel(
    name: str,
    fields: dict[str, Any],
    config_path: Path | None,
) -> dict[str, Any]:
    """Validate-then-write core. Used by enable / disable / set."""
    schema = _channel_schema(name)
    specs = _flatten_schema(schema)

    unknown = [k for k in fields if k not in specs]
    if unknown:
        raise KeyError(f"Unknown field(s) {unknown} for channel '{name}'. Available fields: {sorted(specs.keys())}")

    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, dict[str, Any]]:
        from raven.config.admission import admit_slice, normalize_slice_keys

        data = read_raw_or_raise(path)
        raw_section = (data.get("channels") or {}).get(name) or {}
        working = _materialize(schema, normalize_slice_keys(schema, raw_section))

        prev: dict[str, Any] = {}
        for path_key, raw_val in fields.items():
            decl = _walk_schema_path(schema, path_key)
            coerced = _coerce_value(raw_val, decl)
            prev[path_key] = _set_nested(path_key, coerced, working)

        # The same door the gateway dispenses through: the writer and the
        # reader cannot disagree about what boards.
        validated = admit_slice(schema, working, plugin_id=f"channel:{name}")

        data.setdefault("channels", {})
        data["channels"][name] = _camelize_keys(schema, validated)
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    return atomic_update(path, _apply)


__all__ = [
    "channel_names",
    "channel_field_specs",
    "enable_channel",
    "disable_channel",
    "set_channel_fields",
    "get_channel_config",
    "reset_channel",
]
