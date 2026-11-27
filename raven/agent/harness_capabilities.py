"""Load the checked-in capability switches for generated worker harnesses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Mapping, get_args

ModuleName = Literal["memory", "planning", "capability", "action"]
FunctionKind = Literal["self", "participant"]
_MODULES = frozenset(get_args(ModuleName))

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
_GENERATION_PARAMETERS = (
    ("memory", "systemPrompt"),
    ("memory", "stopWhen"),
    ("capability", "tools"),
    ("action", "checks"),
    ("action", "checksImpl"),
)
"""The parameter switches the generation surface reads.

Hand-written, so it can drift from the call sites that read them: the
check that a switch is declared is only as good as this list. A test reads
the query sites out of the source and holds the two equal.
"""
_GENERATION_FUNCTIONS = (
    ("memory", "participant", "intake"),
    ("planning", "participant", "advise"),
    ("action", "participant", "judge"),
    ("action", "participant", "salvage"),
)
"""The participant function switches the generation surface reads."""
_CACHE_KEY: tuple[Path, int, int] | None = None
_CACHE_ROOT: dict[str, Any] | None = None


def _entry_enabled(entry: Any, path: str) -> bool:
    if not isinstance(entry, dict) or type(entry.get("enabled")) is not bool:
        raise RuntimeError(f"invalid harness generation capability file: {path}.enabled must be a boolean")
    return entry["enabled"]


def _object(section: Any, kind: str, path: str) -> Mapping[str, Any]:
    """One catalogue section, which has to be an object.

    A section that is not one is a broken capability file: read on its own it
    would raise an ``AttributeError`` from inside the walk, where the loader's
    contract is the ``RuntimeError`` that names the file.
    """
    if not isinstance(section, dict):
        raise RuntimeError(f"invalid harness generation capability file: {path}.{kind} must be an object")
    return section


def _items(section: Any, kind: str, path: str) -> Mapping[str, Any]:
    """The ``items`` map of one catalogue section."""
    items = _object(section, kind, path).get("items", {})
    if not isinstance(items, dict):
        raise RuntimeError(f"invalid harness generation capability file: {path}.{kind}.items must be an object")
    return items


def _validate_connected(root: dict[str, Any]) -> None:
    """Every switch the catalogue declares is one this loader serves.

    A module body that is not an object is a broken document, not a key to skip
    past: the module names are the ones the loader reads, so a misshapen one is
    named here rather than at whichever query walks into it. A ``note`` or any
    other key belongs to whoever wrote the file, not to this loader.
    """
    for module, body in root.items():
        if module not in _MODULES:
            continue
        if not isinstance(body, dict):
            raise RuntimeError(f"invalid harness generation capability file: {module} must be an object")
        for name, entry in _items(body.get("parameters", {}), "parameters", module).items():
            path = f"{module}.parameters.{name}"
            if _entry_enabled(entry, path) and (module, name) not in _CONNECTED_PARAMETERS:
                raise RuntimeError(f"harness generation capability is enabled but not wired: {path}")
        sections = _object(body.get("functions", {}), "functions", module)
        for kind in ("self", "participant"):
            for name, entry in _items(sections.get(kind, {}), kind, f"{module}.functions").items():
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
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"invalid harness generation capability file: {exc}") from exc
    if not isinstance(root, dict):
        raise RuntimeError("invalid harness generation capability file: harnessGeneration must be an object")
    _validate_connected(root)
    _CACHE_KEY = cache_key
    _CACHE_ROOT = root
    return root


def validate() -> None:
    """Load the catalogue and run its checks, raising what the loader raises.

    For the caller that wants to know once, up front, whether generation can
    run at all: a broken file surfaces as the ``RuntimeError`` the loader
    names rather than inside whichever query a turn happens to make first.
    """
    for module, name in _GENERATION_PARAMETERS:
        parameter_enabled(module, name)
    for module, kind, name in _GENERATION_FUNCTIONS:
        function_enabled(module, kind, name)


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


__all__ = ["function_enabled", "parameter_enabled", "validate"]
