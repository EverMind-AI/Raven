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
        raise KeyError(
            f"Unknown cron config key: {key!r}. "
            f"Supported: {sorted(CronConfig.model_fields)}"
        )
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


def set_memory_backend(
    backend: str | None,
    *,
    config_path: Path | None = None,
) -> str | None:
    """Patch ``memory.backend`` on the on-disk config. Returns previous value.

    ``"everos"`` enables the EverOS backend; ``None`` disables backend-driven
    memory (falls back to the native Markdown store). The onboarding wizard's
    memory step writes the model sections to ``~/.everos/config.toml`` and
    flips this flag here.
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
]
