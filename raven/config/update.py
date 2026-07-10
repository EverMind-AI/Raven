"""Minimal in-place updates for ~/.raven/config.json.

Unlike ``save_config`` which re-serializes the entire Pydantic model (and
would bake every runtime default back into the file), these helpers read
the raw JSON, patch a small set of fields, and atomically rewrite via
temp-file + rename. Used by ``raven cron config set`` and the
onboarding wizard so the change persists across restarts without
touching unrelated fields.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic.alias_generators import to_camel

from raven.config.loader import get_config_path
from raven.config.schema import CronConfig

# Shared Skill Hub endpoint seeded into a fresh config's skillForge.router.hub.
# Kept here (not as the HubSourceConfig schema default) so non-onboard /
# programmatic loads stay Hub-disabled until a config opts in, while an
# onboarded config shows the live endpoint. apiKey is NOT seeded — the user
# supplies their own Bearer token.
_DEFAULT_SKILL_HUB_ENDPOINT = "https://skillhub.evermind.ai"


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("config/update: failed to read {}: {}", path, exc)
        return {}


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def update_cron_config(
    key: str,
    value: Any,
    *,
    config_path: Path | None = None,
) -> Any:
    """Patch a single CronConfig field on-disk.

    Returns the previous raw value (None if absent). Raises ``KeyError`` if
    ``key`` is not a CronConfig field — defensive only; CLI ``_KEY_HANDLERS``
    already validates before reaching here. Type validation of ``value`` is
    the caller's responsibility (CLI parsers handle it).
    """
    if key not in CronConfig.model_fields:
        raise KeyError(f"Unknown cron config key: {key!r}. Supported: {sorted(CronConfig.model_fields)}")
    path = config_path or get_config_path()
    data = _load_raw(path)
    cron_section = data.setdefault("cron", {})
    camel_key = to_camel(key)
    prev = cron_section.get(camel_key)
    cron_section[camel_key] = value
    _write_atomic(path, data)
    logger.info("config/update: cron.{} set to {!r} (was {!r})", key, value, prev)
    return prev


def reset_cron_config(*, config_path: Path | None = None) -> None:
    """Remove the entire ``cron`` section from on-disk config.

    Schema defaults (``forward_channels=["*"]`` / ``default_timezone="Asia/Shanghai"``)
    take effect on next load. Stays consistent with the file's "never bake
    defaults to disk" principle.
    """
    path = config_path or get_config_path()
    data = _load_raw(path)
    removed = data.pop("cron", None)
    _write_atomic(path, data)
    logger.info("config/update: cron section reset (was {!r})", removed)


def set_sentinel_enabled(
    enabled: bool,
    *,
    config_path: Path | None = None,
) -> bool | None:
    """Patch ``sentinel.enabled`` on the on-disk config. Returns the previous
    raw value (None if absent).

    The Sentinel master switch is read once at process start
    (``build_sentinel_stack`` skips building the runner entirely when it is
    False), so this change takes effect on the next agent/gateway start, not
    on a running process.
    """
    path = config_path or get_config_path()
    data = _load_raw(path)
    section = data.setdefault("sentinel", {})
    prev = section.get("enabled")
    # No-op when already in the desired state (absent defaults to False) —
    # don't rewrite the file just to set the same value.
    if bool(prev) == enabled:
        return prev
    section["enabled"] = enabled
    _write_atomic(path, data)
    logger.info("config/update: sentinel.enabled set to {!r} (was {!r})", enabled, prev)
    return prev


def set_sentinel_nudge_quota(
    *,
    per_hour: int | None = None,
    per_day: int | None = None,
    config_path: Path | None = None,
) -> dict[str, tuple[Any, int]]:
    """Patch ``sentinel.nudge_policy`` per-hour / per-day nudge quotas on-disk.

    Returns ``{field: (prev, new)}`` for each field changed. Effective on the
    next NudgePolicy load (agent/gateway start). Respects whichever key casing
    (camelCase / snake_case) the file already uses — the loader accepts both,
    but writing a second casing for a field already present would duplicate it.
    """
    if per_hour is None and per_day is None:
        raise ValueError("specify at least one of per_hour / per_day")
    for label, val in (("per_hour", per_hour), ("per_day", per_day)):
        if val is not None and val < 1:
            raise ValueError(f"{label} must be >= 1 (got {val})")

    path = config_path or get_config_path()
    data = _load_raw(path)
    sentinel = data.setdefault("sentinel", {})
    np_key = "nudge_policy" if "nudge_policy" in sentinel else "nudgePolicy"
    np = sentinel.setdefault(np_key, {})
    snake_block = np_key == "nudge_policy"

    def _patch(camel: str, snake: str, value: int, changed: dict) -> None:
        # Reuse an existing key as-is; for a new field follow the block's
        # casing convention so we never mix snake + camel within one block.
        if snake in np:
            key = snake
        elif camel in np:
            key = camel
        else:
            key = snake if snake_block else camel
        prev = np.get(key)
        if prev == value:
            return  # already at the target — leave it out of `changed`
        np[key] = value
        changed[snake] = (prev, value)

    changed: dict[str, tuple[Any, int]] = {}
    if per_hour is not None:
        _patch("maxNudgesPerHour", "max_nudges_per_hour", per_hour, changed)
    if per_day is not None:
        _patch("maxNudgesPerDay", "max_nudges_per_day", per_day, changed)

    # Only touch the file when something actually changed.
    if changed:
        _write_atomic(path, data)
        logger.info("config/update: sentinel nudge quota patched {!r}", changed)
    return changed


def set_language(
    language: str,
    *,
    config_path: Path | None = None,
) -> str | None:
    """Patch the top-level ``language`` on the on-disk config. Returns previous value.

    Set by the onboarding wizard's language screen. Read by the CLI/wizard copy
    (via ``_t``) and injected into the agent's system prompt so replies use the
    chosen language.
    """
    path = config_path or get_config_path()
    data = _load_raw(path)
    prev = data.get("language")
    data["language"] = language
    _write_atomic(path, data)
    logger.info("config/update: language set to {!r} (was {!r})", language, prev)
    return prev


def set_default_model(
    model: str,
    *,
    config_path: Path | None = None,
) -> str | None:
    """Patch ``agents.defaults.model`` on the on-disk config. Returns previous value.

    Used by the onboarding wizard after the user picks a provider: the wizard
    needs to swap the default model to one that matches the chosen provider
    (otherwise ``raven agent`` would still route to whatever the freshly
    created ``Config()`` baked in, which is typically a different vendor).
    """
    path = config_path or get_config_path()
    data = _load_raw(path)
    defaults = data.setdefault("agents", {}).setdefault("defaults", {})
    prev = defaults.get("model")
    defaults["model"] = model
    _write_atomic(path, data)
    logger.info("config/update: default model set to {} (was {})", model, prev)
    return prev


def set_sandbox_backend(
    backend: str,
    *,
    config_path: Path | None = None,
) -> str | None:
    """Patch ``sandbox.backend`` on the on-disk config. Returns previous value.

    Used by the onboarding wizard's run-location step. ``backend`` must be one
    of ``SandboxConfig``'s literal values (``none`` / ``auto`` / ``boxlite``);
    the loader validates on next read.
    """
    path = config_path or get_config_path()
    data = _load_raw(path)
    # sandbox lives under tools (Config.tools.sandbox), not at the root — the
    # root Config forbids extras, so a top-level "sandbox" key fails schema
    # validation on the next load.
    section = data.setdefault("tools", {}).setdefault("sandbox", {})
    prev = section.get("backend")
    section["backend"] = backend
    _write_atomic(path, data)
    logger.info("config/update: tools.sandbox.backend set to {!r} (was {!r})", backend, prev)
    return prev


def init_extension_block_defaults(*, config_path: Path | None = None) -> None:
    """Seed the user-facing subset of the memory / plugins / skillForge
    extension blocks into a fresh ``~/.raven/config.json``.

    Called once by the onboarding bootstrap so a new config shows these knobs
    at their schema defaults — discoverable and editable without reading the
    source. Each field is only written when absent (``setdefault``), so this is
    idempotent and never clobbers a value the user (or an earlier wizard step)
    already set. ``memory.backend`` is seeded to its schema default
    (``"everos"``); a fresh install with no EverOS models configured degrades
    gracefully (empty recall + a warning, never a crash), and the wizard's
    Step 4 / skip-guard resolve it back to ``None`` when memory is opted out or
    left unconfigured.

    Defaults are pulled from the Pydantic models so this seed can't drift from
    the schema, with three deliberate onboard-time overrides:
      - ``skillForge.everos.enabled`` is seeded ``True`` (per-turn extraction on
        for a fresh install) even though the schema default is conservative-off;
      - ``skillForge.router.hub.endpoint`` is seeded to the live Skill Hub URL
        (the schema default is ``None`` so programmatic loads stay Hub-off);
        ``apiKey`` is left null for the user to fill with their own token;
      - ``plugins.config["everos-memory"]`` is seeded with the plugin's identity
        wiring so the block is never empty and the user can see/edit it.

    The optional service fields on ``SkillForgeConfig`` (``embedding_url`` /
    ``embedding_api_key`` / ``reranker_url`` / ``reranker_api_key`` /
    ``mass_library_db``) are deliberately NOT written. They stay at public
    schema defaults and deployments that need hosted services add explicit
    values by hand.

    Key casing follows each block's convention: ``memory`` / ``skillForge`` use
    camelCase (the file-level alias); ``plugins.config`` is a verbatim
    pass-through dict whose keys stay snake_case (each plugin owns its schema).
    """
    from raven.config.raven import (
        MemoryConfig,
        PluginsConfig,
        SkillForgeRouterConfig,
    )

    path = config_path or get_config_path()
    data = _load_raw(path)

    mem = MemoryConfig()
    memory = data.setdefault("memory", {})
    memory.setdefault("backend", mem.backend)
    memory.setdefault("userId", mem.user_id)
    memory.setdefault("agentId", mem.agent_id)
    memory.setdefault("memoryTopK", mem.memory_top_k)

    plugins = data.setdefault("plugins", {})
    plugins.setdefault("disabled", list(PluginsConfig().disabled))
    # snake_case keys: plugins.config is handed to the plugin factory verbatim.
    # user_id / agent_id mirror memory.* so the recall identities match (the
    # backend stamps these onto stored messages; a mismatch makes memory
    # unretrievable — see MemoryConfig docstring).
    plugins.setdefault("config", {}).setdefault(
        "everos-memory",
        {
            "mode": "embedded",
            "base_url": "http://localhost:1995",
            "user_id": mem.user_id,
            "agent_id": mem.agent_id,
        },
    )

    router_defaults = SkillForgeRouterConfig()
    skill_forge = data.setdefault("skillForge", {})
    skill_forge.setdefault("enabled", True)
    # Onboard turns per-turn extraction ON (schema default is off for
    # non-onboard programmatic use).
    skill_forge.setdefault("everos", {}).setdefault("enabled", True)
    router = skill_forge.setdefault("router", {})
    router.setdefault("enabled", router_defaults.enabled)
    router.setdefault("weights", dict(router_defaults.weights))
    hub = router.setdefault("hub", {})
    # Default the Hub source ON, pointed at the shared Skill Hub. apiKey stays
    # null — the user fills in their own Bearer token; a baked placeholder would
    # be sent verbatim as auth. timeoutS / minSafety surface the tunable knobs.
    hub.setdefault("endpoint", _DEFAULT_SKILL_HUB_ENDPOINT)
    hub.setdefault("apiKey", router_defaults.hub.api_key)
    hub.setdefault("timeoutS", router_defaults.hub.timeout_s)
    hub.setdefault("minSafety", router_defaults.hub.min_safety)

    _write_atomic(path, data)
    logger.info("config/update: seeded memory/plugins/skillForge extension defaults")


def set_memory_backend(
    backend: str | None,
    *,
    config_path: Path | None = None,
) -> str | None:
    """Patch ``memory.backend`` on the on-disk config. Returns previous value.

    ``"everos"`` enables the EverOS backend; ``None`` disables backend-driven
    memory (falls back to the native Markdown store). The onboarding wizard's
    memory step writes the model sections to ``~/.everos/raven/everos.toml``
    and flips this flag here.
    """
    path = config_path or get_config_path()
    data = _load_raw(path)
    section = data.setdefault("memory", {})
    prev = section.get("backend")
    section["backend"] = backend
    _write_atomic(path, data)
    logger.info("config/update: memory.backend set to {!r} (was {!r})", backend, prev)
    return prev


__all__ = [
    "update_cron_config",
    "reset_cron_config",
    "set_sentinel_enabled",
    "set_sentinel_nudge_quota",
    "set_default_model",
    "set_sandbox_backend",
    "set_memory_backend",
    "init_extension_block_defaults",
]
