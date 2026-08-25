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
_LEGACY_EVEROS_SUFFIX = (".everos", "raven")

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
    """raven's pre-move EverOS home, still in use by existing installs.

    Built from :meth:`Path.home` rather than ``Path("~/...").expanduser()``:
    expanduser reads ``$HOME`` out of the environment directly, so it walked
    straight past the home redirection callers and tests install and reached
    the real one.
    """
    return Path.home().joinpath(*_LEGACY_EVEROS_SUFFIX)


def _is_default_installation() -> bool:
    """Whether this process is the installation that owns ``~/.raven``."""
    from raven.config.loader import get_config_path

    return get_config_path() == Path.home() / ".raven" / "config.json"


def applicable_legacy_root() -> Path | None:
    """The legacy root, when this installation is the one that could have made it.

    Every other root is derived from the config directory, so pointing raven at
    another home moves them together. This one is a single machine-wide path,
    which means an instance running from a moved config would otherwise pick up
    the default installation's root -- and then converge it, stopping a service
    and rewriting an ``[api]`` belonging to an installation it is meant to be
    isolated from.

    Selection only. :func:`root_is_raven_owned` still recognises the path
    unconditionally, because a config that records it recorded a root raven
    created, whichever installation is reading it now.
    """
    return legacy_everos_root() if _is_default_installation() else None


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


def fallback_everos_root() -> Path:
    """Which root to assume when nothing is recorded.

    The legacy location when it holds a config, so an install from before the
    move keeps its memories; otherwise the current default. Derives its answer
    without reading raven's config, which is what lets ``_migrate_config`` use it
    while holding a config dict of its own -- calling :func:`everos_root` there
    would re-read whatever path is globally current, not the file being migrated.
    """
    legacy = applicable_legacy_root()
    if legacy is not None and (legacy / "everos.toml").is_file():
        return legacy
    return default_everos_root()


def everos_root() -> Path:
    """The active EverOS root: the recorded one, else the fallback."""
    recorded = _recorded_slice().get("root")
    if recorded:
        return Path(str(recorded)).expanduser()
    return fallback_everos_root()


def owned_everos_root() -> Path:
    """The root raven may create in and write to.

    Deliberately not the same question as :func:`everos_root`. The active root
    can be one the user manages, and "raven needs a root of its own" must never
    resolve to that one: a user who declines to share theirs would otherwise have
    it adopted, seeded with templates and overwritten with raven's models.
    """
    if everos_owned():
        return everos_root()
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


class EverosRootNotOwnedError(RuntimeError):
    """A write was attempted against a root the user manages.

    Not a ``PermissionError``: that is a filesystem condition and callers catch
    it as one (``except OSError``), which would swallow exactly the signal this
    is meant to raise.
    """


def _require_owned(action: str) -> None:
    """Refuse a write unless raven owns the active root.

    The read-only promise used to live only at the call sites that happened to
    remember it -- the same shape as the drift this whole change is about, where
    one rule was enforced in several places and one of them was wrong. Enforcing
    it at the write primitives means a new caller cannot quietly opt out.
    """
    if everos_owned():
        return
    raise EverosRootNotOwnedError(f"refusing to {action}: {everos_root()} is managed by the user, not by raven")


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
    _require_owned("create config templates in")
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


def role_configured_in(data: dict[str, Any], section: str) -> bool:
    """Whether ``section`` of an already-parsed everos.toml counts as configured.

    Same criterion as :func:`everos_role_configured`, applied to a toml the caller
    read itself. Discovery needs this: it inspects candidate roots before any of
    them is the active one, and a second hand-rolled "does it have a key" check
    is exactly what made two callers disagree once before.
    """
    sec = data.get(section) or {}
    return bool(sec.get("model") and sec.get("api_key"))


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
    return role_configured_in(load_everos_config(), section)


def set_everos_section(section: str, fields: dict[str, Any]) -> None:
    """Merge ``fields`` into ``[section]`` of the user-level toml.

    ``None`` values are dropped (treated as "leave unset"); existing keys in
    the section and every other section are preserved.
    """
    if section not in WRITABLE_SECTIONS:
        raise KeyError(f"unknown everos section {section!r}; writable: {WRITABLE_SECTIONS}")
    _require_owned(f"write [{section}]")
    data = load_everos_config()
    clean = {k: v for k, v in fields.items() if v is not None}
    data[section] = {**data.get(section, {}), **clean}
    _write_atomic(get_everos_config_path(), data)


def clear_everos_section(section: str) -> None:
    """Drop ``[section]`` from the user-level toml (no-op if absent)."""
    if section not in WRITABLE_SECTIONS:
        raise KeyError(f"unknown everos section {section!r}; writable: {WRITABLE_SECTIONS}")
    _require_owned(f"clear [{section}]")
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
