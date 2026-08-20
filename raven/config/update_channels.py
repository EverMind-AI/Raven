"""Atomic operations for channel config sections.

This module is the ONLY write path for channel configuration. All entry
points (CLI commands, future wizard, future WebUI, future REPL slash)
must call functions defined here. Direct load_config / save_config on
the channels section is forbidden -- see plan rule.
"""

from __future__ import annotations

import json
import os
import typing
from pathlib import Path
from typing import Any, Union

from loguru import logger
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from raven.config.loader import get_config_path, read_raw_or_raise
from raven.config.schema import ChannelsConfig

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: temp-file then os.replace. Preserves indent=2, UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _unwrap_optional(annotation: Any) -> Any:
    """Strip ``Optional[X]`` / ``X | None`` down to ``X``."""
    import types as _types

    origin = typing.get_origin(annotation)
    if origin is Union or origin is getattr(_types, "UnionType", None):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_model_class(ann: Any) -> bool:
    return isinstance(ann, type) and issubclass(ann, BaseModel)


def _channel_names() -> list[str]:
    """Return channel field names defined on ChannelsConfig (BaseModel subfields only)."""
    out: list[str] = []
    for fname, finfo in ChannelsConfig.model_fields.items():
        ann = _unwrap_optional(finfo.annotation)
        if _is_model_class(ann):
            out.append(fname)
    return out


def _channel_schema_cls(name: str) -> type[BaseModel]:
    """Look up the Pydantic class for a channel ('telegram' -> TelegramConfig)."""
    field = ChannelsConfig.model_fields.get(name)
    if field is None:
        raise KeyError(f"Unknown channel '{name}'. Available channels: {sorted(_channel_names())}")
    ann = _unwrap_optional(field.annotation)
    if not _is_model_class(ann):
        raise KeyError(f"'{name}' is not a channel section. Available channels: {sorted(_channel_names())}")
    return ann


def _annotation_str(ann: Any) -> str:
    """Compact human-readable string for a type annotation.

    Literal renders as the bare keyword; concrete choices are surfaced
    via the description column (see ``_flatten_fields``).
    """
    ann = _unwrap_optional(ann)
    origin = typing.get_origin(ann)
    if origin is typing.Literal:
        return "Literal"
    if origin is list:
        args = typing.get_args(ann)
        return f"list[{_annotation_str(args[0])}]" if args else "list"
    if origin is dict:
        args = typing.get_args(ann)
        if args and len(args) == 2:
            return f"dict[{_annotation_str(args[0])}, {_annotation_str(args[1])}]"
        return "dict"
    if hasattr(ann, "__name__"):
        return ann.__name__
    return str(ann)


_SECRET_EXACT = {"token", "secret", "password", "api_key"}
_SECRET_SUFFIXES = (
    "_token",
    "_secret",
    "_key",
    "_password",
)


def _is_secret_field(field_name: str, field_info: Any) -> bool:
    """Detect secret fields, in order:

    1. Explicit: ``field_info.json_schema_extra.get('secret') is True``
    2. Exact name match (``token``, ``secret``, ``password``, ``api_key``)
    3. Suffix match (``_token``, ``_secret``, ``_key``, ``_password``)
    """
    extra = getattr(field_info, "json_schema_extra", None)
    if isinstance(extra, dict) and extra.get("secret") is True:
        return True
    if field_name in _SECRET_EXACT:
        return True
    return any(field_name.endswith(suf) for suf in _SECRET_SUFFIXES)


def _is_required_field(field_info: Any) -> bool:
    """A field is required when explicitly marked ``json_schema_extra={'required': True}``.

    Every channel field carries a Pydantic default (so partial/disabled configs load),
    so pydantic's own required flag is always False; requiredness is an explicit UX marker.
    """
    extra = getattr(field_info, "json_schema_extra", None)
    return isinstance(extra, dict) and extra.get("required") is True


def _walk_nested_path(model_cls: type[BaseModel], dotted_key: str) -> tuple[type[BaseModel], str]:
    """Walk ``a.b.c`` into nested ``BaseModel`` classes.

    For ``'dm.policy'`` on ``SlackConfig`` returns ``(SlackDMConfig, 'policy')``.

    Raises:
        KeyError: when a segment does not exist or is not a nested model.
    """
    segs = dotted_key.split(".")
    cls: type[BaseModel] = model_cls
    for seg in segs[:-1]:
        finfo = cls.model_fields.get(seg)
        if finfo is None:
            raise KeyError(f"Unknown nested field '{seg}' in {cls.__name__}")
        ann = _unwrap_optional(finfo.annotation)
        if not _is_model_class(ann):
            raise KeyError(f"Field '{seg}' in {cls.__name__} is not a nested model")
        cls = ann
    leaf = segs[-1]
    if leaf not in cls.model_fields:
        raise KeyError(f"Unknown field '{leaf}' in {cls.__name__}")
    return cls, leaf


def _coerce_value(value: Any, annotation: Any) -> Any:
    """Pre-Pydantic coercion for CLI string inputs.

    - ``"true"/"false"/"1"/"0"`` -> bool
    - ``"a,b,c"`` -> ``["a","b","c"]`` (when annotation is ``list[...]``)
    - ``'["a","b"]'`` -> ``["a","b"]`` (JSON form for lists)
    - ``'{"k":"v"}'`` -> ``{"k":"v"}`` (JSON form for dicts)
    - everything else -> leave as-is and let Pydantic coerce / report
    """
    if not isinstance(value, str):
        return value

    base = _unwrap_optional(annotation)

    if base is bool:
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
        return value

    if base is int:
        try:
            return int(value)
        except ValueError:
            return value

    if base is float:
        try:
            return float(value)
        except ValueError:
            return value

    origin = typing.get_origin(base)
    if origin is list:
        v = value.strip()
        if v.startswith("[") and v.endswith("]"):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in value.split(",") if item.strip()]

    if origin is dict:
        v = value.strip()
        if v.startswith("{") and v.endswith("}"):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                pass
        return value

    return value


def _field_default(field_info: Any) -> Any:
    """Resolve a Pydantic FieldInfo's effective default (call factory if any)."""
    if field_info.default_factory is not None:
        try:
            return field_info.default_factory()
        except Exception:
            return None
    if field_info.default is PydanticUndefined:
        return None
    return field_info.default


def _flatten_fields(cls: type[BaseModel], prefix: str = "") -> dict[str, dict[str, Any]]:
    """Recurse into nested ``BaseModel`` fields, producing a flat dict of specs.

    For ``Literal[...]`` fields with no user-provided description, the choice
    list is rendered into ``description`` so CLI consumers can surface it.
    """
    out: dict[str, dict[str, Any]] = {}
    for fname, finfo in cls.model_fields.items():
        ann = _unwrap_optional(finfo.annotation)
        path = f"{prefix}{fname}"
        if _is_model_class(ann):
            out.update(_flatten_fields(ann, prefix=f"{path}."))
            continue
        description = finfo.description or ""
        origin = typing.get_origin(ann)
        if origin is typing.Literal and not description:
            choices = ", ".join(str(a) for a in typing.get_args(ann))
            description = f"Choices: {choices}"
        out[path] = {
            "type": _annotation_str(ann),
            "default": _field_default(finfo),
            "is_secret": _is_secret_field(fname, finfo),
            "required": _is_required_field(finfo),
            "description": description,
        }
    return out


def _flatten_instance(instance: BaseModel, prefix: str = "") -> dict[str, Any]:
    """Flatten a Pydantic instance to dotted-path -> value (skipping nested-model nodes)."""
    out: dict[str, Any] = {}
    for fname in type(instance).model_fields:
        val = getattr(instance, fname)
        path = f"{prefix}{fname}"
        if isinstance(val, BaseModel):
            out.update(_flatten_instance(val, prefix=f"{path}."))
        else:
            out[path] = val
    return out


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


def channel_field_specs(name: str) -> dict[str, dict[str, Any]]:
    """Reflect a channel schema into a flat ``dotted-path -> spec`` map.

    Each entry has keys: ``type``, ``default``, ``is_secret``, ``required``, ``description``.
    Used by CLI parsers, the ``channels help`` command, and ``get_channel_config``
    to know which fields exist and which to redact.
    """
    cls = _channel_schema_cls(name)
    return _flatten_fields(cls)


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
        ValidationError: a field value violates the channel's Pydantic schema.
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
    cls = _channel_schema_cls(name)
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    raw_section = (data.get("channels") or {}).get(name) or {}

    try:
        instance = cls.model_validate(raw_section)
    except ValidationError:
        instance = cls()

    specs = channel_field_specs(name)
    flat = _flatten_instance(instance)
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
    """Reset ``channels.<name>`` to schema defaults.

    The section's key is preserved so that downstream discovery still sees
    the channel; only field values revert. Equivalent to instantiating the
    Pydantic class fresh and writing its ``model_dump(by_alias=True)``.
    """
    cls = _channel_schema_cls(name)
    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    data.setdefault("channels", {})
    instance = cls()
    data["channels"][name] = instance.model_dump(by_alias=True)
    _write_atomic(path, data)
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
    cls = _channel_schema_cls(name)
    specs = channel_field_specs(name)

    unknown = [k for k in fields if k not in specs]
    if unknown:
        raise KeyError(f"Unknown field(s) {unknown} for channel '{name}'. Available fields: {sorted(specs.keys())}")

    path = config_path or get_config_path()
    data = read_raw_or_raise(path)
    raw_section = (data.get("channels") or {}).get(name) or {}

    try:
        current = cls.model_validate(raw_section)
    except ValidationError:
        current = cls()

    working = current.model_dump()

    prev: dict[str, Any] = {}
    for path_key, raw_val in fields.items():
        leaf_cls, leaf_field = _walk_nested_path(cls, path_key)
        leaf_info = leaf_cls.model_fields[leaf_field]
        coerced = _coerce_value(raw_val, leaf_info.annotation)
        prev[path_key] = _set_nested(path_key, coerced, working)

    validated = cls.model_validate(working)

    data.setdefault("channels", {})
    data["channels"][name] = validated.model_dump(by_alias=True)
    _write_atomic(path, data)
    return prev


__all__ = [
    "channel_field_specs",
    "enable_channel",
    "disable_channel",
    "set_channel_fields",
    "get_channel_config",
    "reset_channel",
]
