"""Configuration loading utilities."""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from raven.config.schema import Config

# Generation counter for the run-once config migrations below. Bump it, and add
# the matching rule, when a migration must run exactly once per config rather
# than on every load -- the watermark is what lets a user re-set by hand
# whatever a migration cleared. Kept out of the schema on purpose: see
# ``_stamp_path``.
CURRENT_CONFIG_VERSION = 2

# The generation that introduced each run-once migration. Each is gated on its
# own floor rather than on "is this config current", because those are not the
# same question: a config stamped at 1 is behind the current mark while having
# already been through generation 1. Gating the pair on one shared mark meant
# raising it to 2 for the provider migration re-opened the context-window one
# on every config that went through 0.1.11/0.1.12 -- deleting a
# ``contextWindowTokens: 65536`` the user had put back by hand, after our own
# notice invited them to. Adding a migration means adding a floor here.
_CONTEXT_WINDOW_MIGRATION = 1
_AUTO_PROVIDER_MIGRATION = 2

# The context window every pre-0.1.11 bootstrap wrote to disk verbatim: back
# then ``AgentDefaults.context_window_tokens`` defaulted to this number and
# ``save_config`` dumped every default. A pin is honoured over the model's real
# window by design, so on an upgraded install this exact value silently caps
# every model at 64k -- see ``_migrate_legacy_context_window``.
LEGACY_CONTEXT_WINDOW_TOKENS = 65_536

# Single source of truth for Raven extension block keys.
# Both _migrate_config (pop before base Config validates) and
# load_raven_config (extract into overrides) reference this.
# Add new extension blocks here — one place, no duplication.
EXTENSION_KEYS = (
    "context",
    "sentinel",
    "tokenWise",
    "skillForge",
    "token_wise",
    "skill_forge",
    # CFG-1 additions: each key is listed in both camelCase (preferred
    # by config files) and snake_case (preferred by Python).
    "plugins",
    "memory",
    # Bug2 / runtime-discipline 5th pillar — checkpoint policy etc.
    "runtime",
    # In-tree observability tracing (raven.tracing).
    "tracing",
)

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None

# Paths already warned about as malformed in this process; repeated
# load_config calls (status/doctor load more than once) warn only once.
_warned_paths: set[str] = set()

# User-facing lines produced by a migration that actually changed something,
# waiting to be printed by whichever CLI entry point owns the terminal.
# Migrations run inside the loader, which has no console of its own and whose
# logs land in a file nobody reads; a config the user can see us edit has to be
# announced where the user is looking. Drained, not read, so N loads per
# process yield one telling.
_migration_notices: list[str] = []


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".raven" / "config.json"


class ConfigReadError(Exception):
    """An existing config file could not be parsed. Callers doing a
    read-modify-write MUST NOT proceed: overwriting would replace the user's
    whole config with just their section (data loss). Only a genuinely-absent
    file is safe to create fresh.

    Deliberately NOT a RuntimeError: the CLI write commands wrap their ops in a
    broad ``except RuntimeError`` (for provider OAuth-refusal etc.), and we want
    a parse error to bypass those and reach the single ``run()`` handler (or a
    caller's explicit ``except ConfigReadError``), not be swept up implicitly."""


def read_raw_or_raise(path: Path) -> dict[str, Any]:
    """Read a config file as raw JSON for a read-modify-write cycle.

    Returns ``{}`` ONLY when the file is absent. A present-but-unreadable file
    raises :class:`ConfigReadError` rather than returning ``{}`` -- returning
    ``{}`` and then writing was the bug that wiped a real config over a lone
    JSON syntax error (e.g. a // comment). The single read path for every
    ``update_*`` write module.
    """
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}  # empty file: no data to lose, safe to create fresh (like absent)
        data = json.loads(text)
        # A valid-JSON non-object (null / list / scalar) is not a usable config;
        # return {} so callers get a mapping (not None) without an AttributeError.
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ConfigReadError(
            f"{path} is not valid JSON ({exc}). Fix it first (JSON allows no comments or "
            "trailing commas); your config was left unchanged."
        ) from exc


def drain_migration_notices() -> list[str]:
    """Take the pending migration notices, clearing them.

    Drained rather than read so that a process loading the config several times
    (status and doctor do; the TUI RPC server reloads every turn) tells the user
    once. Callers own a console -- see ``cli._helpers``.
    """
    notices = list(_migration_notices)
    _migration_notices.clear()
    return notices


def _stamp_path(config_path: Path) -> Path:
    """Where the migration watermark for ``config_path`` lives.

    Deliberately NOT inside the config: ``Config`` is ``extra='forbid'`` and
    ``load_config`` raises on a validation error, so a key this build knows and
    an older one does not turns every older build into a hard boot failure --
    a reverted release, a pinned older version, or just a second checkout
    sharing the same ~/.raven. A sidecar is ours to write and invisible to any
    build that has never heard of it, which also keeps a revert of this change
    clean.

    Sibling of the config so a ``--config`` path or a second instance gets its
    own watermark rather than borrowing the default one's.
    """
    return config_path.with_name(config_path.stem + ".migrations.json")


def _migration_version(config_path: Path) -> int:
    """Which generation of stamped migrations ``config_path`` has been through.

    Absent / unreadable / malformed all mean generation 0: the migrations are
    written to be safe to re-run, so erring towards running them beats trusting
    a file we could not parse.
    """
    try:
        data = json.loads(_stamp_path(config_path).read_text(encoding="utf-8"))
        return int(data["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _write_migration_version(config_path: Path) -> None:
    """Record that this config has been through the current migrations.

    Written even when nothing needed changing -- this is our file, not the
    user's, so stamping it costs the user nothing and buys the guarantee that
    matters: from here on, a ``contextWindowTokens`` the user sets by hand is
    never second-guessed, whatever its value.
    """
    stamp = _stamp_path(config_path)
    try:
        stamp.write_text(json.dumps({"version": CURRENT_CONFIG_VERSION}) + "\n", encoding="utf-8")
    except OSError as exc:
        logging.getLogger(__name__).debug("Could not write the migration stamp %s: %s", stamp, exc)


def _migrate_legacy_context_window(data: dict[str, Any], *, notify: bool = False) -> bool:
    """Clear the retired 65536 context-window pin. True when ``data`` changed.

    Callers gate this on ``_CONTEXT_WINDOW_MIGRATION`` -- its own floor, not the
    current mark -- because the value carries no provenance: 65536 written by the
    old bootstrap and 65536 chosen by a user are the same three bytes. Running it
    once means we clear what we planted; from then on the number is the user's,
    and the ladder in ``providers.rates.effective_context_window`` honours it like
    any other pin. A later generation must not bring this back: the user we would
    hit is the one who read our notice and put the line back on purpose.

    ``notify`` is for the caller that has a user to tell -- the persist pass
    re-runs this on the raw file and must not double-announce.
    """
    agents = data.get("agents")
    defaults = agents.get("defaults") if isinstance(agents, dict) else None
    if not isinstance(defaults, dict):
        return False

    changed = False
    for legacy_key in ("contextWindowTokens", "context_window_tokens"):
        if defaults.get(legacy_key) == LEGACY_CONTEXT_WINDOW_TOKENS:
            defaults.pop(legacy_key)
            changed = True
            logging.getLogger(__name__).info(
                "Migrated: dropped agents.defaults.%s (the retired 65536 default)",
                legacy_key,
            )

    if not changed:
        return False

    notice = (
        f"Removed the leftover `contextWindowTokens: {LEGACY_CONTEXT_WINDOW_TOKENS}` from your config (an old "
        "default, written there by earlier versions). The context window now follows each model's real size. "
        "Put the line back if you did want that number -- for an endpoint served with a smaller window, for "
        "instance."
    )
    # Deduped, not just drained: the extension loader migrates its own read of
    # the same file, so one command can walk this path twice before anything is
    # printed.
    if notify and notice not in _migration_notices:
        _migration_notices.append(notice)
    return True


def _migrate_auto_provider(data: dict[str, Any], *, notify: bool = False) -> bool:
    """Write down the vendor an ``auto`` config was in fact resolving to.

    ``agents.defaults.provider: "auto"`` never detected anything. For a bare
    model id it walked ``PROVIDERS`` in registry order and took the first
    configured one that claimed the id -- so with both anthropic and openrouter
    keyed, ``gpt-4.1`` went to openrouter over openai because openrouter sits
    third in a list and openai eighth. Which vendor's key paid for a call was
    decided by an array index.

    So the value is resolved once, here, and written down. Behaviour does not
    change -- the answer is exactly what the old derivation would have given --
    but it becomes something the user can read in their own file and argue
    with. A config the resolution cannot answer for (no configured provider
    serves that model) is left alone: an empty provider is reported where it is
    used, which is a better failure than a vendor picked to fill the blank.
    """
    agents = data.get("agents")
    defaults = agents.get("defaults") if isinstance(agents, dict) else None
    if not isinstance(defaults, dict):
        return False
    current = defaults.get("provider")
    if current not in (None, "", "auto"):
        return False

    try:
        # Keep only what ``Config`` declares, by field name or alias. ``Config``
        # is extra='forbid', and this runs before the shims that relocate legacy
        # top-level blocks, so a config still carrying ``skillRouter`` would fail
        # the probe -- and be stamped anyway, so it would never be retried. That
        # lands on the oldest configs, which are the ones most likely to still
        # say ``auto``. A name filter rather than a pop list: the next shim adds
        # a legacy key without having to remember this one.
        allowed = {name for name in Config.model_fields}
        allowed |= {f.alias for f in Config.model_fields.values() if f.alias}
        probe = {k: v for k, v in data.items() if k in allowed}
        probe_agents = dict(agents or {})
        probe_defaults = dict(defaults)
        probe_defaults["provider"] = "auto"
        probe_agents["defaults"] = probe_defaults
        probe["agents"] = probe_agents
        resolved = Config.model_validate(probe)._match_provider()[1]
    except Exception as exc:
        logging.getLogger(__name__).debug("could not resolve the implicit provider: %s", exc)
        return False
    if not resolved:
        return False

    defaults["provider"] = resolved
    logging.getLogger(__name__).info("Migrated: agents.defaults.provider %r -> %r", current, resolved)
    notice = (
        f"Wrote `provider: {resolved}` into your config. It was unset, which used to mean "
        "'pick one by walking a list' -- the same vendor you were already getting, now "
        "written down instead of inferred. Change it if that is not the one you meant."
    )
    if notify and notice not in _migration_notices:
        _migration_notices.append(notice)
    return True


def _persist_migrations(path: Path, from_version: int = 0) -> None:
    """Apply the stamped migrations to the file itself, then stamp. Best effort.

    Re-reads the raw file instead of reusing the mapping ``load_config`` already
    migrated: that one has the extension blocks popped (see
    ``pop_extension_keys``), so writing it back would delete the user's memory /
    plugins / skillForge sections. ``save_config`` is no use here either -- it
    dumps every default (~8 KB), re-planting the very kind of fossil this
    migration exists to pull out. So: surgical edit, atomic replace.

    A config that needed no edit is left byte for byte alone; only the sidecar
    stamp is written. Commands like ``provider use`` promise not to touch a
    config they decided against changing, and that promise is theirs to keep,
    not ours to spend.

    Silent on failure (read-only home): the in-memory migration already made
    this process correct, and the write only serves to keep the file from
    disagreeing with it.
    """
    try:
        raw = read_raw_or_raise(path)
    except ConfigReadError:
        return

    changed = False
    if raw:
        # Gated on the same per-migration floors the in-memory pass used, so the
        # file and the loaded config owe each other nothing: persisting a
        # migration the load path skipped would write an edit this process is
        # not running on.
        if from_version < _CONTEXT_WINDOW_MIGRATION:
            changed = _migrate_legacy_context_window(raw) or changed
        if from_version < _AUTO_PROVIDER_MIGRATION:
            changed = _migrate_auto_provider(raw) or changed
    if changed:
        # PID in the name: two processes migrating at once would otherwise share
        # one temp path, and the second's truncating write could be read as an
        # empty config.json by anyone loading between it and the replace.
        tmp = path.with_name(f"{path.name}.migrating.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
            # os.replace swaps the inode, so the original's mode is not carried
            # over by anything: a config the user tightened to owner-only (it
            # holds providers.*.apiKey) would come back world-readable. See
            # config.paths.restrict_to_owner on why a replacing writer owns this.
            try:
                os.chmod(tmp, path.stat().st_mode & 0o7777)
            except OSError:
                pass
            os.replace(tmp, path)
        except OSError as exc:
            logging.getLogger(__name__).debug("Could not persist config migration to %s: %s", path, exc)
            tmp.unlink(missing_ok=True)
            return

    _write_migration_version(path)


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    config: Config | None = None
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # Read the watermark before the migrations run, and let it gate
            # them: once stamped, this costs one small sidecar read per load
            # (the TUI RPC server reloads every turn) and nothing else.
            from_version = _migration_version(path)
            unstamped = from_version < CURRENT_CONFIG_VERSION
            data = _migrate_config(data, from_version=from_version)
        except json.JSONDecodeError as e:
            # Boot on defaults for a malformed file (a transient mid-write race
            # shouldn't brick callers) but warn LOUDLY -- a persistent syntax
            # error would else revert every setting with no visible cause.
            # Raising instead needs atomic save_config first (separate change).
            msg = (
                f"config at {path} is not valid JSON ({e}) -- IGNORING it and running on "
                "DEFAULTS. Fix the file (JSON allows no comments or trailing commas) and restart."
            )
            # Single user-visible channel: the stderr print (visible under any
            # loguru sink config). The log-file trace uses stdlib logging, NOT
            # loguru — loguru's default sink echoes DEBUG to stderr, which
            # would re-duplicate the warning on plain CLI runs; the stdlib
            # record reaches the file sink via the CLI's logging intercept.
            if str(path) not in _warned_paths:
                _warned_paths.add(str(path))
                print(f"WARNING: {msg}", file=sys.stderr)
            logging.getLogger(__name__).debug(msg)
        else:
            # A clean parse re-arms the warning: the dedup exists to silence
            # repeated loads of the same broken state within one command, not
            # to spend the one warning a long-lived process (the TUI RPC
            # server reloads every turn) gets for a later re-breakage.
            _warned_paths.discard(str(path))
            try:
                config = Config.model_validate(data)
            except ValidationError as e:
                # Schema mismatch is a user/programmer error — surface
                # loudly rather than masking with defaults. Silently
                # using defaults makes "feature X did nothing" debug
                # take 24h instead of 24s.
                raise ValueError(
                    f"Config at {path} fails schema validation:\n{e}",
                ) from e
            # Only once the migrated data is known to validate: a file we
            # cannot load is a file we have no business rewriting.
            if unstamped:
                _persist_migrations(path, from_version)

    if config is None:
        config = Config()

    return config


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Only what differs from the defaults is written. A dump of everything is
    lossless on reload either way -- a value equal to its default reloads as
    that default -- but it freezes today's defaults into the user's file, and
    then a default we change later never reaches anyone who already has one.
    That is not hypothetical: ``contextWindowTokens: 65536`` was written this
    way, and every upgraded install stayed capped at 64k on a 1M model until a
    migration went and took it out again. Writing less means the next default
    we improve simply applies.

    It also leaves a file a person can read: the handful of lines they chose,
    rather than eight kilobytes of settings they have never heard of.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(by_alias=True, exclude_defaults=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict, *, pop_extension_keys: bool = True, from_version: int = CURRENT_CONFIG_VERSION) -> dict:
    """Migrate old config formats to current.

    ``pop_extension_keys``: when True (default, used by ``load_config``),
    strip extension block keys so the base ``Config(extra='forbid')``
    doesn't reject them. Set to False when the caller needs to read
    extension blocks from the migrated data (``load_raven_config``).

    ``from_version``: the generation this config has already been through, which
    gates the run-once migrations individually. Defaults to the current mark, so
    a caller that cannot read the watermark runs none of them; only the caller
    holding the config path -- the one that can also write the mark back -- passes
    a real value. The shims below are idempotent and always run.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

    if from_version < _CONTEXT_WINDOW_MIGRATION:
        _migrate_legacy_context_window(data, notify=True)
    if from_version < _AUTO_PROVIDER_MIGRATION:
        _migrate_auto_provider(data, notify=True)

    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    # Relocate any legacy ``agents.defaults.{everos,everosSkillLight,
    # everos_skill_light}`` block to ``skillForge.everos`` (the current
    # home for the embedded extraction pipeline). The retired plain
    # ``agents.defaults.everos`` block from the EverOS-HTTP era is also
    # dropped — old configs may still carry it but the runtime no
    # longer accepts it under agents.defaults.
    agents = data.get("agents", {})
    defaults = agents.get("defaults") if isinstance(agents, dict) else None
    if isinstance(defaults, dict):
        legacy_esl = defaults.pop("everosSkillLight", None)
        if legacy_esl is None:
            legacy_esl = defaults.pop("everos_skill_light", None)
        dropped_everos = defaults.pop("everos", None)
        if dropped_everos is not None:
            _log.info("Migrated: dropped agents.defaults.everos (retired)")
        if legacy_esl is not None:
            # Strip retired everosSkillLight keys that EverOSConfig
            # (extra='forbid') no longer accepts; the per-turn gate is now
            # sourced from skill_forge.detect_min_tool_calls. snake_case and
            # camelCase both, since user configs may use either.
            for legacy_key in (
                "minMessages",
                "min_messages",
                "minToolCalls",
                "min_tool_calls",
            ):
                if legacy_key in legacy_esl:
                    legacy_esl.pop(legacy_key)
                    _log.info(
                        "Migrated: dropped everosSkillLight.%s (retired; use skill_forge.detect_min_tool_calls)",
                        legacy_key,
                    )
            if "skillForge" in data and isinstance(data["skillForge"], dict):
                sf_key = "skillForge"
            elif "skill_forge" in data and isinstance(data["skill_forge"], dict):
                sf_key = "skill_forge"
            else:
                sf_key = "skillForge"
                data[sf_key] = {}
            skill_forge = data[sf_key]
            if "everos" not in skill_forge:
                skill_forge["everos"] = legacy_esl
                _log.info(
                    "Migrated: agents.defaults.everosSkillLight → skillForge.everos",
                )

    # skills_dir → local_dirs migration now handled by
    # SkillForgeConfig._migrate_skills_dir model_validator (R5).

    # Strip retired sentinel keys that ``SentinelConfig(extra='forbid')``
    # would otherwise reject. Listed in both snake_case and camelCase
    # since user configs may use either.
    sentinel = data.get("sentinel") if isinstance(data, dict) else None
    if isinstance(sentinel, dict):
        for legacy_key in (
            "monitors",  # dropped: never had a reader
            "task_discovery_forward_channels",  # collapsed into task_discovery_targets
            "taskDiscoveryForwardChannels",
            "auto_enabled",  # retired sentinel.auto subsystem
            "autoEnabled",
        ):
            if legacy_key in sentinel:
                sentinel.pop(legacy_key)
                _log.info(
                    "Migrated: dropped sentinel.%s (retired field)",
                    legacy_key,
                )

    # Strip retired cron keys so stale config entries don't linger silently
    # (schema models default to extra='ignore', so they would load — this
    # strip exists for the one-time migration log, not to prevent a crash).
    # forward_channels died with trigger-time delivery routing
    # (fire-at-origin binds the target at creation).
    cron = data.get("cron") if isinstance(data, dict) else None
    if isinstance(cron, dict):
        for legacy_key in ("forward_channels", "forwardChannels"):
            if legacy_key in cron:
                cron.pop(legacy_key)
                _log.info(
                    "Migrated: dropped cron.%s (retired field)",
                    legacy_key,
                )

    # Nest the legacy top-level ``skillRouter`` / ``skill_router`` block
    # into ``skillForge.router`` — the router is now a SkillForge sub-block,
    # not a sibling top-level key. Explicit ``skillForge.router`` wins.
    router_block = data.pop("skillRouter", None)
    if router_block is None:
        router_block = data.pop("skill_router", None)
    if router_block is not None:
        if isinstance(data.get("skillForge"), dict):
            sf_key = "skillForge"
        elif isinstance(data.get("skill_forge"), dict):
            sf_key = "skill_forge"
        else:
            sf_key = "skillForge"
            data[sf_key] = {}
        sf = data[sf_key]
        if isinstance(sf, dict) and "router" not in sf:
            sf["router"] = router_block
            _log.info("Migrated: top-level skillRouter → skillForge.router")

    # Drop the retired ``mass`` source block from the router — the Skill
    # Hub source replaces it. (Removed field; would trip extra='forbid'.)
    for sf_key in ("skillForge", "skill_forge"):
        sf = data.get(sf_key)
        if isinstance(sf, dict) and isinstance(sf.get("router"), dict):
            if sf["router"].pop("mass", None) is not None:
                _log.info("Migrated: dropped skillForge.router.mass (retired; use skillForge.router.hub)")
            # ``hub.prefetch_bodies`` retired — body hydration moved into
            # SkillsSegmentBuilder (always-on for Hub hits the segment is
            # about to render), so the knob no longer has a reader.
            hub = sf["router"].get("hub")
            if isinstance(hub, dict):
                for legacy_key in ("prefetch_bodies", "prefetchBodies"):
                    if hub.pop(legacy_key, None) is not None:
                        _log.info(
                            "Migrated: dropped skillForge.router.hub.%s "
                            "(retired; SkillsSegmentBuilder always "
                            "hydrates Hub bodies)",
                            legacy_key,
                        )

    # Bring ``plugins.config["everos-memory"]`` onto the current shape: record
    # which EverOS root is in use and whether raven owns it, and drop the dead
    # ``mode`` key. Both new fields are decisions rather than derivable facts, so
    # leaving them absent would mean re-deriving them at every call site --
    # including the ones that decide whether writing to that root is allowed.
    #
    # Read-time normalisation only: this is a pure dict transform apart from one
    # existence check, and it must stay that way. Reading everos.toml or probing
    # a port here would put IO on every config load. The shape persists the next
    # time anything writes the config.
    plugins = data.get("plugins")
    if isinstance(plugins, dict):
        slice_ = (plugins.get("config") or {}).get("everos-memory")
        if isinstance(slice_, dict):
            if slice_.pop("mode", None) is not None:
                _log.info("Migrated: dropped plugins.config.everos-memory.mode (no reader)")
            from raven.config.update_everos import fallback_everos_root, root_is_raven_owned

            if "root" not in slice_:
                # Not ``everos_root()``: that re-reads whatever config path is
                # globally current, which is not necessarily the file being
                # migrated here.
                slice_["root"] = str(fallback_everos_root())
                _log.info("Migrated: recorded everos-memory.root = %s", slice_["root"])
            if "owned" not in slice_:
                slice_["owned"] = root_is_raven_owned(slice_["root"])

    # ── Pop extension keys before base Config validates ──────────────
    if pop_extension_keys:
        for ek in EXTENSION_KEYS:
            data.pop(ek, None)

    return data
