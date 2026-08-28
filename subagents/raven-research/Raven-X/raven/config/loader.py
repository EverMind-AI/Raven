"""Configuration loading utilities."""

import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from loguru import logger
from pydantic import BaseModel, ValidationError

from raven.config.schema import Config

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
    # Deep-research flow (raven.agent.flow).
    "drFlow",
    "dr_flow",
)

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


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


def _collect_unknown_keys(value: Any, annotation: Any, prefix: str) -> list[str]:
    """Dotted paths under ``prefix`` that no schema field will consume.

    The base ``Config`` family ignores unknown keys (pydantic default), so a
    misspelled field silently falls back to its default — which has already
    cost a real A/B run its iteration-budget parity (``maxIterations`` vs
    ``maxToolIterations``). This walk makes the typo visible at load time
    without rejecting old configs. Both the snake_case field name and its
    alias are accepted, mirroring ``populate_by_name``. Union branches only
    flag a key unknown to every checkable branch; non-model annotations end
    the walk (free-form dicts stay free-form).
    """
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        branches = [_collect_unknown_keys(value, arg, prefix) for arg in get_args(annotation)]
        common = set(branches[0])
        for b in branches[1:]:
            common &= set(b)
        return sorted(common)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel) and isinstance(value, dict):
        if annotation.model_config.get("extra") == "allow":
            return []
        known: dict[str, Any] = {}
        for fname, finfo in annotation.model_fields.items():
            known[fname] = finfo.annotation
            for alias in (finfo.alias, finfo.validation_alias):
                if isinstance(alias, str):
                    known[alias] = finfo.annotation
        unknown: list[str] = []
        for key, val in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if key not in known:
                unknown.append(path)
            else:
                unknown.extend(_collect_unknown_keys(val, known[key], path))
        return unknown
    if origin is dict and isinstance(value, dict):
        args = get_args(annotation)
        if len(args) == 2:
            out: list[str] = []
            for key, val in value.items():
                out.extend(_collect_unknown_keys(val, args[1], f"{prefix}.{key}" if prefix else key))
            return out
    if origin is list and isinstance(value, list):
        args = get_args(annotation)
        if args:
            out = []
            for i, val in enumerate(value):
                out.extend(_collect_unknown_keys(val, args[0], f"{prefix}[{i}]"))
            return out
    return []


def warn_unknown_config_keys(data: dict[str, Any], model_cls: type[BaseModel] = Config) -> list[str]:
    """Log one warning listing config keys the schema will silently drop."""
    unknown = _collect_unknown_keys(data, model_cls, "")
    if unknown:
        logger.warning(
            "config: unknown key(s) ignored by schema (check spelling against raven/config): {}",
            ", ".join(unknown),
        )
    return unknown


# Paths already seeded, so a process that loads its config repeatedly parses the
# file once. Re-seeding would also undo a deliberate `del os.environ[...]` made
# after the first load.
_DOTENV_SEEDED: set[Path] = set()

DOTENV_DISABLE_VAR = "RAVEN_DOTENV"


def seed_env_from_dotenv(config_path: Path) -> list[str]:
    """Merge ``KEY=VALUE`` lines from the config's own ``.env`` into the process.

    Seeding only. It adds NO way to resolve a setting: the tools still read
    ``config value or environment variable`` and nothing else, so a value still
    has one resolution rule however it arrived. Substituting ``${VAR}`` inside
    the config file would have been the second mechanism, operating on text that
    ``_migrate_config`` and ``warn_unknown_config_keys`` have already walked.

    An exported value always beats the file, or a stale ``.env`` would silently
    override a key given on the command line for one run. Exported-but-EMPTY
    does not count as a value and is filled in, the same reading of "set" that
    ``scripts/everos_dr_smoke.py:load_env_file`` and the raven-research
    launcher's ``env_value`` already use.

    Anchored on the config file's own directory, which is where this runtime
    already puts ``sessions/``, ``cache/`` and ``ledger/``: one anchor, one rule.
    Reading the working directory instead would make the same command pick up a
    different ``.env`` depending on where it was launched from.
    """
    env_file = config_path.parent / ".env"
    if env_file in _DOTENV_SEEDED:
        return []
    _DOTENV_SEEDED.add(env_file)
    try:
        text = env_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if env_file.stat().st_mode & 0o077:
        logger.warning(
            "config: {} is readable by other users; it holds credentials, so chmod 600 it",
            env_file,
        )
    added: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and not os.environ.get(key):
            os.environ[key] = value
            added.append(key)
    if added:
        # Names only. The values are credentials.
        logger.info("config: seeded {} from {}", ", ".join(added), env_file)
    return added


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()
    # Before the file is read, because the keys it carries are resolved by the
    # tools at call time from the environment when the config does not hold
    # them. Off inside the test suite (see tests/conftest.py): this writes
    # process-global state, and a suite whose result depends on a file in the
    # developer's home cannot answer "is this branch green".
    if os.environ.get(DOTENV_DISABLE_VAR) != "0":
        seed_env_from_dotenv(path)

    config: Config | None = None
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
        except json.JSONDecodeError as e:
            # Boot on defaults for a malformed file (a transient mid-write race
            # shouldn't brick callers) but warn LOUDLY -- a persistent syntax
            # error would else revert every setting with no visible cause.
            # Raising instead needs atomic save_config first (separate change).
            msg = (
                f"config at {path} is not valid JSON ({e}) -- IGNORING it and running on "
                "DEFAULTS. Fix the file (JSON allows no comments or trailing commas) and restart."
            )
            print(f"WARNING: {msg}", file=sys.stderr)
            logger.warning(msg)
        else:
            warn_unknown_config_keys(data)
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

    if config is None:
        config = Config()

    return config


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict, *, pop_extension_keys: bool = True) -> dict:
    """Migrate old config formats to current.

    ``pop_extension_keys``: when True (default, used by ``load_config``),
    strip extension block keys so the base ``Config(extra='forbid')``
    doesn't reject them. Set to False when the caller needs to read
    extension blocks from the migrated data (``load_raven_config``).
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

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

    # Web credentials moved from the tool that used them to the vendor that
    # issues them: ``tools.web.search.apiKey`` → ``tools.web.providers.serper``,
    # ``tools.web.jinaApiKey`` → ``tools.web.providers.jina``, and the same for
    # the per-tool AnySearch/SerpApi fields. One AnySearch account serves both
    # web_search and web_fetch, so a per-tool key had to be pasted twice and
    # could drift into two values for one credential.
    #
    # The host raven this agent inherits from is a separate checkout on the old
    # layout, so its keys arrive in the legacy shape indefinitely; this is not a
    # one-off upgrade path that can eventually be deleted.
    web = (data.get("tools") or {}).get("web") if isinstance(data.get("tools"), dict) else None
    if isinstance(web, dict):
        search = web.get("search") if isinstance(web.get("search"), dict) else {}
        legacy_web_keys = (
            ("jinaApiKey", web, "jina"),
            ("jina_api_key", web, "jina"),
            ("apiKey", search, "serper"),
            ("api_key", search, "serper"),
            ("anysearchApiKey", search, "anysearch"),
            ("anysearch_api_key", search, "anysearch"),
            ("serpapiApiKey", search, "serpapi"),
            ("serpapi_api_key", search, "serpapi"),
        )
        for legacy_key, holder, vendor in legacy_web_keys:
            value = holder.pop(legacy_key, None)
            if not value:
                continue
            providers = web.setdefault("providers", {})
            if not isinstance(providers, dict):
                continue
            vendor_block = providers.setdefault(vendor, {})
            # An explicit new-shape value wins: a config carrying both is being
            # migrated by hand, and the new path is the one its author meant.
            if isinstance(vendor_block, dict) and not (
                vendor_block.get("apiKey") or vendor_block.get("api_key")
            ):
                vendor_block["apiKey"] = value
                _log.info("Migrated: tools.web...%s -> tools.web.providers.%s.apiKey", legacy_key, vendor)

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

    # ── Pop extension keys before base Config validates ──────────────
    if pop_extension_keys:
        for ek in EXTENSION_KEYS:
            data.pop(ek, None)

    return data
