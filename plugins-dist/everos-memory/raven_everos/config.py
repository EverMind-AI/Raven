"""Atomic operations for EverOS memory settings (``<root>/everos.toml``).

This module is the ONLY write path for the EverOS memory-model sections
(llm / embedding / rerank / multimodal) and for the ``[api]`` address. The
onboard wizard's memory step writes here; EverOS reads it back through its own
pydantic-settings loader (user-level toml, ``EVEROS_*`` env). It lives apart
from raven's ``config.json`` because EverOS owns this channel.

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
    from raven.home import get_config_path

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


def _raven_config_raw() -> dict[str, Any]:
    """raven's config.json, parsed and nothing more.

    Raw rather than through the validated config so that ``raven doctor`` and
    the runtime can ask small questions of it without paying for schema
    validation, and so an unrelated validation error elsewhere cannot make the
    memory path unreadable. An absent or unparseable file reads as empty.
    """
    from raven.home import get_config_path

    try:
        with get_config_path().open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def host_embedding_section() -> dict[str, str]:
    """raven's ``embedding`` block, in this module's spelling.

    Raven's config writes camelCase; everos.toml and the rest of this module
    speak snake_case, so the two names each value can have are resolved here
    once rather than at every reader.

    Empty when the block is absent or incomplete -- all three values are what
    :func:`configure_embedding_env` needs before it binds anything, so anything
    less is not an endpoint.
    """
    block = _raven_config_raw().get("embedding")
    if not isinstance(block, dict):
        return {}
    got = {
        "model": str(block.get("model") or ""),
        "base_url": str(block.get("baseUrl") or block.get("base_url") or ""),
        "api_key": str(block.get("apiKey") or block.get("api_key") or ""),
    }
    return got if all(got.values()) else {}


def _recorded_slice() -> dict[str, Any]:
    """raven's ``plugins.config["everos-memory"]``, read as raw JSON.

    Falls back to the friendlier ``everos`` key, which older configs recorded
    the slice under. An absent or unparseable file reads as "nothing recorded".
    """
    data = _raven_config_raw()
    plugins = data.get("plugins") or {}
    slice_ = (plugins.get("config") or {}).get("everos-memory") if isinstance(plugins, dict) else None
    if not isinstance(slice_, dict) and isinstance(plugins, dict):
        slice_ = (plugins.get("config") or {}).get("everos")
    return slice_ if isinstance(slice_, dict) else {}


def fallback_everos_root() -> Path:
    """Which root to assume when nothing is recorded.

    The legacy location when it holds a config, so an install from before the
    move keeps its memories; otherwise the current default. Derives its answer
    without reading raven's config so it can be asked before any config is on
    disk -- a fresh install, or a root question asked ahead of onboarding
    writing one.
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

    Enforced at the write primitives so a new caller cannot opt out; the toml is
    the address of record.
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


def configure_embedding_env(embedding: Any) -> bool:
    """Offer EverOS the host's embedding endpoint, when EverOS has none of its own.

    The host's block is a default to fall back on, not a takeover: an operator
    who wrote ``[embedding]`` into ``everos.toml`` chose that endpoint for
    memory specifically, and reusing the host's is a convenience they are
    entitled to decline. So this defers to the file and fills the gap only when
    the file leaves one -- which is also what the settings page still edits.

    Delivered through the env binding EverOS already documents
    (``EVEROS_EMBEDDING__MODEL`` and friends) rather than by writing the file:
    the file belongs to whoever manages the root, and on a self-managed root
    raven promised not to touch it. Env beats the file in EverOS's own source
    order, which is exactly why the file is checked first here.

    Returns whether anything was set, so a caller can log which lane it took.

    Must run BEFORE EverOS's cached ``load_settings()``, same as
    :func:`configure_everos_env`.
    """
    env = embedding_env(
        {
            "model": getattr(embedding, "model", ""),
            "base_url": getattr(embedding, "base_url", ""),
            "api_key": getattr(embedding, "api_key", ""),
            "dimensions": getattr(embedding, "dimensions", None),
        }
    )
    os.environ.update(env)
    return bool(env)


def embedding_env(values: Any) -> dict[str, str]:
    """``values`` rendered as EverOS's embedding variables, or ``{}``.

    Split out from :func:`configure_embedding_env` because two consumers need
    the same answer in different forms: that function binds it into this
    process, and the child environment a spawn builds fills it in for a launch
    nobody bound it for. Both go through here so the deference below is decided
    once.

    Empty when the three values are not all present -- fewer than three is not
    an endpoint -- and empty when ``everos.toml`` carries an ``[embedding]`` of
    its own: an operator who wrote one chose that endpoint for memory
    specifically, and reusing the host's is a convenience they may decline. The
    shipped template seeds a placeholder ``"<...>"`` model name, which is not a
    choice anybody made.
    """
    model = str(values.get("model") or "")
    base_url = str(values.get("base_url") or "")
    api_key = str(values.get("api_key") or "")
    if not (model and base_url and api_key):
        return {}
    if everos_has_own_embedding():
        return {}
    env = {
        "EVEROS_EMBEDDING__MODEL": model,
        "EVEROS_EMBEDDING__BASE_URL": base_url,
        "EVEROS_EMBEDDING__API_KEY": api_key,
    }
    dimensions = values.get("dimensions")
    if isinstance(dimensions, int) and dimensions > 0:
        env["EVEROS_EMBEDDING__DIMENSIONS"] = str(dimensions)
    return env


def everos_has_own_embedding() -> bool:
    """Whether ``everos.toml`` carries an ``[embedding]`` an operator chose.

    The one question that decides which of the endpoint's two homes is in
    force, asked by everything that reads or writes it -- the binding, the
    wizard, and the settings page -- so no surface can show one home while
    another writes the other.

    Model **and** key, which is :func:`role_configured_in`'s criterion and not
    a second one: the shipped template seeds every section with a real model
    name and an empty key, so "has a model" is true of a root nobody has
    configured. Reading it that way made a fresh managed install look like an
    operator's deliberate choice -- the host's endpoint was never bound, and
    the service the wizard had just started ran keyword-only while the wizard
    said embedding was configured.
    """
    return role_configured_in(load_everos_config(), "embedding")


def host_embedding_env() -> dict[str, str]:
    """The binding raven's own ``embedding`` block earns, or ``{}``."""
    return embedding_env(host_embedding_section())


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

    ``embedding`` has two homes since its endpoint became raven's: the toml
    still wins when an operator wrote one there, and raven's block fills the
    gap -- the same precedence :func:`configure_embedding_env` binds with.
    Asking the toml alone made doctor, the wizard's recap and the
    unavailable-embedding warning all answer "not configured" the moment the
    wizard wrote the endpoint where it now belongs.
    """
    if role_configured_in(load_everos_config(), section):
        return True
    return section == "embedding" and bool(host_embedding_section())


def set_everos_section(section: str, fields: dict[str, Any]) -> None:
    """Merge ``fields`` into ``[section]`` of the user-level toml.

    Three states, because two are not enough to express "remove this":

    - absent or ``None`` -- leave whatever is stored alone;
    - ``""`` -- delete the key from the section;
    - anything else -- store it.

    Without the middle one, dropping only an api_key meant deleting the whole
    section and losing its model and base_url with it. Every other section is
    preserved either way.
    """
    if section not in WRITABLE_SECTIONS:
        raise KeyError(f"unknown everos section {section!r}; writable: {WRITABLE_SECTIONS}")
    _require_owned(f"write [{section}]")
    data = load_everos_config()
    merged = dict(data.get(section, {}))
    for key, value in fields.items():
        if value is None:
            continue
        if value == "":
            merged.pop(key, None)
        else:
            merged[key] = value
    data[section] = merged
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


def set_everos_api(*, host: str, port: int) -> None:
    """Record the address a server for this root must listen on.

    raven used to pass ``--port`` on the command line, which overrode the toml
    and left the file describing an address nobody was using. Writing it instead
    makes the root self-describing: anything that can read the directory knows
    where its server lives, with no second place to drift out of sync.
    """
    set_everos_section("api", {"host": host, "port": int(port)})


def recorded_slice() -> dict:
    """Public read of the recorded everos slice -- the wizard's input to
    discovery.

    The cargo-side describer (``roots``) takes its candidate roots as
    parameters now and imports nothing from the host; the wizard reads the
    record here and passes it in. Public because a caller outside this module
    (onboard) legitimately consumes it -- no one imports the private form.
    """
    return _recorded_slice()
