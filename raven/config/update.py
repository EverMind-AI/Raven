"""Minimal in-place updates for ~/.raven/config.json.

Unlike ``save_config`` which re-serializes the entire Pydantic model (and
would bake every runtime default back into the file), these helpers read
the raw JSON, patch a small set of fields, and rewrite through the locked
read-modify-write transaction in ``raven.utils.atomic_io``. Used by
``raven cron config set`` and the onboarding wizard so the change persists
across restarts without touching unrelated fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic.alias_generators import to_camel

from raven.config.loader import get_config_path, read_raw_or_raise
from raven.config.schema import CronConfig
from raven.utils.atomic_io import atomic_update

# Shared Skill Hub endpoint seeded into a fresh config's skillForge.router.hub.
# Kept here (not as the HubSourceConfig schema default) so non-onboard /
# programmatic loads stay Hub-disabled until a config opts in, while an
# onboarded config shows the live endpoint. apiKey is NOT seeded — the user
# supplies their own Bearer token.
_DEFAULT_SKILL_HUB_ENDPOINT = "https://skillhub.evermind.ai"


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

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        cron_section = data.setdefault("cron", {})
        camel_key = to_camel(key)
        prev = cron_section.get(camel_key)
        cron_section[camel_key] = value
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
    logger.info("config/update: cron.{} set to {!r} (was {!r})", key, value, prev)
    return prev


def reset_cron_config(*, config_path: Path | None = None) -> None:
    """Remove the entire ``cron`` section from on-disk config.

    Schema defaults (``default_timezone="Asia/Shanghai"``) take effect on
    next load. Stays consistent with the file's "never bake defaults to
    disk" principle.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        removed = data.pop("cron", None)
        return json.dumps(data, indent=2, ensure_ascii=False), removed

    removed = atomic_update(path, _apply)
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

    def _apply(_text: str | None) -> tuple[str | None, tuple[Any, bool]]:
        data = read_raw_or_raise(path)
        section = data.setdefault("sentinel", {})
        prev = section.get("enabled")
        # No-op when already in the desired state (absent defaults to False) —
        # don't rewrite the file just to set the same value.
        if bool(prev) == enabled:
            return None, (prev, False)
        section["enabled"] = enabled
        return json.dumps(data, indent=2, ensure_ascii=False), (prev, True)

    prev, wrote = atomic_update(path, _apply)
    if wrote:
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

    def _apply(_text: str | None) -> tuple[str | None, dict[str, tuple[Any, int]]]:
        data = read_raw_or_raise(path)
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
        if not changed:
            return None, changed
        return json.dumps(data, indent=2, ensure_ascii=False), changed

    changed = atomic_update(path, _apply)
    if changed:
        logger.info("config/update: sentinel nudge quota patched {!r}", changed)
    return changed


def set_skill_blocked(
    name: str,
    blocked: bool,
    *,
    config_path: Path | None = None,
) -> list[str]:
    """Add/remove a skill name on ``skillForge.blocklist``; returns the new
    list. Matching is case-insensitive; adding an already-listed name or
    removing an absent one is a no-op (the file is still not rewritten).

    The blocklist is read at process start (AgentLoop / context engine
    construction), so a change takes effect on the next agent/gateway
    start, not on a running process.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str | None, tuple[list[str], bool]]:
        data = read_raw_or_raise(path)
        section = data.setdefault("skillForge", {})
        current = [str(x) for x in (section.get("blocklist") or [])]
        lowered = {x.casefold() for x in current}
        if blocked:
            if name.casefold() in lowered:
                return None, (current, False)
            current.append(name)
        else:
            if name.casefold() not in lowered:
                return None, (current, False)
            current = [x for x in current if x.casefold() != name.casefold()]
        section["blocklist"] = current
        return json.dumps(data, indent=2, ensure_ascii=False), (current, True)

    current, wrote = atomic_update(path, _apply)
    if wrote:
        logger.info(
            "config/update: skillForge.blocklist now {!r} ({} {!r})",
            current,
            "blocked" if blocked else "unblocked",
            name,
        )
    return current


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

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        prev = data.get("language")
        data["language"] = language
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
    logger.info("config/update: language set to {!r} (was {!r})", language, prev)
    return prev


def set_default_model(
    model: str,
    *,
    provider: str | None = None,
    config_path: Path | None = None,
) -> str | None:
    """Patch ``agents.defaults.model`` on the on-disk config. Returns previous value.

    Used by the onboarding wizard after the user picks a provider: the wizard
    needs to swap the default model to one that matches the chosen provider
    (otherwise ``raven agent`` would still route to whatever the freshly
    created ``Config()`` baked in, which is typically a different vendor).

    ``provider`` writes ``agents.defaults.provider`` in the same patch. That field
    overrides what a model id says, so leaving it behind lets a stale pin route
    the new model to the old vendor -- with the old vendor's key -- while the
    write that was just reported as successful changes nothing. Callers that do
    not know which provider serves the model pass None and leave it alone.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        defaults = data.setdefault("agents", {}).setdefault("defaults", {})
        prev = defaults.get("model")
        defaults["model"] = model
        if provider is not None:
            defaults["provider"] = provider
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
    logger.info("config/update: default model set to {} (was {}), provider={}", model, prev, provider)
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

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        # sandbox lives under tools (Config.tools.sandbox), not at the root — the
        # root Config forbids extras, so a top-level "sandbox" key fails schema
        # validation on the next load.
        section = data.setdefault("tools", {}).setdefault("sandbox", {})
        prev = section.get("backend")
        section["backend"] = backend
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
    logger.info("config/update: tools.sandbox.backend set to {!r} (was {!r})", backend, prev)
    return prev


def set_plugin_config_fields(
    plugin_id: str,
    fields: dict[str, Any],
    *,
    remove: tuple[str, ...] | None = None,
    config_path: Path | None = None,
) -> None:
    """Merge ``fields`` into ``plugins.config[plugin_id]`` on the on-disk config.

    A merge rather than a replace: the slice holds several independent decisions
    (which EverOS root, whether raven owns it, its cached address) written at
    different moments, and a replacing write would drop whichever the caller did
    not happen to be carrying.

    ``remove`` names keys that no longer apply, for the case a merge cannot
    express. Switching to an EverOS the user runs has to retract the recorded
    root, not merely stop updating it: left behind, it is still exported as
    ``EVEROS_ROOT`` and still points raven at a directory it has just promised
    to leave alone.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, None]:
        data = read_raw_or_raise(path)
        slice_ = data.setdefault("plugins", {}).setdefault("config", {}).setdefault(plugin_id, {})
        slice_.update(fields)
        for key in remove or ():
            slice_.pop(key, None)
        return json.dumps(data, indent=2, ensure_ascii=False), None

    atomic_update(path, _apply)
    logger.info(
        "config/update: plugins.config.{} updated ({}{})",
        plugin_id,
        ", ".join(fields),
        f"; removed {', '.join(remove)}" if remove else "",
    )


def set_playbook_disabled(
    name: str,
    disabled: bool,
    *,
    config_path: Path | None = None,
) -> bool:
    """Add/remove one playbook name on the ``playbooks.disabled`` deny list.

    Returns True when the file changed (False = already in the desired
    state). The list is the only per-machine playbook state: playbook.md is
    the distribution unit and carries no switch, so disable adds the name
    here and enable removes it — for builtin and user playbooks alike. The
    runtime reads the list on every model call (``config.live``), so a change
    applies to the next one rather than to the next process.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str | None, bool]:
        data = read_raw_or_raise(path)
        section = data.setdefault("playbooks", {})
        deny = list(section.get("disabled") or [])
        if disabled == (name in deny):
            return None, False
        section["disabled"] = sorted(set(deny) | {name}) if disabled else [n for n in deny if n != name]
        return json.dumps(data, indent=2, ensure_ascii=False), True

    wrote = atomic_update(path, _apply)
    if wrote:
        logger.info("config/update: playbooks.disabled {} {!r}", "added" if disabled else "removed", name)
    return wrote


def set_memory_backend(
    backend: str | None,
    *,
    config_path: Path | None = None,
) -> str | None:
    """Patch ``memory.backend`` on the on-disk config. Returns previous value.

    ``"everos"`` enables the EverOS backend; ``None`` disables backend-driven
    memory entirely -- there is no second backend to fall back to, so recall and
    storage simply stop happening. The onboarding wizard's
    memory step writes the model sections to ``~/.everos/raven/everos.toml``
    and flips this flag here.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        section = data.setdefault("memory", {})
        prev = section.get("backend")
        section["backend"] = backend
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
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
    "set_skill_blocked",
    "set_playbook_disabled",
]
