"""``config.get`` / ``config.set`` RPC handlers (specs §3.6).

Contract source: ``docs/openspec/changes/tui-ipc-bridge/specs/tui-ipc.md §3.6``.

The v0.1 surface exposes only **four hot-changeable** keys; any other write
target raises :class:`ConfigFieldReadonlyError` (-32010). Values are stored
in ``~/.raven/config.json`` using dotted-path nesting (``tui.theme`` →
``{"tui": {"theme": "..."}}``) so that the same file is loadable by the legacy
``raven.config.raven_loader`` without any schema gymnastics.

Validation
----------

Per-key validators reject:

* ``agent.thinking_budget``: must be a non-negative integer.
* ``agent.temperature``: must be a number (int/float) in the closed range
  ``[0.0, 2.0]``.
* ``tui.theme``: must be a non-empty string matching ``[A-Za-z0-9_-]+``.
* ``tui.show_token_usage``: must be a boolean.

Anything else → :class:`ConfigValidationError` (-32011).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from raven.tui_rpc.errors import (
    ConfigFieldReadonlyError,
    ConfigValidationError,
)

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher


_CONFIG_DIR_NAME = ".raven"
_CONFIG_FILENAME = "config.json"

# Default values returned by config.get when the on-disk config omits the key.
_DEFAULTS: dict[str, Any] = {
    "agent.thinking_budget": 0,
    "agent.temperature": 1.0,
    "tui.theme": "default",
    "tui.show_token_usage": True,
}


# ---------------------------------------------------------------------------
# Per-key validators
# ---------------------------------------------------------------------------


_THEME_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_thinking_budget(value: Any) -> int:
    # Booleans are a subclass of int — reject them explicitly so True doesn't
    # silently coerce to 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigValidationError(
            "agent.thinking_budget must be a non-negative integer",
            data={"field": "agent.thinking_budget", "got": repr(value)},
        )
    if value < 0:
        raise ConfigValidationError(
            "agent.thinking_budget must be non-negative",
            data={"field": "agent.thinking_budget", "value": value},
        )
    return value


def _validate_temperature(value: Any) -> float:
    if isinstance(value, bool):  # bool is a subclass of int — reject upfront
        raise ConfigValidationError(
            "agent.temperature must be a number in [0, 2]",
            data={"field": "agent.temperature", "got": repr(value)},
        )
    if not isinstance(value, (int, float)):
        raise ConfigValidationError(
            "agent.temperature must be a number in [0, 2]",
            data={"field": "agent.temperature", "got": repr(value)},
        )
    if not (0.0 <= float(value) <= 2.0):
        raise ConfigValidationError(
            "agent.temperature out of range [0, 2]",
            data={"field": "agent.temperature", "value": value},
        )
    return float(value)


def _validate_theme(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigValidationError(
            "tui.theme must be a non-empty string",
            data={"field": "tui.theme", "got": repr(value)},
        )
    if not _THEME_NAME_RE.match(value):
        raise ConfigValidationError(
            "tui.theme must match [A-Za-z0-9_-]+",
            data={"field": "tui.theme", "value": value},
        )
    return value


def _validate_show_token_usage(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigValidationError(
            "tui.show_token_usage must be a boolean",
            data={"field": "tui.show_token_usage", "got": repr(value)},
        )
    return value


_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "agent.thinking_budget": _validate_thinking_budget,
    "agent.temperature": _validate_temperature,
    "tui.theme": _validate_theme,
    "tui.show_token_usage": _validate_show_token_usage,
}

# Public: the canonical writable-key set; consumers can iterate to enumerate
# defaults without mutating ``_DEFAULTS`` directly.
CONFIG_WRITABLE_KEYS: tuple[str, ...] = tuple(_VALIDATORS.keys())


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _config_path() -> Path:
    return Path.home() / _CONFIG_DIR_NAME / _CONFIG_FILENAME


def _load_config() -> dict[str, Any]:
    """Load ``config.json`` or return an empty dict on any failure.

    Symmetric with setup.status's v0.1 fallback policy — we never want a stray
    write or unreadable file to crash a get/set call. The on-disk file is the
    source of truth; downstream loaders read the same file independently.
    """
    path = _config_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _save_config(payload: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _get_nested(payload: dict[str, Any], dotted_key: str) -> Any | None:
    """Return the value at the dotted path, or None if absent."""
    parts = dotted_key.split(".")
    cur: Any = payload
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_nested(payload: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    cur: dict[str, Any] = payload
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def config_get(params: dict) -> dict:
    """Return values for whitelisted keys.

    Spec §3.6: unknown keys are silently omitted (NOT an error).
    """
    requested_raw = params.get("keys") if isinstance(params, dict) else None
    if requested_raw is None:
        requested: list[str] = list(CONFIG_WRITABLE_KEYS)
    else:
        if not isinstance(requested_raw, list) or not all(
            isinstance(k, str) for k in requested_raw
        ):
            raise ConfigValidationError(
                "config.get params.keys must be a list[str] if provided",
                data={"field": "keys", "got": repr(requested_raw)},
            )
        requested = requested_raw

    payload = _load_config()
    out: dict[str, Any] = {}
    for key in requested:
        if key not in _VALIDATORS:
            # Unknown / non-whitelisted key — silently omit per spec.
            continue
        value = _get_nested(payload, key)
        out[key] = value if value is not None else _DEFAULTS[key]
    return {"config": out}


async def config_set(params: dict) -> dict:
    """Write a single whitelisted key. Returns ``{applied, previous}``.

    Raises:
        ConfigValidationError (-32011): params shape or value invalid.
        ConfigFieldReadonlyError (-32010): key not in writable whitelist.
    """
    if not isinstance(params, dict):
        raise ConfigValidationError(
            "config.set params must be an object",
            data={"got": type(params).__name__},
        )

    key = params.get("key")
    if not isinstance(key, str) or not key:
        raise ConfigValidationError(
            "config.set params.key is required and must be a non-empty string",
            data={"field": "key", "got": repr(key)},
        )
    if "value" not in params:
        raise ConfigValidationError(
            "config.set params.value is required",
            data={"field": "value"},
        )
    raw_value = params["value"]

    if key not in _VALIDATORS:
        raise ConfigFieldReadonlyError(
            f"key '{key}' is not in the v0.1 hot-changeable whitelist",
            data={"field": key, "writable": list(CONFIG_WRITABLE_KEYS)},
        )

    validated = _VALIDATORS[key](raw_value)

    payload = _load_config()
    previous = _get_nested(payload, key)
    _set_nested(payload, key, validated)
    _save_config(payload)

    return {"applied": True, "previous": previous}


def register_config_methods(dispatcher: "Dispatcher") -> None:
    """Register ``config.get`` / ``config.set`` on a dispatcher instance."""
    dispatcher.register("config.get", config_get)
    dispatcher.register("config.set", config_set)


__all__ = [
    "config_get",
    "config_set",
    "register_config_methods",
    "CONFIG_WRITABLE_KEYS",
]
