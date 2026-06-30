"""Atomic operations for EverOS memory settings (``~/.everos/raven/config.toml``).

This module is the ONLY write path for the EverOS memory-model sections
(llm / embedding / rerank / multimodal). The onboard wizard's memory step
writes here; EverOS reads it back through its own pydantic-settings loader
(user-level toml, ``EVEROS_*`` env). It lives apart from raven's
``config.json`` because EverOS owns this channel — see plan rule.

Only the four model sections are writable; other sections EverOS ships
(memory / sqlite / lancedb / api) are preserved untouched on every write.

EverOS home: raven scopes EverOS under ``~/.everos/raven`` (not the bare
``~/.everos`` EverOS defaults to) so raven's instance keeps its config + data
in one place, isolated from any other EverOS consumer. :func:`configure_everos_env`
points EverOS there via ``EVEROS_CONFIG_FILE`` + ``EVEROS_MEMORY__ROOT``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

# raven's EverOS home. Both the user-level config toml and the data root
# (sqlite / lancedb / .index / ome.db) live under here. EverOS itself defaults
# to a bare ``~/.everos``; ``configure_everos_env`` redirects it.
_EVEROS_BASE = Path("~/.everos/raven")
_EVEROS_CONFIG = _EVEROS_BASE / "config.toml"

WRITABLE_SECTIONS = ("llm", "embedding", "rerank", "multimodal")


def get_everos_config_path() -> Path:
    """Path of the user-level EverOS config toml (``~`` expanded)."""
    return _EVEROS_CONFIG.expanduser()


def configure_everos_env() -> None:
    """Point embedded EverOS at raven's ``~/.everos/raven`` home.

    Sets two env vars EverOS reads at settings-load time:
      - ``EVEROS_CONFIG_FILE`` → the user-level toml raven manages;
      - ``EVEROS_MEMORY__ROOT`` → the data root (overrides ``[memory].root``;
        EverOS derives sqlite / lancedb / .index / ome.db paths from it).

    Uses ``setdefault`` so an explicit operator override (a pre-set ``EVEROS_*``
    env) still wins. Must run BEFORE EverOS's ``load_settings()`` — which is
    ``@cache``-d — first executes; the everos backend factory / tool factory
    call this as the earliest raven-controlled EverOS entry points.
    """
    base = _EVEROS_BASE.expanduser()
    os.environ.setdefault("EVEROS_CONFIG_FILE", str(base / "config.toml"))
    os.environ.setdefault("EVEROS_MEMORY__ROOT", str(base))


def load_everos_config() -> dict[str, Any]:
    """Return the parsed user-level toml, or ``{}`` when absent."""
    path = get_everos_config_path()
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` as TOML via temp-file + rename.

    A bare ``open(...); dump`` would truncate-then-write, so a Ctrl+C
    (KeyboardInterrupt) mid-write could leave a half-written / empty toml that
    EverOS then fails to parse. Writing to a sibling temp file and
    ``os.replace`` makes the swap atomic — readers see either the old file or
    the complete new one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        tomli_w.dump(data, f)
    os.replace(tmp, path)


def set_everos_section(section: str, fields: dict[str, Any]) -> None:
    """Merge ``fields`` into ``[section]`` of the user-level toml.

    ``None`` values are dropped (treated as "leave unset"); existing keys in
    the section and every other section are preserved.
    """
    if section not in WRITABLE_SECTIONS:
        raise KeyError(
            f"unknown everos section {section!r}; writable: {WRITABLE_SECTIONS}"
        )
    data = load_everos_config()
    clean = {k: v for k, v in fields.items() if v is not None}
    data[section] = {**data.get(section, {}), **clean}
    _write_atomic(get_everos_config_path(), data)


def clear_everos_section(section: str) -> None:
    """Drop ``[section]`` from the user-level toml (no-op if absent)."""
    if section not in WRITABLE_SECTIONS:
        raise KeyError(
            f"unknown everos section {section!r}; writable: {WRITABLE_SECTIONS}"
        )
    data = load_everos_config()
    if section not in data:
        return
    del data[section]
    _write_atomic(get_everos_config_path(), data)
