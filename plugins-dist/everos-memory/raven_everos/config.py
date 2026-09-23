"""EverOS memory settings: the roles raven records, and the file it still writes.

Two homes, on purpose.

**raven's config** holds what serves each of the four roles (llm / embedding /
rerank / multimodal) as a **pin** -- a model and the vendor serving it, never a
credential. The address and key are resolved from that vendor at the moment they
are needed, so rotating a key is one edit and every role on that vendor follows.
Embedding's pin is raven's own top-level block because a knowledge base embeds
with it too and must keep working when this plugin is not the configured
backend; the other three live in this plugin's ``plugins.config`` slice.

**``<root>/everos.toml``** is EverOS's own file. raven writes exactly one
section of it now, ``[api]``, which records where a server for that root
listens. The four role sections are reached by the ``EVEROS_*`` environment
instead -- emitted from the pins on every spawn and bound into this process at
backend start -- which is what lets a change take effect without editing a file
raven may not own. The runtime knobs the file carries (timeouts, batch sizes,
the multimodal file-uri allowlist) are untouched and keep applying: EverOS
merges per key, not per section.

Everything EverOS ships and raven never writes (memory / sqlite / lancedb) is
preserved untouched on every write.

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

1. :func:`migrate_roles` — move any role still in ``everos.toml`` into raven's
   config. Runs every start and skips a role already pinned.
2. :func:`bind_roles_here` — the four roles into this process's environment, so
   the multimodal tool that runs in-process reads the same models the service
   does.
3. :func:`configure_everos_env` — ``EVEROS_ROOT`` → the recorded root
4. :func:`ensure_everos_home` — create ``everos.toml`` + ``ome.toml`` from
   shipped templates (skip if exists) + migrate legacy ``config.toml``.
   Owned roots only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tomli_w

logger = logging.getLogger(__name__)

# Where raven's EverOS home used to live: inside the root EverOS itself
# defaults to. That squatted on a scope slot of the user's own root --
# ``~/.everos/raven`` is exactly the directory EverOS gives an app_id of
# "raven" -- so new installs get ``<raven data dir>/everos`` instead. Existing
# installs keep this one; discovery finds it and nothing is moved.
_LEGACY_EVEROS_SUFFIX = (".everos", "raven")

WRITABLE_SECTIONS = ("api",)
"""What raven still writes into ``everos.toml``.

Only ``[api]`` -- where this root's server listens, which has to be spelled the
same way in the bind and in the health probe. The four role sections moved to
raven's own config and travel as environment variables; leaving them addressable
here would be a second way to write them, and the two would drift.
"""


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


class RoleRequiredError(RuntimeError):
    """Raised when something tries to erase a role listed in REQUIRED_ROLES.

    Its own class rather than a bare ValueError so every door can turn it into
    that door's refusal: the RPC into a ConfigValidationError the page renders,
    the wizard into a line rather than a traceback.
    """


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


_EMBEDDING_ENV_KEYS = (
    "EVEROS_EMBEDDING__MODEL",
    "EVEROS_EMBEDDING__BASE_URL",
    "EVEROS_EMBEDDING__API_KEY",
)

PROVENANCE_ENV = "RAVEN_EVEROS_BOUND"
"""Where the provenance below is kept so it outlives this module object.

``raven gateway --restart`` re-launches through ``os.execv``, which keeps the
environment and builds a new interpreter: the values survive and a set built at
import does not. A restarted gateway then read its own binding as somebody
else's export -- blank settings card, refused save, and no second bind -- for
the rest of its life. Provenance has to travel with the thing it describes.
"""

_BOUND_HERE: set[str] = {k for k in os.environ.get(PROVENANCE_ENV, "").split(",") if k}
"""Which of those variables this process, or the one it replaced, set itself.

Provenance, not a cache. ``bind_roles_here`` puts raven's own pins
into ``os.environ`` so the in-process EverOS imports and every child see it --
after which the variables are present and complete, and a reader that asks only
"are all three set" cannot tell the host's own binding from an operator's
export. It answered "an operator exported these" about values raven had written
a moment earlier, and the settings card then refused an edit by telling the
person to change variables they had never set.
"""


def embedding_is_env_managed() -> bool:
    """Whether the endpoint EverOS uses came from outside this process.

    Named apart from :func:`role_is_env_managed` because one surface needs
    to tell the homes apart rather than only know that one is in force: a
    settings page can offer to edit a file, and cannot offer to edit somebody's
    shell.

    All three present and not all three ours. Partly ours cannot arise -- a
    fragment is not an endpoint, so the binding replaces it whole -- and is
    read the conservative way if it ever does.
    """
    if not all(os.environ.get(k) for k in _EMBEDDING_ENV_KEYS):
        return False
    return not all(k in _BOUND_HERE for k in _EMBEDDING_ENV_KEYS)


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


def _vendor_serving(base_url: str, api_key: str) -> str | None:
    """Which vendor an old section's address and key belong to.

    raven's own configured providers first, through the lookup every migration
    uses -- somebody who configured a row has already said which address that
    vendor serves for them. The wizard's vendor table answers last, by host.

    The table is what carries DeepInfra: its rerank section holds
    ``/v1/inference`` while chat is served from ``/v1/openai``, and on the
    machine this was measured on raven carried no row for it at all.
    """
    from raven.config.update_providers import provider_serving_at

    try:
        named = provider_serving_at(base_url, api_key=api_key)
    except Exception:  # noqa: BLE001 - nothing to match against is not a failure
        named = None
    if named:
        return named
    host = urlparse(base_url).netloc
    if not host:
        return None
    for row in vendors():
        for field in ("base_url", "rerank_base_url"):
            if urlparse(str(row.get(field) or "")).netloc == host:
                return str(row["name"])
    return None


def migrate_roles() -> list[str]:
    """Move the four role sections out of ``everos.toml`` into raven's config.

    Returns the notices a surface should show. Automatic by construction: it
    runs on every backend start, because until it has run raven holds no pin for
    any role and the environment it sends the memory service blanks all four --
    long-term memory simply stops, with a remedy nobody was told to run.

    Scheduled from the plugin rather than raven's config migrations on purpose.
    The host may know this plugin only through the plugin contract, so a branch
    in ``loader.py`` calling in here would be the host importing the plugin --
    and reading ``everos.toml`` or judging who owns a root is not something the
    host can do for itself either.

    Per role: name the vendor from the address and the key, create the provider
    row when raven carries none and move the key into it, then record the pin. A
    role whose vendor cannot be named is **left unset** with a notice -- a
    guessed vendor would send memory's traffic to the wrong endpoint, which is
    worse than a slot somebody fills in once.

    Naming runs for every role before any row is created, so two roles served by
    the same unnamed vendor agree on it rather than racing to describe it
    differently. DeepInfra is exactly that case on the machine this was measured
    on: embedding and rerank, one vendor, two addresses, no row at all.

    Idempotent: a role raven already holds a pin for is skipped, so running it on
    every start means the same thing as running it once.
    """
    notices: list[str] = []
    if not everos_owned():
        # A root the user manages: raven records its address and never edits its
        # config, so the sections in it are theirs and stay where they are.
        return notices

    toml = load_everos_config()
    if not toml:
        return notices

    named: dict[str, tuple[str, dict[str, Any]]] = {}
    for section in ROLES:
        if role_pin(section) is not None:
            continue
        block = toml.get(section)
        if not isinstance(block, dict):
            continue
        model, base_url = str(block.get("model") or ""), str(block.get("base_url") or "")
        api_key = str(block.get("api_key") or "")
        if not (model and base_url and api_key):
            # The shipped template seeds every role with a real model name and an
            # empty key. Migrating that reports a role as configured that has
            # never worked -- the same bar the gate's own criterion sets.
            continue
        vendor_name = _vendor_serving(base_url, api_key)
        if vendor_name is None:
            notices.append(
                f"EverOS {section}: could not tell which provider serves {base_url}, so the role is "
                f"unset -- pick a model and a provider for it in settings"
            )
            continue
        named[section] = (vendor_name, block)

    from raven.config.update_providers import resolve_provider_credentials, set_provider_fields

    for vendor_name, block in named.values():
        try:
            if resolve_provider_credentials(vendor_name):
                continue
        except KeyError:
            pass
        # A vendor raven carries no spec for is still configurable when LiteLLM
        # knows it, which every name in the table is: the plain section with an
        # address and a key is all such a provider needs.
        row = vendor(vendor_name)
        set_provider_fields(
            vendor_name,
            {
                "api_key": str(block.get("api_key") or ""),
                "api_base": str((row or {}).get("base_url") or block.get("base_url") or ""),
            },
        )

    for section, (vendor_name, block) in named.items():
        protocol = ""
        if section == "rerank" and not rerank_protocol(vendor_name):
            # The old file's own `provider` field, which names a request shape
            # rather than a vendor. Kept only where the table cannot answer.
            protocol = str(block.get("provider") or "")
        try:
            set_role(section, model=str(block["model"]), provider=vendor_name, protocol=protocol)
        except Exception as exc:  # noqa: BLE001 - one role must not take the others down
            # Per role, because the writers validate. `set_embedding_endpoint`
            # refuses a pair that cannot embed, and an old file naming a chat
            # model there is exactly the install this migration exists for --
            # letting that escape left llm migrated, rerank and multimodal not,
            # and the binding below never run at all.
            logger.warning("everos: could not migrate the %s role: %s", section, exc)
            notices.append(
                f"EverOS {section}: {exc}. The role is unset -- pick a model and a provider for it in settings."
            )

    return notices


def everos_role_configured(section: str) -> bool:
    """True iff the user really configured this EverOS role.

    Sole criterion for "configured", shared by all nine callers. It reads raven's
    own config now: the role pins moved there, and ``everos.toml`` is no longer
    written for these sections at all.

    Lives beside the writers rather than in the wizard so a reader does not have
    to import it: the wizard module costs ~290ms to load, which `raven doctor`
    (a millisecond command) would otherwise pay just to answer this.

    Three things now, not two: a model, a vendor, and that vendor resolving to a
    usable credential. The old reading -- model AND api_key in ``everos.toml`` --
    cannot be computed once raven's blocks hold no key, and "a model pinned to a
    vendor with no credential" is precisely the state that would spawn a server
    doomed to die building its LLM client.

    One rule, two readers: the settings page reports the same answer as
    ``api_key_set``, so the gate and the card cannot drift apart.

    ``llm`` gates long-term memory outright, so a wrong answer here is memory
    switched off without a word. A role the operator manages through exported
    variables counts as configured: raven cannot read their shell, but it can
    see the endpoint is there.
    """
    if role_is_env_managed(section):
        return True
    return resolve_role(section) is not None


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


# Curated OpenAI-compatible endpoints for EverOS memory models. Picking one
# pre-fills its base_url (mirrors the main provider step); everything else is
# reachable via "reuse an existing endpoint" or "custom" (type a base_url).
# These are the providers' documented OpenAI-compatible /v1 endpoints.
VENDORS: list[dict[str, Any]] = [
    {
        "name": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "supports": {"llm", "embedding", "multimodal"},
    },
    {
        "name": "openrouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "supports": {"llm", "embedding", "rerank", "multimodal"},
        "rerank_protocol": "vllm",
    },
    {
        "name": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "supports": {"llm"},
    },
    {
        "name": "deepinfra",
        "label": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "supports": {"llm", "embedding", "rerank"},
        "rerank_protocol": "deepinfra",
        "rerank_base_url": "https://api.deepinfra.com/v1/inference",
    },
    {
        "name": "siliconflow",
        "label": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "supports": {"llm", "embedding", "rerank"},
        "rerank_protocol": "vllm",
    },
    {
        "name": "dashscope",
        "label": "DashScope (Alibaba)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "supports": {"llm", "embedding", "rerank"},
        "rerank_protocol": "dashscope",
        "rerank_base_url": "https://dashscope.aliyuncs.com",
    },
]


def vendors() -> list[dict[str, Any]]:
    """Every vendor a role may be pinned to.

    The curated table plus the slots raven keeps for a deployment of the
    operator's own. Those are read from raven's provider registry rather than
    listed a second time here: their names, labels and default addresses are
    already declared there, and the main chat model has always been able to use
    them -- a memory role that could not was the odd one out, not a design.

    They carry every role and no rerank protocol. What somebody's own box serves
    is theirs to say, so the wizard asks and records the answer on the role,
    which is the one case the curated table cannot answer for.
    """
    from raven.providers.registry import PROVIDERS

    curated = {str(row.get("name") or "") for row in VENDORS}
    rest: list[dict[str, Any]] = []
    for spec in PROVIDERS:
        if spec.name in curated:
            continue
        self_host = spec.is_local or spec.name == "custom"
        rest.append(
            {
                "name": spec.name,
                "label": spec.label,
                "base_url": spec.default_api_base,
                # Every role but rerank. What the curated table above knows that
                # the registry does not is the rerank request shape, and nothing
                # else -- the other three are ordinary OpenAI-compatible calls
                # any vendor here can serve. Narrowing them to the curated six
                # took forty-odd vendors off the slots, which was never the bug
                # being fixed; the rerank slot offering a vendor that cannot
                # rerank was.
                #
                # Self-hosted rows are the exception: what somebody's own box
                # serves is theirs to say, so they carry rerank too and the
                # wizard asks for the shape.
                "supports": set(ROLES) if self_host else {"llm", "embedding", "multimodal"},
                **({"self_host": True} if self_host else {}),
            }
        )
    return [*VENDORS, *rest]


def vendor(name: str) -> dict[str, Any] | None:
    """The row `vendors()` carries for ``name``, or None for one it does not."""
    return next((v for v in vendors() if v.get("name") == name), None)


def vendor_supports(name: str, role: str) -> bool:
    """Whether this vendor can serve ``role`` at all.

    False for a vendor the table does not carry, because offering a role a vendor
    cannot serve is how the rerank slot came to list OpenAI -- the slot asked only
    whether a provider had a key.
    """
    row = vendor(name)
    return bool(row and role in (row.get("supports") or ()))


RERANK_PROTOCOLS: tuple[str, ...] = ("deepinfra", "vllm", "dashscope")
"""The request shapes EverOS can build a rerank client for.

EverOS's own ``rerank.provider`` field, whose values these are. Named here
because three surfaces have to agree on them -- the vendor table's
``rerank_protocol``, the wizard's question for a self-hosted endpoint, and the
settings page's validation -- and a fourth spelling would be accepted, stored,
and then fail at the first query.
"""


def rerank_protocol(name: str) -> str | None:
    """Which client implementation EverOS must build for this vendor.

    EverOS's ``rerank.provider`` names a request shape, not a vendor: DeepInfra
    posts to ``{base}/{model}`` while vLLM posts to ``{base}/rerank``. None means
    "say nothing", which leaves whatever the file holds -- the honest answer for a
    vendor this table has never heard of.
    """
    row = vendor(name)
    return row.get("rerank_protocol") if row else None


def rerank_protocol_for_role() -> str | None:
    """The request shape reranking must use, whoever knows it.

    The curated table first, because a vendor's shape is a fact about the vendor.
    The value recorded on the role second, for a self-hosted endpoint the table
    has never heard of -- there the operator is the only one who knows, and the
    wizard asked them. Not two homes for one fact: the table is silent exactly
    where the recorded answer exists.
    """
    pin = role_pin("rerank")
    if pin is None:
        return None
    return rerank_protocol(pin[1]) or str((_recorded_slice().get("rerank") or {}).get("protocol") or "") or None


def rerank_base_url(name: str, default: str) -> str:
    """The address reranking goes to, which is not always the chat address.

    DeepInfra serves rerank from ``/v1/inference`` and chat from ``/v1/openai``;
    borrowing the chat one is why rerank configured from the settings page has
    never worked against it.
    """
    row = vendor(name)
    return (row or {}).get("rerank_base_url") or default


ROLES: tuple[str, ...] = ("llm", "embedding", "rerank", "multimodal")
"""The four models EverOS talks to. ``embedding`` is listed with the rest because
callers reason about four roles, even though its pin lives somewhere else."""

REQUIRED_ROLES: tuple[str, ...] = ("llm", "embedding")
"""Roles that cannot be cleared from the page.

Clearing ``llm`` turns long-term memory off outright, and ``embedding`` is what
every stored vector was written under. Neither should be one stray click away;
both are still editable, just not erasable.
"""


@dataclass(frozen=True)
class RoleEndpoint:
    """What a role resolves to at the moment it is asked: an address and a key.

    Built on demand, never stored. The stored form is a pin -- a model and a
    provider -- so rotating a key is one edit in the provider and every role
    serving on it follows.
    """

    model: str
    base_url: str
    api_key: str
    dimensions: int | None = None


def role_pin(section: str) -> tuple[str, str] | None:
    """What raven records for ``section``: a model and the vendor serving it.

    Two homes, one reader. ``embedding`` is raven's own top-level block because a
    knowledge base embeds with it too and must keep working when the memory
    plugin is not the configured backend; the other three are this plugin's
    slice. Callers should not have to know which is which.
    """
    if section not in ROLES:
        raise KeyError(f"unknown everos role {section!r}; roles: {ROLES}")
    if section == "embedding":
        block = _raven_config_raw().get("embedding") or {}
    else:
        block = _recorded_slice().get(section) or {}
    model, provider = str(block.get("model") or ""), str(block.get("provider") or "")
    return (model, provider) if model and provider else None


def resolve_role(section: str) -> RoleEndpoint | None:
    """The pin turned into an address and a key, or None when nothing is pinned.

    Resolved on every call rather than cached: a value written a second ago has
    to reach the next spawn without restarting raven.

    ``None`` rather than a raise, for three states that are all ordinary: nothing
    pinned, a provider that no longer exists, a provider with no usable
    credential. The caller turns that into "set this up first"; a raise here
    would take down a path that merely wanted to know.

    Rerank asks the vendor table for its address, because the endpoint that
    serves reranking is not always the one that serves chat.
    """
    pin = role_pin(section)
    if pin is None:
        return None
    model, provider = pin
    try:
        from raven.config.update_providers import resolve_provider_credentials
        from raven.providers.wire import wire_model
    except Exception:  # noqa: BLE001 - an import failure here is not this module's to report
        return None
    try:
        resolved = resolve_provider_credentials(provider)
    except KeyError:
        # A pin naming a provider that has since been removed. Ordinary enough
        # that it must not take the gate down: unconfigured, not broken.
        logger.warning("everos: the %s role names provider %r, which is not configured", section, provider)
        return None
    if resolved is None:
        logger.warning("everos: the %s role names provider %r, which has no usable credential", section, provider)
        return None
    base_url, api_key = resolved
    if section == "rerank":
        base_url = rerank_base_url(provider, base_url)
    dimensions = None
    if section == "embedding":
        raw = (_raven_config_raw().get("embedding") or {}).get("dimensions")
        dimensions = int(raw) if isinstance(raw, int) and raw > 0 else None
    return RoleEndpoint(
        model=wire_model(model, client_provider=provider),
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        dimensions=dimensions,
    )


def set_role(section: str, *, model: str, provider: str, protocol: str = "") -> str:
    """Record what serves ``section``.

    The guard is here, not at the caller: ``_require_owned`` sits at the write
    primitives so a new caller cannot opt out, and moving these writes out of
    ``everos.toml`` was very nearly that new caller.

    Embedding goes through ``set_embedding_endpoint`` -- the same writer a
    knowledge base's own settings use, which validates that the pair can actually
    embed. Writing it into this plugin's slice instead would save cleanly and
    never take effect, because nothing reads embedding there.

    ``protocol`` is recorded only for rerank pinned to a self-hosted endpoint,
    where the vendor table cannot name the request shape and the operator can.
    See :func:`rerank_protocol_for_role` for the precedence.

    Returns what moving this role costs, empty when it costs nothing. Only
    embedding has an answer: everything already embedded answers to the old
    model, and the caller is the one place that can say so while the person is
    still looking at the change they made.
    """
    if section not in ROLES:
        raise KeyError(f"unknown everos role {section!r}; roles: {ROLES}")
    if section == "embedding":
        # Not gated on owning the EverOS root, and the other three are. This
        # value is raven's own block, which a knowledge base embeds with whether
        # or not this plugin is the configured backend -- refusing to write it
        # because somebody else manages an EverOS directory couples two things
        # that have nothing to do with each other. It is also editable from the
        # knowledge settings, which never asked about a root at all.
        from raven.config.update import embedding_model_change, set_embedding_endpoint

        fields = {"model": model, "provider": provider}
        previous = set_embedding_endpoint(fields)
        return embedding_model_change(previous, fields)
    _require_owned(f"configure the {section} role")
    from raven.config.update import set_plugin_config_fields

    block: dict[str, str] = {"model": model, "provider": provider}
    if section == "rerank":
        # Written every time, empty included: a vendor switch that left the old
        # box's shape behind would post the wrong request to the new endpoint.
        block["protocol"] = protocol if not rerank_protocol(provider) else ""
    set_plugin_config_fields("everos-memory", {section: block})
    return ""


def clear_role(section: str) -> None:
    """Forget what serves ``section``; the next spawn emits it empty.

    Emitting it empty is what makes this mean anything: raven no longer writes
    ``everos.toml``, so a section left in that file would otherwise come back
    into force the moment raven stopped naming a model.
    """
    if section not in ROLES:
        raise KeyError(f"unknown everos role {section!r}; roles: {ROLES}")
    if section in REQUIRED_ROLES:
        # The rule lives here, with the operation, rather than only at the RPC
        # door that used to be its only reader. The wizard reaches this function
        # too, and its own table answers a different question -- `optional`
        # means "may be left unset", not "may be erased" -- so it cleared the
        # one endpoint every knowledge base embeds with.
        raise RoleRequiredError(f"{section} is required for EverOS memory and cannot be cleared")
    _require_owned(f"clear the {section} role")
    if section == "embedding":
        from raven.config.update import set_embedding_endpoint

        set_embedding_endpoint({"model": "", "provider": ""})
        return
    from raven.config.update import set_plugin_config_fields

    set_plugin_config_fields("everos-memory", {}, remove=(section,))


def role_is_env_managed(section: str) -> bool:
    """Whether ``section``'s endpoint came from outside this process.

    Generalises ``embedding_is_env_managed`` to all four roles. An operator who
    exports ``EVEROS_<SECTION>__*`` outranks raven -- the settings page already
    refuses a save it could not honour -- and raven must not blank what it does
    not own either. The provenance set is what tells "somebody else exported
    this" from "raven bound this a moment ago"; it survives ``os.execv`` through
    ``PROVENANCE_ENV``, because a restarted gateway reading its own binding as
    somebody else's export is a bug this codebase has already had.
    """
    keys = (
        f"EVEROS_{section.upper()}__MODEL",
        f"EVEROS_{section.upper()}__BASE_URL",
        f"EVEROS_{section.upper()}__API_KEY",
    )
    if not all(os.environ.get(k) for k in keys):
        return False
    return not all(k in _BOUND_HERE for k in keys)


def everos_env() -> dict[str, str]:
    """The binding all four roles earn, from raven's own config.

    Every role is emitted on every spawn. A role raven does not hold is emitted
    **empty**, which suppresses whatever ``everos.toml`` says. That is what makes
    clearing a role in the UI mean anything now that raven no longer edits that
    file, and what stops an upgraded install's stale section -- old model, old key
    -- from coming back into force.

    Only the model and credential keys travel, so the runtime knobs in the file
    (timeouts, batch sizes, the multimodal file-uri allowlist) keep applying:
    EverOS merges per key, not per section. Measured, not assumed --
    ``.work_context/everos_role_config_moves_to_raven/spikes/env_merge.py``.

    Two keys are deliberately not emitted empty, because both are typed and an
    empty string is not "unset" to a typed field -- it is invalid. The rerank
    protocol has no sensible empty request shape, and ``DIMENSIONS`` is an int:
    sending ``""`` for it fails EverOS\'s own settings validation and the server
    exits before it serves anything. Measured 2026-09-22 against a real spawn,
    which is the only layer that could have said so -- nothing below it builds
    EverOS\'s ``Settings``.

    A role the operator has exported for themselves is skipped whole -- see
    ``role_is_env_managed``. "raven emits what it holds" is about the roles raven
    manages, and a shell raven cannot edit is not one of them.
    """
    env: dict[str, str] = {}
    for section in ROLES:
        if role_is_env_managed(section):
            # Not ours to say anything about, empty included: the operator put a
            # complete endpoint in the environment on purpose.
            continue
        prefix = f"EVEROS_{section.upper()}__"
        endpoint = resolve_role(section)
        env[f"{prefix}MODEL"] = endpoint.model if endpoint else ""
        env[f"{prefix}BASE_URL"] = endpoint.base_url if endpoint else ""
        env[f"{prefix}API_KEY"] = endpoint.api_key if endpoint else ""
        if section == "embedding" and endpoint and endpoint.dimensions:
            env[f"{prefix}DIMENSIONS"] = str(endpoint.dimensions)
        if section == "rerank":
            protocol = rerank_protocol_for_role()
            if protocol:
                env[f"{prefix}PROVIDER"] = protocol
    return env


def role_env_digest() -> str:
    """A digest of everything :func:`everos_env` would hand a spawn.

    EverOS builds its model clients once, in the API lifespan, so a running
    server keeps the credentials it booted with. A key rotated in raven's
    provider settings afterwards reaches the file and never reaches that
    process -- and a health probe cannot tell the difference, because it never
    touches a model. Recording this at the spawn is what lets the next start
    ask whether the server it is about to adopt is running on what raven still
    holds.

    Over the emitted environment rather than over the provider sections: an
    env-managed role is skipped by ``everos_env`` and must be skipped here too,
    or raven would restart a server over a value it does not own.
    """
    env = everos_env()
    blob = "\n".join(f"{name}={env[name]}" for name in sorted(env))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def everos_toml_role_notes() -> list[str]:
    """What ``everos.toml`` still says about the four roles, and what wins.

    Two sentences, both of which somebody hits. A section raven now holds a pin
    for is dead text that still reads like configuration -- an operator who
    edits it and sees nothing change has no way to find out why. A section raven
    holds no pin for has not moved yet, which happens the next time memory
    starts; saying so is the difference between "wait" and "reconfigure".

    Read-only, and reported through the backend's health rather than written by
    doctor: the move belongs to the start path, and doctor is the one command
    that may not write.
    """
    overridden: list[str] = []
    pending: list[str] = []
    for section in ROLES:
        try:
            if not (everos_section(section) or {}).get("model"):
                continue
            (overridden if role_pin(section) is not None else pending).append(section)
        except Exception:  # noqa: BLE001 - a report never fails on a read
            return []

    notes: list[str] = []
    if overridden:
        notes.append(
            f"[{'] ['.join(overridden)}] is still in the file, and raven's own configuration "
            "serves those roles now and overrides it -- editing them there changes nothing"
        )
    if pending:
        notes.append(
            f"[{'] ['.join(pending)}] has not moved into raven's configuration yet; it moves the "
            "next time long-term memory starts, and needs no command"
        )
    return notes


def bind_roles_here() -> dict[str, str]:
    """Put all four roles into *this* process, as a spawn puts them into its child.

    ``understand_media`` runs multimodal inside raven through EverOS's cached
    ``load_settings()``, so a role that only ever reached the spawned server was
    a role that tool could not use. One source for both paths -- ``everos_env``
    -- so the answer cannot differ by which door it came through.

    Recorded in the provenance set, and through it in the environment: the next
    reader has to tell raven's own binding from an operator's export, and the
    record survives ``os.execv`` for exactly that reason. A restarted gateway
    reading its own binding as somebody else's is a bug this codebase has had.

    Must run BEFORE anything imports EverOS's settings, same as
    :func:`configure_everos_env`: ``load_settings`` is cached on first read.
    """
    env = everos_env()
    os.environ.update(env)
    _BOUND_HERE.update(env)
    os.environ[PROVENANCE_ENV] = ",".join(sorted(_BOUND_HERE))
    return env


def describe_roles() -> dict[str, Any]:
    """What the settings page needs to render the four role slots.

    Reports the pin as stored -- a model and the vendor serving it -- so the page
    stops reverse-looking-up a vendor from an address, which answered blank for
    every endpoint that did not match a provider's own base url character for
    character.

    ``api_key_set`` means "this vendor has a usable credential", the same
    question :func:`everos_role_configured` answers. One rule, two readers: a
    card that said "key set" while the gate said "not configured" is exactly the
    disagreement this collapses.

    ``supports`` rides along rather than getting a call of its own. The page
    needs it at the moment it renders these slots -- a rerank slot must not offer
    a vendor that cannot rerank -- and a second round trip would let the two
    answers disagree.

    EverOS's rerank protocol is deliberately absent: it is derived from the
    vendor table now, not chosen by anyone, and returning it under the name
    ``provider`` is what made one wire field mean two different things.

    ``required`` names the roles that cannot be cleared, because the page has to
    know which slots get a clear control and guessing put one on a slot whose
    clear the write refuses.
    """
    sections: dict[str, Any] = {}
    for section in ROLES:
        pin = role_pin(section)
        sections[section] = {
            "model": pin[0] if pin else "",
            "provider": pin[1] if pin else "",
            "api_key_set": everos_role_configured(section),
            "env_managed": role_is_env_managed(section),
        }
    supports: dict[str, list[str]] = {}
    for row in vendors():
        name = str(row.get("name") or "")
        if name:
            supports[name] = sorted(row.get("supports") or ())
    return {
        "available": True,
        "owned": everos_owned(),
        "config_path": str(get_everos_config_path()),
        "sections": sections,
        "supports": supports,
        # Sent rather than mirrored, because the page was mirroring it and had
        # drifted: it drew a clear button on embedding, which the write refuses.
        # A contract the caller has to remember is a contract that goes stale.
        "required": list(REQUIRED_ROLES),
    }
