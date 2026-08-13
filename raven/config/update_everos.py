"""Atomic operations for EverOS memory settings (``<root>/everos.toml``).

This module is the ONLY write path for the EverOS memory-model sections
(llm / embedding / rerank / multimodal) and for the ``[api]`` address. The
onboard wizard's memory step writes here; EverOS reads it back through its own
pydantic-settings loader (user-level toml, ``EVEROS_*`` env). It lives apart
from raven's ``config.json`` because EverOS owns this channel — see plan rule.

Only those sections are writable; the rest EverOS ships (memory / sqlite /
lancedb) are preserved untouched on every write.

**Which root.** EverOS resolves its root from ``EVEROS_ROOT`` (default: a bare
``~/.everos``). raven does not read that variable as an input — it *writes* it
from the root recorded in ``plugins.config["everos-memory"]["root"]``, so the
choice of root is an explicit, recorded decision rather than something inherited
from an ambient environment. A root picked up from the environment and never
written down was silent data loss waiting to happen: run raven without the
variable and the memories are still on disk while raven reports none.

**Which owner.** ``plugins.config["everos-memory"]["owned"]`` says whether raven
may write to that root at all. A root raven created is raven's to configure and
serve; a root the user manages is read-only — raven records its address and
nothing else.

Boot sequence (called by ``make_backend`` / ``make_understand_media_tool``):

1. :func:`configure_everos_env` — ``EVEROS_ROOT`` → the recorded root
2. :func:`ensure_everos_home` — create ``everos.toml`` + ``ome.toml`` from
   shipped templates (skip if exists) + migrate legacy ``config.toml``.
   Owned roots only.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

logger = logging.getLogger(__name__)

# Where raven's EverOS home used to live: inside the root EverOS itself
# defaults to. That squatted on a scope slot of the user's own root --
# ``~/.everos/raven`` is exactly the directory EverOS gives an app_id of
# "raven" -- so new installs get ``<raven data dir>/everos`` instead. Existing
# installs keep this one; discovery finds it and nothing is moved.
_LEGACY_EVEROS_BASE = Path("~/.everos/raven")

WRITABLE_SECTIONS = ("llm", "embedding", "rerank", "multimodal", "api")

# Sections holding a model + credentials, as opposed to the address.
MODEL_SECTIONS = ("llm", "embedding", "rerank", "multimodal")


def default_everos_root() -> Path:
    """Where a fresh install puts raven's own EverOS home.

    Under raven's data directory, so it follows ``--config`` and reads as
    raven's property rather than a squatter in EverOS's default root.
    """
    from raven.config.paths import get_data_dir

    return get_data_dir() / "everos"


def legacy_everos_root() -> Path:
    """raven's pre-move EverOS home, still in use by existing installs."""
    return _LEGACY_EVEROS_BASE.expanduser()


def raven_owned_roots() -> tuple[Path, ...]:
    """Roots raven creates and therefore owns, newest layout first."""
    return (default_everos_root(), legacy_everos_root())


def root_is_raven_owned(root: Path | str) -> bool:
    """True when ``root`` is one raven creates for itself.

    Used only to infer ``owned`` for a config written before the field existed.
    Once recorded, the field is the answer -- a root the user points raven at
    cannot be classified by its path.
    """
    resolved = Path(root).expanduser()
    return any(resolved == owned for owned in raven_owned_roots())


def _recorded_slice() -> dict[str, Any]:
    """raven's ``plugins.config["everos-memory"]``, read as raw JSON.

    Raw rather than through the validated config so that ``raven doctor`` and
    the runtime can ask "which root" without paying for schema validation, and
    so an unrelated validation error elsewhere cannot make the memory path
    unreadable. An absent or unparseable file reads as "nothing recorded".
    """
    from raven.config.loader import get_config_path

    try:
        with get_config_path().open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    plugins = data.get("plugins") or {}
    slice_ = (plugins.get("config") or {}).get("everos-memory") if isinstance(plugins, dict) else None
    return slice_ if isinstance(slice_, dict) else {}


def everos_root() -> Path:
    """The active EverOS root.

    The recorded value when there is one; otherwise the legacy location if it
    holds a config (so an install from before the move keeps its memories), else
    the current default. ``_migrate_config`` records the outcome on the next
    write, so this fallback only runs until then.
    """
    recorded = _recorded_slice().get("root")
    if recorded:
        return Path(str(recorded)).expanduser()
    legacy = legacy_everos_root()
    if (legacy / "everos.toml").is_file():
        return legacy
    return default_everos_root()


def everos_owned() -> bool:
    """Whether raven may write to and start the active root.

    ``False`` means read-only reuse: record the address, never touch the config,
    never start or stop the process. Absent from an older config, the answer is
    inferred from the path, and anything raven did not create is treated as not
    ours -- the conservative direction.
    """
    slice_ = _recorded_slice()
    if "owned" in slice_:
        return bool(slice_["owned"])
    return root_is_raven_owned(everos_root())


def get_everos_config_path() -> Path:
    """Path of the user-level EverOS config toml."""
    return everos_root() / "everos.toml"


def configure_everos_env(root: Path | str | None = None) -> None:
    """Point EverOS at ``root`` (default: the recorded root).

    Sets ``EVEROS_ROOT`` so EverOS resolves both its config file
    (``<root>/everos.toml``) and its data directories (sqlite / lancedb /
    .index / ome.toml) under it.

    Assigns rather than ``setdefault``: an ambient ``EVEROS_ROOT`` is not an
    input to raven's choice of root. Following it silently pointed raven at a
    root nothing had recorded, so the next run without the variable reported no
    memories while they sat on disk. Operators who want a different root record
    it in the config instead.

    Must run BEFORE EverOS's ``load_settings()`` -- which is ``@cache``-d --
    first executes, or in-process EverOS imports keep the earlier root.
    """
    resolved = Path(root).expanduser() if root is not None else everos_root()
    os.environ["EVEROS_ROOT"] = str(resolved)


def ensure_everos_home(root: Path | str | None = None) -> None:
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

    Callers must gate this on :func:`everos_owned`: dropping template files into
    a root the user manages is an unrequested write, and "the files are usually
    there already" is not a basis for a read-only promise.
    """
    base = Path(root).expanduser() if root is not None else everos_root()
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


def everos_section(section: str) -> dict[str, Any]:
    """Current values of an EverOS section, or ``{}``."""
    return load_everos_config().get(section, {}) or {}


def everos_role_configured(section: str) -> bool:
    """True iff the user really configured this EverOS role.

    Sole criterion for "configured", shared by every caller: model AND api_key.
    The shipped everos.toml template seeds each section's model name with an
    empty api_key, so a model alone also holds on a fresh install -- two callers
    disagreeing on this made the wizard's Back loop on itself forever.

    Lives beside the writers rather than in the wizard so a reader does not have
    to import it: the wizard module costs ~290ms to load, which `raven doctor`
    (a millisecond command) would otherwise pay just to answer this.
    """
    sec = everos_section(section)
    return bool(sec.get("model") and sec.get("api_key"))


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


def everos_declared_address() -> str | None:
    """The address ``<root>/everos.toml`` declares, or ``None`` when unset.

    This is the authority on where a server for that root listens: EverOS reads
    ``[api]`` at startup, and raven no longer overrides it on the command line.
    Everything else -- raven's ``base_url``, a doctor probe -- is a copy of it.
    """
    api = everos_section("api")
    host = api.get("host")
    port = api.get("port")
    if not host or not port:
        return None
    return f"http://{host}:{port}"


def set_everos_api(*, host: str, port: int) -> None:
    """Record the address a server for this root must listen on.

    raven used to pass ``--port`` on the command line, which overrode the toml
    and left the file describing an address nobody was using. Writing it instead
    makes the root self-describing: anything that can read the directory knows
    where its server lives, with no second place to drift out of sync.
    """
    set_everos_section("api", {"host": host, "port": int(port)})
