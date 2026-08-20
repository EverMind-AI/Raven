"""Atomic operations for EverOS memory settings (``<data dir>/everos/everos.toml``).

This module is the ONLY write path for the EverOS memory-model sections
(llm / embedding / rerank / multimodal). The onboard wizard's memory step
writes here; EverOS reads it back through its own pydantic-settings loader
(user-level toml, ``EVEROS_*`` env). It lives apart from raven's
``config.json`` because EverOS owns this channel — see plan rule.

Only the four model sections are writable; other sections EverOS ships
(memory / sqlite / lancedb / api) are preserved untouched on every write.

EverOS home: raven keeps EverOS's config and data under its own instance
data dir, next to sessions and logs, so one raven instance owns one memory
store and a second instance configured elsewhere does not share it.

The home is deliberately *not* bucketed per workspace. ``EVEROS_ROOT``
decides which store the server process serves, so a per-workspace root
would mean a server process per workspace. Workspace isolation is the
``project_id`` half of the storage scope instead
(:mod:`raven.plugin.memory.everos.scope`), which is what EverOS partitions
on and what a search is confined to.

Boot sequence (called by ``make_backend`` / the understand_media tool):

1. :func:`configure_everos_env` — ``EVEROS_ROOT`` → ``<data dir>/everos``
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

# Where raven's EverOS home lived before it moved under the instance data
# dir. Still read to point a migration message at it.
_LEGACY_EVEROS_BASE = Path("~/.everos/raven")

WRITABLE_SECTIONS = ("llm", "embedding", "rerank", "multimodal")


def get_everos_home() -> Path:
    """Directory holding EverOS's config toml and its whole data root
    (sqlite / lancedb / .index / ome.db).

    Resolves the path without creating it. Asking where memory would live
    must not be what brings it into existence: an evaluation run with
    memory switched off has to leave no directory behind.
    :func:`ensure_everos_home` is the one that creates it.
    """
    from raven.config.paths import get_data_dir

    return get_data_dir() / "everos"


def get_everos_config_path() -> Path:
    """Path of the user-level EverOS config toml."""
    return get_everos_home() / "everos.toml"


def configure_everos_env() -> None:
    """Point embedded EverOS at raven's own home under the data dir.

    Sets ``EVEROS_ROOT`` so EverOS resolves both its config file
    (``<root>/everos.toml``) and data directories (sqlite / lancedb /
    .index / ome.toml) there.

    Ordering is load-bearing in both directions: this must run AFTER
    ``loader.set_config_path()`` (the data dir is derived from the config
    file's location, so running earlier resolves the default home), and
    BEFORE EverOS's ``load_settings()``, which is ``@cache``-d and freezes
    the root on its first call.

    Uses ``setdefault`` so an explicit operator override still wins; a
    conflicting pre-set value is reported rather than silently obeyed,
    because it decides which store a run reads and writes.
    """
    base = get_everos_home()
    existing = os.environ.get("EVEROS_ROOT")
    if existing is not None and Path(existing) != base:
        logger.warning(
            "EVEROS_ROOT is already set to %s; raven would have used %s. Memory will read and write the pre-set root.",
            existing,
            base,
        )
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

    A store left at the pre-move location is reported, not read: copying a
    live sqlite + lancedb pair underneath a running engine is how indexes
    get corrupted, so moving it is the operator's call.
    """
    base = get_everos_home()
    base.mkdir(parents=True, exist_ok=True)

    everos_toml = base / "everos.toml"
    ome_toml = base / "ome.toml"

    legacy = _LEGACY_EVEROS_BASE.expanduser()
    if not everos_toml.exists() and (legacy / "everos.toml").is_file():
        logger.info(
            "an EverOS home from before the move still exists at %s; "
            "move its contents to %s to keep that memory (model keys included)",
            legacy,
            base,
        )

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


def everos_models_configured() -> bool:
    """Whether the local EverOS runtime has the credentials it needs to boot.

    A keyless ``everos server start`` refuses to come up -- both provider
    clients are built during startup, so a missing LLM key raises
    ``LLMNotConfiguredError`` and a missing embedding key raises before the
    app binds (measured on the pinned 1.1.3). "Memory on by default" is
    therefore only honest when this returns True for both. Per section,
    the env var wins over the user toml, mirroring EverOS's own settings
    precedence. Rerank / multimodal keys are NOT gated on: the reranker is
    resolved lazily and degrades to None.
    """

    def _key(section: str) -> str:
        env = os.environ.get(f"EVEROS_{section.upper()}__API_KEY")
        if env and env.strip():
            return env
        value = (load_everos_config().get(section) or {}).get("api_key", "")
        return value.strip() if isinstance(value, str) else ""

    return bool(_key("llm")) and bool(_key("embedding"))


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
