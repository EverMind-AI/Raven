"""Configuration loading utilities."""

import json
from pathlib import Path

from pydantic import ValidationError

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
            data = _migrate_config(data)
        except json.JSONDecodeError as e:
            # Corrupted JSON (e.g. mid-write race): tolerate and fall
            # back to defaults so a transient bad-read doesn't brick
            # callers. Schema mismatches below are NOT tolerated.
            print(f"Warning: corrupted JSON in {path}: {e}")
            print("Using default configuration.")
        else:
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
