"""Load the checked-in capability switches for generated worker harnesses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

ModuleName = Literal["memory", "planning", "capability", "action"]
FunctionKind = Literal["self", "participant"]

_PATH = Path(__file__).parents[1] / "playbook" / "harness_generation.json"
_CONNECTED_PARAMETERS = frozenset(
    {
        ("memory", "systemPrompt"),
        ("memory", "stopWhen"),
        ("capability", "tools"),
        ("action", "checks"),
    }
)
_CONNECTED_FUNCTIONS = frozenset(
    {
        ("memory", "participant", "intake"),
        ("planning", "participant", "advise"),
        ("action", "participant", "judge"),
        ("action", "participant", "salvage"),
    }
)
_CACHE_KEY: tuple[Path, int, int] | None = None
_CACHE_ROOT: dict[str, Any] | None = None


def _entry_enabled(entry: Any, path: str) -> bool:
    if not isinstance(entry, dict) or type(entry.get("enabled")) is not bool:
        raise RuntimeError(f"invalid harness generation capability file: {path}.enabled must be a boolean")
    return entry["enabled"]


def _validate_connected(root: dict[str, Any]) -> None:
    for module, body in root.items():
        if module == "note" or not isinstance(body, dict):
            continue
        parameters = body.get("parameters", {}).get("items", {})
        for name, entry in parameters.items():
            path = f"{module}.parameters.{name}"
            if _entry_enabled(entry, path) and (module, name) not in _CONNECTED_PARAMETERS:
                raise RuntimeError(f"harness generation capability is enabled but not wired: {path}")
        functions = body.get("functions", {})
        for kind in ("self", "participant"):
            for name, entry in functions.get(kind, {}).get("items", {}).items():
                path = f"{module}.functions.{kind}.{name}"
                if _entry_enabled(entry, path) and (module, kind, name) not in _CONNECTED_FUNCTIONS:
                    raise RuntimeError(f"harness generation capability is enabled but not wired: {path}")


def _root() -> dict[str, Any]:
    global _CACHE_KEY, _CACHE_ROOT
    try:
        stat = _PATH.stat()
        cache_key = (_PATH, stat.st_mtime_ns, stat.st_size)
        if _CACHE_KEY == cache_key and _CACHE_ROOT is not None:
            return _CACHE_ROOT
        document = json.loads(_PATH.read_text(encoding="utf-8"))
        root = document["harnessGeneration"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"invalid harness generation capability file: {exc}") from exc
    if not isinstance(root, dict):
        raise RuntimeError("invalid harness generation capability file: harnessGeneration must be an object")
    _validate_connected(root)
    _CACHE_KEY = cache_key
    _CACHE_ROOT = root
    return root


def parameter_enabled(module: ModuleName, name: str) -> bool:
    """Whether the generator may emit one module parameter."""
    root = _root()
    try:
        entry = root[module]["parameters"]["items"][name]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"harness generation capability is not declared: {module}.parameters.{name}") from exc
    return _entry_enabled(entry, f"{module}.parameters.{name}")


def function_enabled(module: ModuleName, kind: FunctionKind, name: str) -> bool:
    """Whether the generator may emit one module function."""
    root = _root()
    try:
        entry = root[module]["functions"][kind]["items"][name]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"harness generation capability is not declared: {module}.functions.{kind}.{name}") from exc
    return _entry_enabled(entry, f"{module}.functions.{kind}.{name}")


__all__ = ["function_enabled", "parameter_enabled"]
