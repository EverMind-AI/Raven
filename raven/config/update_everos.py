"""Atomic operations for EverOS memory settings (``~/.everos/raven/everos.toml``).

This module is the ONLY write path for the EverOS memory-model sections
(llm / embedding / rerank / multimodal). The onboard wizard's memory step
writes here; EverOS reads it back through its own pydantic-settings loader
(user-level toml, ``EVEROS_*`` env). It lives apart from raven's
``config.json`` because EverOS owns this channel — see plan rule.

Only the four model sections are writable; other sections EverOS ships
(memory / sqlite / lancedb / api) are preserved untouched on every write.

EverOS home: raven scopes EverOS under ``~/.everos/raven`` (not the bare
``~/.everos`` EverOS defaults to) so raven's instance keeps its config + data
in one place, isolated from any other EverOS consumer.

Boot sequence (called by ``make_backend`` / ``make_understand_media_tool``):

1. :func:`configure_everos_env` — ``EVEROS_ROOT`` → ``~/.everos/raven``
2. :func:`ensure_everos_home` — create ``everos.toml`` + ``ome.toml`` from
   shipped templates (skip if exists) + migrate legacy ``config.toml``
"""

from __future__ import annotations

import logging
import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

logger = logging.getLogger(__name__)

# raven's EverOS home. Both the user-level config toml and the data root
# (sqlite / lancedb / .index / ome.db) live under here. EverOS itself defaults
# to a bare ``~/.everos``; ``configure_everos_env`` redirects it.
_EVEROS_BASE = Path("~/.everos/raven")
_EVEROS_CONFIG = _EVEROS_BASE / "everos.toml"

WRITABLE_SECTIONS = ("llm", "embedding", "rerank", "multimodal")


def get_everos_config_path() -> Path:
    """Path of the user-level EverOS config toml (``~`` expanded)."""
    return _EVEROS_CONFIG.expanduser()


def configure_everos_env() -> None:
    """Point embedded EverOS at raven's ``~/.everos/raven`` home.

    Sets ``EVEROS_ROOT`` so EverOS resolves both its config file
    (``<root>/everos.toml``) and data directories (sqlite / lancedb /
    .index / ome.toml) under raven's scoped home.

    Uses ``setdefault`` so an explicit operator override (a pre-set
    ``EVEROS_ROOT``) still wins. Must run BEFORE EverOS's
    ``load_settings()`` — which is ``@cache``-d — first executes.
    """
    base = _EVEROS_BASE.expanduser()
    os.environ.setdefault("EVEROS_ROOT", str(base))


def ensure_everos_home() -> None:
    """Ensure the EverOS home directory has the required config files.

    Three steps, all idempotent:

    1. **Migrate** legacy ``config.toml`` → ``everos.toml`` (everos >=1.1
       renamed the config file). Existing content is preserved.
    2. **Create** ``everos.toml`` from the shipped template if absent.
       Users who already ran ``raven onboard`` have this file; new
       installs get the template with empty API keys (onboard fills
       them later).
    3. **Create** ``ome.toml`` from the shipped template if absent.
       Without this file the OME engine's ``ConfigReloader`` raises
       ``FileNotFoundError`` and the memory backend silently degrades.
    """
    base = _EVEROS_BASE.expanduser()
    base.mkdir(parents=True, exist_ok=True)

    everos_toml = base / "everos.toml"
    ome_toml = base / "ome.toml"

    # Step 1: migrate legacy config.toml → everos.toml (preserves content).
    old_cfg = base / "config.toml"
    if old_cfg.is_file() and not everos_toml.exists():
        old_cfg.rename(everos_toml)
        logger.info("migrated %s → %s", old_cfg, everos_toml)

    # Steps 2-3: copy shipped templates for any missing config file.
    try:
        # Deferred: everos may not be installed.
        from everos.entrypoints.cli.commands.init_cmd import (
            _EVEROS_TEMPLATE,
            _OME_TEMPLATE,
        )
    except ImportError:
        return

    for target, template in [
        (everos_toml, _EVEROS_TEMPLATE),
        (ome_toml, _OME_TEMPLATE),
    ]:
        if target.exists():
            continue
        shutil.copy2(template, target)
        logger.info("created %s from template", target)


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
        raise KeyError(f"unknown everos section {section!r}; writable: {WRITABLE_SECTIONS}")
    data = load_everos_config()
    clean = {k: v for k, v in fields.items() if v is not None}
    data[section] = {**data.get(section, {}), **clean}
    _write_atomic(get_everos_config_path(), data)


def clear_everos_section(section: str) -> None:
    """Drop ``[section]`` from the user-level toml (no-op if absent)."""
    if section not in WRITABLE_SECTIONS:
        raise KeyError(f"unknown everos section {section!r}; writable: {WRITABLE_SECTIONS}")
    data = load_everos_config()
    if section not in data:
        return
    del data[section]
    _write_atomic(get_everos_config_path(), data)
