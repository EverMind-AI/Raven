"""``subagents.*`` RPC handlers: configure third-party sub-agents from the TUI.

Thin adapters only. The config write path, the preset templates, the probe and
the persisted test verdicts all already exist and are shared with the web RPC
(`raven/web_rpc/methods_config.py`); duplicating any of that logic here would
let the two surfaces disagree about what "installed" means or which fields a
write is allowed to touch.

The install group is computed here rather than in the client because the web UI
computes it client-side in `ui-webui/frontend/src/pages/subagent/catalog.ts`; a
third copy in the TUI is how the rule drifts.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, NoReturn

from pydantic import ValidationError

from raven.agent.subagent.presets import (
    THIRD_PARTY_SUBAGENT_PRESETS,
    third_party_subagent_preset,
    third_party_subagent_presets,
)
from raven.agent.subagent.probe import ProbeResult, probe_all, run_test
from raven.agent.subagent.test_state import TestStateStore
from raven.config.loader import get_config_path
from raven.config.schema import SubagentsConfig
from raven.config.update_subagents import (
    get_third_party_subagents,
    remove_third_party_subagent,
    set_third_party_subagents,
)
from raven.rpc.errors import ConfigValidationError, SubagentNotFoundError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


def _as_configs(entries: list[dict]) -> list[Any]:
    return list(SubagentsConfig(third_party=entries).third_party)


def _group(cfg: Any, probe_status: str) -> str:
    """Which install group a row belongs to.

    An openai entry is keyed off its api key, not the probe: the probe reports
    "api key not set or rejected" for both a missing key and a rejected one, and
    those are different groups needing different user action.
    """
    if getattr(cfg, "kind", None) == "openai":
        return "installed" if (getattr(cfg, "api_key", "") or "").strip() else "uninstalled"
    return "installed" if probe_status == "ready" else "uninstalled"


def _validation_detail(exc: ValidationError) -> str:
    """Build a message from each error's ``loc``/``type`` only.

    ``str(exc)`` embeds pydantic's ``input_value=...`` diagnostic, which for a
    whole-entry failure (e.g. a required field missing) is the *entire* offending
    dict - including a plaintext ``apiKey``. Never touch that; the field path and
    error type are enough to act on and carry no config values at all.
    """
    parts = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('type', 'invalid')}" if loc else err.get("type", "invalid"))
    return "; ".join(parts) or "invalid subagent configuration"


def _raise_config_error(exc: Exception) -> NoReturn:
    """Convert a config schema/write-path failure into -32011, never the original.

    Left uncaught, a ``ValueError`` (duplicate name) or pydantic ``ValidationError``
    (malformed entry) would fall through to the dispatcher's catch-all and come
    back as an opaque -32603 ``internal_error`` - and for ``ValidationError`` in
    particular, its ``str()`` can carry a secret (see ``_validation_detail``).
    """
    if isinstance(exc, ValidationError):
        raise ConfigValidationError(_validation_detail(exc)) from exc
    raise ConfigValidationError(str(exc)) from exc


def _clean_name(raw: str | None, *, field: str) -> str | None:
    """Trim a caller-supplied name; ``None`` means "no change" and passes through.

    A value that is blank after trimming is rejected rather than stored or
    silently dropped: stored as-is it would be advertised to the dispatching
    model as an agent nobody can address.
    """
    if raw is None:
        return None
    trimmed = raw.strip()
    if not trimmed:
        raise ConfigValidationError(f"{field} must not be blank", data={"field": field})
    return trimmed


async def _rows(*, probe: bool = True) -> list[dict]:
    """Every row the overlay shows: configured entries first, then presets that
    have no configured entry of their own.

    ``probe`` False skips the network availability check entirely: every result
    reports ``probe_status="unknown"``, which is enough to still resolve `group`
    correctly (an openai entry is grouped by its api key, not the probe; a cli
    entry without a "ready" probe is uninstalled) without the up-to-10s-per-entry
    round trip a mutation's follow-up list call has no reason to pay for.
    """
    try:
        configured_raw = get_third_party_subagents(config_path=get_config_path())
    except ValidationError as exc:
        _raise_config_error(exc)
    configured = _as_configs(configured_raw)
    claimed = {getattr(c, "preset", None) for c in configured}
    presets = [p for p in third_party_subagent_presets() if p.get("preset") not in claimed]
    preset_cfgs = _as_configs(presets)

    entries: list[tuple[Any, str]] = [(c, "config") for c in configured]
    entries += [(c, "preset") for c in preset_cfgs]

    verdicts = TestStateStore().load(entries)
    if probe:
        results = await probe_all(entries, verdicts=verdicts)
    else:
        results = [
            ProbeResult(cfg.name, source, cfg.kind, "unknown", "", "", 0, verdicts.get(f"{source}:{cfg.name}"))
            for cfg, source in entries
        ]

    rows: list[dict] = []
    for (cfg, source), result in zip(entries, results, strict=True):
        last = result.last_test
        task = _RUNNING.get(cfg.name)
        rows.append(
            {
                "name": cfg.name,
                "preset": getattr(cfg, "preset", None),
                "kind": cfg.kind,
                "description": getattr(cfg, "description", "") or "",
                "enabled": bool(getattr(cfg, "enabled", True)) if source == "config" else False,
                "configured": source == "config",
                "group": _group(cfg, result.status),
                "probe_status": result.status,
                "probe_detail": result.detail,
                "has_api_key": bool((getattr(cfg, "api_key", "") or "").strip()),
                "last_test_ok": None if last is None else last.ok,
                "last_test_detail": None if last is None else last.detail,
                "last_test_at_ms": None if last is None else last.tested_at_ms,
                "test_running": task is not None and not task.done(),
            }
        )
    return rows


async def subagents_list(params: dict) -> dict:
    """Every configured sub-agent plus every unconfigured preset, with status.

    ``probe`` (default ``True``) may be set ``False`` to skip the network
    availability check - the overlay does this on the list call it issues right
    after its own mutation, where a fresh probe would only re-measure what it
    already knows it just wrote.
    """
    probe = params.get("probe", True)
    return {"rows": await _rows(probe=bool(probe))}


async def subagents_probe(params: dict) -> dict:
    """Re-run the free availability probe. Same shape as ``subagents.list``."""
    return {"rows": await _rows()}


def _hot_apply(agent_loop_factory: "AgentLoopFactory | None") -> None:
    """Push the new roster into the live runtime.

    Skipped silently when there is no loop (the demo runner): the config write
    is the durable part, and refusing the whole call would make the TUI's own
    demo mode unable to configure anything.
    """
    if agent_loop_factory is None:
        return
    loop = agent_loop_factory()
    if loop is None or not hasattr(loop, "apply_third_party_subagents"):
        return
    try:
        entries = get_third_party_subagents(config_path=get_config_path())
    except ValidationError as exc:
        # The config is shared with other clients (web UI, hand edits): a
        # concurrent write between our own write and this re-read can leave a
        # malformed entry here even though this call's own mutation succeeded.
        _raise_config_error(exc)
    loop.apply_third_party_subagents(_as_configs(entries))


async def subagents_add(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Add a configured entry from a preset template.

    Only `name`, `description` and `api_key` come from the caller; every
    execution field (command, resumeCommand, idSource, transcriptFormat, ...)
    comes from the preset, which is already correct and version-verified.
    """
    preset_name = params.get("preset")
    if preset_name not in THIRD_PARTY_SUBAGENT_PRESETS:
        raise SubagentNotFoundError(
            f"unknown preset: {preset_name!r}",
            data={"preset": preset_name, "known": sorted(THIRD_PARTY_SUBAGENT_PRESETS)},
        )
    entry = third_party_subagent_preset(preset_name)
    name = _clean_name(params.get("name"), field="name")
    if name:
        entry["name"] = name
    if params.get("description"):
        entry["description"] = params["description"]
    if params.get("api_key") is not None:
        entry["apiKey"] = params["api_key"]
    # Every preset ships `enabled: true`, but an openai entry with no key cannot
    # answer: advertising it to the model would produce a sub-agent that fails on
    # first dispatch. Added disabled instead, so the user enables it once the key
    # is in. A cli preset is added enabled - the roster is how it becomes usable,
    # and its probe status is already shown in the row.
    if entry.get("kind") == "openai" and not (entry.get("apiKey") or "").strip():
        entry["enabled"] = False
    try:
        kept = list(get_third_party_subagents(config_path=get_config_path()))
        set_third_party_subagents([*kept, entry], config_path=get_config_path())
    except (ValueError, ValidationError) as exc:
        _raise_config_error(exc)
    _hot_apply(agent_loop_factory)
    return {"added": True, "name": entry["name"]}


def _default_description(preset_name: str | None) -> str:
    """The preset's shipped description, or "" when there is no preset to fall
    back to (a hand-written entry with no ``preset`` provenance)."""
    if preset_name and preset_name in THIRD_PARTY_SUBAGENT_PRESETS:
        return third_party_subagent_preset(preset_name)["description"]
    return ""


async def subagents_update(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Change only name / description / api key on an existing entry."""
    name = params.get("name")
    try:
        entries = get_third_party_subagents(config_path=get_config_path())
    except ValidationError as exc:
        _raise_config_error(exc)
    target = next((e for e in entries if e.get("name") == name), None)
    if target is None:
        raise SubagentNotFoundError(f"no configured sub-agent named {name!r}", data={"name": name})
    new_name = _clean_name(params.get("new_name"), field="new_name")
    if new_name:
        target["name"] = new_name
    if params.get("description") is not None:
        description = params["description"]
        # Blank/whitespace-only reverts to the preset default, same as add: a
        # cleared description would otherwise strip the agent's only description
        # from the `spawn` roster the dispatching model reads.
        target["description"] = description if description.strip() else _default_description(target.get("preset"))
    # Blank/absent means keep the stored key: the caller is never shown it, so
    # an empty field is "unchanged", never "clear it".
    if (params.get("api_key") or "").strip():
        target["apiKey"] = params["api_key"]
    try:
        set_third_party_subagents(entries, config_path=get_config_path())
    except (ValueError, ValidationError) as exc:
        _raise_config_error(exc)
    _hot_apply(agent_loop_factory)
    return {"updated": True, "name": target["name"]}


async def subagents_toggle(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Set `enabled` on one entry - the flag the roster filter reads."""
    name = params.get("name")
    enabled = bool(params.get("enabled"))
    try:
        entries = get_third_party_subagents(config_path=get_config_path())
    except ValidationError as exc:
        _raise_config_error(exc)
    target = next((e for e in entries if e.get("name") == name), None)
    if target is None:
        raise SubagentNotFoundError(f"no configured sub-agent named {name!r}", data={"name": name})
    target["enabled"] = enabled
    try:
        set_third_party_subagents(entries, config_path=get_config_path())
    except (ValueError, ValidationError) as exc:
        _raise_config_error(exc)
    _hot_apply(agent_loop_factory)
    return {"enabled": enabled}


async def subagents_remove(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Delete one entry. Reports `removed: false` for a name that was not there."""
    try:
        removed = remove_third_party_subagent(params.get("name", ""), config_path=get_config_path())
    except (ValueError, ValidationError) as exc:
        _raise_config_error(exc)
    if removed:
        _hot_apply(agent_loop_factory)
    return {"removed": removed}


# name -> the in-flight test task, so `subagents.test_cancel` can reach it.
# Cancelling the task is what kills the subprocess: `CliAgentBackend` catches
# `asyncio.CancelledError` and killpg's the whole process group
# (`backends/cli_agent.py:170`), and `run_test` catches only `Exception`, so the
# cancellation is not swallowed on the way out.
_RUNNING: dict[str, asyncio.Task] = {}


def _find(name: str, source: str) -> Any:
    """The config object a test should run against, by name and source.

    ``get_third_party_subagents`` re-validates the *whole* on-disk section, so a
    malformed entry anywhere in it (written by any client sharing this config,
    not necessarily this feature) would otherwise raise a bare ``ValidationError``
    out of a request to test one unrelated, perfectly healthy row.
    """
    try:
        pool = (
            third_party_subagent_presets()
            if source == "preset"
            else get_third_party_subagents(config_path=get_config_path())
        )
    except ValidationError as exc:
        _raise_config_error(exc)
    entry = next((e for e in pool if e.get("name") == name), None)
    if entry is None:
        raise SubagentNotFoundError(f"no {source} sub-agent named {name!r}", data={"name": name})
    return _as_configs([entry])[0]


async def subagents_test(params: dict) -> dict:
    """Dispatch the real agent once and report the verdict.

    This spends the agent's own quota, so it is only ever reached by an explicit
    request. Looked up by name against config or the presets - never by running a
    command supplied by the caller.
    """
    name = params.get("name", "")
    source = params.get("source", "config")

    # A second call for a name already running is refused rather than started: a
    # concurrent `_RUNNING[name] = task` would silently overwrite the first task's
    # entry, so whichever run finished first would pop the *other* run's entry out
    # from under it, leaving that survivor both undispatchable-a-verdict and
    # uncancellable for up to the full test timeout. Refusing keeps one running
    # test per name, which keeps `subagents.test_cancel` able to reach it.
    existing = _RUNNING.get(name)
    if existing is not None and not existing.done():
        return {
            "ok": False,
            "detail": f"a test is already running for {name!r}",
            "elapsed_ms": 0,
            "reply": None,
            "cancelled": False,
        }

    cfg = _find(name, source)

    task = asyncio.ensure_future(run_test(cfg, source=source))
    _RUNNING[name] = task
    try:
        result = await task
    except asyncio.CancelledError:
        # Cancelled through `subagents.test_cancel`: report it rather than
        # propagating, so the overlay gets a normal result to render. No verdict
        # is recorded - a cancelled run proves nothing either way.
        return {"ok": False, "detail": "test cancelled", "elapsed_ms": 0, "reply": None, "cancelled": True}
    finally:
        # Identity-checked: only remove this call's own entry, so a future
        # change to this map cannot resurrect the same class of bug where one
        # run's cleanup deletes a different run's still-live task.
        if _RUNNING.get(name) is task:
            _RUNNING.pop(name, None)

    TestStateStore().record(
        cfg,
        source,
        ok=result.ok,
        detail=result.detail,
        tested_at_ms=int(time.time() * 1000),
    )
    return {
        "ok": result.ok,
        "detail": result.detail,
        "elapsed_ms": result.elapsed_ms,
        "reply": result.reply,
        "cancelled": False,
    }


async def subagents_test_cancel(params: dict) -> dict:
    """Cancel an in-flight test, killing the agent's process group."""
    task = _RUNNING.get(params.get("name", ""))
    if task is None or task.done():
        return {"cancelled": False}
    task.cancel()
    return {"cancelled": True}


def register_subagents_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register the ``subagents.*`` methods on a dispatcher instance."""
    dispatcher.register("subagents.list", subagents_list)
    dispatcher.register("subagents.probe", subagents_probe)

    async def _add(params: dict) -> dict:
        return await subagents_add(params, agent_loop_factory=agent_loop_factory)

    async def _update(params: dict) -> dict:
        return await subagents_update(params, agent_loop_factory=agent_loop_factory)

    async def _toggle(params: dict) -> dict:
        return await subagents_toggle(params, agent_loop_factory=agent_loop_factory)

    async def _remove(params: dict) -> dict:
        return await subagents_remove(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("subagents.add", _add)
    dispatcher.register("subagents.update", _update)
    dispatcher.register("subagents.toggle", _toggle)
    dispatcher.register("subagents.remove", _remove)
    dispatcher.register("subagents.test", subagents_test)
    dispatcher.register("subagents.test_cancel", subagents_test_cancel)


__all__ = [
    "subagents_list",
    "subagents_probe",
    "subagents_add",
    "subagents_update",
    "subagents_toggle",
    "subagents_remove",
    "subagents_test",
    "subagents_test_cancel",
    "register_subagents_methods",
]
