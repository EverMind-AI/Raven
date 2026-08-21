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
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from loguru import logger
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
    get_agents,
    remove_agent,
    set_agents,
)
from raven.rpc.errors import ConfigValidationError, SubagentNotFoundError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


def _as_configs(entries: list[dict]) -> list[Any]:
    return list(SubagentsConfig(agents=entries).agents)


def _upgrade_transport(cfg: Any, source: str) -> str | None:
    """The transport this entry's preset moved to, or ``None`` when it is current.

    A preset fixes one transport per agent, and that choice can change in a
    release: ``codex`` used to be reached by shelling out to its CLI and is now
    reached over ACP. A configured entry is never rewritten underneath the user --
    its command would change and every session handle bound to it would stop
    meaning anything -- so the mismatch is surfaced here and acted on by hand.

    Only for configured rows: a preset row *is* the current transport.
    """
    if source != "config":
        return None
    preset_name = getattr(cfg, "preset", None)
    preset = THIRD_PARTY_SUBAGENT_PRESETS.get(preset_name) if preset_name else None
    if preset is None:
        return None
    preset_kind = preset.get("kind")
    return preset_kind if preset_kind != getattr(cfg, "kind", None) else None


def _group(cfg: Any, probe_status: str) -> str:
    """Which install group a row belongs to.

    An openai entry is keyed off its api key, not the probe: the probe reports
    "api key not set or rejected" for both a missing key and a rejected one, and
    those are different groups needing different user action.

    A built-in row gets a group of its own rather than being sorted into
    installed/uninstalled. There is nothing to install: it is raven's own loop,
    always available, and it has no command to check -- so a row that could only
    ever read "uninstalled" (never probed, so never "ready") would be telling the
    user to go and install something that is already running.
    """
    if getattr(cfg, "kind", None) == "builtin":
        return "builtin"
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
        configured_raw = get_agents(config_path=get_config_path())
    except ValidationError as exc:
        _raise_config_error(exc)
    configured = _as_configs(configured_raw)
    claimed = {getattr(c, "preset", None) for c in configured}
    presets = [p for p in third_party_subagent_presets() if p.get("preset") not in claimed]
    preset_cfgs = _as_configs(presets)

    # The built-in rows, merged the way the runtime merges them, so this list shows
    # the same table the model dispatches against -- including a user's override of
    # one. They are reported as ``source == "builtin"`` rather than "config",
    # because "configured" drives the delete button and a seed row cannot be
    # deleted: not writing one is what "use the default" means.
    from raven.agent.subagent.builtin_agents import merge_builtin_seeds
    from raven.agent.subagent.vendored_agents import (
        discover_vendored_rows,
        merge_vendored_seeds,
        vendored_state,
    )

    # Composed exactly the way ``AgentRegistry.apply`` composes it, and for the
    # same reason the registry exists at all: this list is what a human reads to
    # answer "what can the model dispatch to", so a view assembled from a
    # different subset of the sources is a view that disagrees with the runtime.
    # It did: the vendored rows were on the table and absent here, which reads as
    # "the four agents did not install" on the one screen built to tell you they
    # had.
    vendored = discover_vendored_rows()
    vendored_names = {getattr(c, "name", "") for c in vendored}
    unready = vendored_state()
    merged = merge_vendored_seeds(configured, vendored)

    builtin_cfgs = [c for c in merge_builtin_seeds(merged) if getattr(c, "kind", None) == "builtin"]
    external = [c for c in merged if getattr(c, "kind", None) != "builtin"]
    configured_names = {getattr(c, "name", "") for c in configured}

    entries: list[tuple[Any, str]] = [(c, "builtin") for c in builtin_cfgs]
    # A discovered row is reported as its own source, not as "config": there is no
    # config entry to delete, and the delete button reads ``configured``. Removing
    # one means removing its folder. A row the user has also written by hand is
    # theirs, so it keeps the config source and its delete button.
    entries += [
        (c, "config" if getattr(c, "name", "") in configured_names else "vendored")
        if getattr(c, "name", "") in vendored_names
        else (c, "config")
        for c in external
    ]
    entries += [(c, "preset") for c in preset_cfgs]

    verdicts = TestStateStore().load(entries)
    # Built-in rows are excluded from the probe: there is no command to launch and
    # no endpoint to reach, so probing one would spend its per-entry budget to
    # learn nothing. They report ``unknown`` and are grouped by kind instead.
    probeable = [(cfg, source) for cfg, source in entries if source != "builtin"]
    if probe and probeable:
        probed = dict(
            zip(
                [(cfg.name, source) for cfg, source in probeable],
                await probe_all(probeable, verdicts=verdicts),
                strict=True,
            )
        )
        results = [
            probed.get(
                (cfg.name, source),
                ProbeResult(cfg.name, source, cfg.kind, "unknown", "", "", 0, verdicts.get(f"{source}:{cfg.name}")),
            )
            for cfg, source in entries
        ]
    else:
        results = [
            ProbeResult(cfg.name, source, cfg.kind, "unknown", "", "", 0, verdicts.get(f"{source}:{cfg.name}"))
            for cfg, source in entries
        ]

    rows: list[dict] = []
    for (cfg, source), result in zip(entries, results, strict=True):
        # The probe launches nothing: for a cli row it checks the command's first
        # token, which for one of these is the interpreter and always exists. So a
        # folder with no venv probed "ready" and this page called it installed
        # while the roster -- correctly -- refused to advertise it. The readiness
        # verdict overrides the probe for a vendored row, and carries the reason,
        # because "not ready" without "why" sends the reader looking.
        building = (task := _BUILDING.get(cfg.name)) is not None and not task.done()
        if source == "vendored" and (verdict := unready.get(cfg.name)) and not verdict.ready:
            # A build in flight outranks the reason it is fixing: leaving "venv not
            # built -- run install.sh" on screen for the minutes a build takes
            # reads as nothing having happened.
            # And a reason the installer cannot fix is `attention`, not `missing`:
            # `missing` is what the page reads as "offer Install", and offering it
            # for a folder whose venv is already built and whose problem is a
            # credential is offering a button that returns in seconds having
            # changed nothing. `attention` renders the reason instead, which is
            # what that folder needs a human to read.
            detail = _BUILD_ERROR.get(cfg.name) or verdict.detail
            missing = verdict.buildable and not building
            result = replace(result, status="missing" if missing else "attention", detail="" if building else detail)
        last = result.last_test
        task = _RUNNING.get(cfg.name)
        rows.append(
            {
                "name": cfg.name,
                "preset": getattr(cfg, "preset", None),
                "kind": cfg.kind,
                "description": getattr(cfg, "description", "") or "",
                # A built-in row's switch is real (it is the only way to take one
                # off the roster); a preset row has none until it is configured.
                "enabled": bool(getattr(cfg, "enabled", True)) if source != "preset" else False,
                "configured": source == "config",
                # Discovered under ``subagents/`` rather than written anywhere.
                # The page needs to tell the two apart: one is deletable, the
                # other is a folder on disk.
                "vendored": source == "vendored",
                # A vendored folder's venv is being built right now. Its own flag
                # rather than `test_running`: a build and a test are different
                # verbs on the same row, and one must not read as the other.
                "building": building,
                "builtin": source == "builtin",
                "group": _group(cfg, result.status),
                "upgrade_to": _upgrade_transport(cfg, source),
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
    if loop is None or not hasattr(loop, "apply_agents"):
        return
    try:
        entries = get_agents(config_path=get_config_path())
    except ValidationError as exc:
        # The config is shared with other clients (web UI, hand edits): a
        # concurrent write between our own write and this re-read can leave a
        # malformed entry here even though this call's own mutation succeeded.
        _raise_config_error(exc)
    loop.apply_agents(_as_configs(entries))


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
        kept = list(get_agents(config_path=get_config_path()))
        set_agents([*kept, entry], config_path=get_config_path())
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
        entries = get_agents(config_path=get_config_path())
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
        set_agents(entries, config_path=get_config_path())
    except (ValueError, ValidationError) as exc:
        _raise_config_error(exc)
    _hot_apply(agent_loop_factory)
    return {"updated": True, "name": target["name"]}


async def subagents_toggle(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Set `enabled` on one entry - the flag the roster filter reads."""
    name = params.get("name")
    enabled = bool(params.get("enabled"))
    try:
        entries = get_agents(config_path=get_config_path())
    except ValidationError as exc:
        _raise_config_error(exc)
    target = next((e for e in entries if e.get("name") == name), None)
    if target is None:
        # A built-in row exists on the table without existing in config, and
        # ``enabled`` is the only way to take one off the roster -- so toggling one
        # for the first time has to *create* its override row rather than report
        # the name unknown. Only ``enabled`` is written: everything else keeps
        # coming from the package's seed.
        from raven.agent.subagent.builtin_agents import BUILTIN_AGENT_NAMES

        if name not in BUILTIN_AGENT_NAMES:
            raise SubagentNotFoundError(f"no configured sub-agent named {name!r}", data={"name": name})
        target = {"name": name, "kind": "builtin"}
        entries.append(target)
    target["enabled"] = enabled
    try:
        set_agents(entries, config_path=get_config_path())
    except (ValueError, ValidationError) as exc:
        _raise_config_error(exc)
    _hot_apply(agent_loop_factory)
    return {"enabled": enabled}


async def subagents_remove(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Delete one entry. Reports `removed: false` for a name that was not there."""
    try:
        removed = remove_agent(params.get("name", ""), config_path=get_config_path())
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


# name -> the in-flight build task, and name -> why the last one failed ("" once
# one succeeds). Separate from `_RUNNING`: a build and a test are different verbs
# on the same row and one must not cancel or shadow the other.
_BUILDING: dict[str, asyncio.Task] = {}
_BUILD_ERROR: dict[str, str] = {}


async def _run_installer(installer: "Path", folder_name: str) -> tuple[int, str]:
    """Run the tree's installer for one folder. Returns ``(exit status, output)``.

    Its own function so a test can stand in for it. Spawning a real subprocess
    from an async test is what made these tests hang rather than fail: every test
    gets a fresh event loop, and the second one to fork in a process waits forever
    on a child watcher the first loop left behind. The seam is here rather than in
    the test because the alternative is patching ``asyncio`` itself.
    """
    proc = await asyncio.create_subprocess_exec(
        "bash",
        str(installer),
        folder_name,
        cwd=str(installer.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace")


async def _build_vendored(name: str, folder: "Path", installer: "Path") -> None:
    """Build one vendored folder's venv, recording why if it did not work.

    The verdict is read back off the filesystem rather than taken from the exit
    status: the script does several things per folder, and "it returned 0" is not
    the same claim as "this checkout now has a launcher raven can start". Nothing
    is returned -- the caller is long gone by the time this finishes, and the page
    learns the outcome by seeing the row go ready in a later list.
    """
    from raven.agent.subagent.vendored_agents import checkout_of, venv_ready

    code, out = await _run_installer(installer, folder.name)
    if venv_ready(checkout_of(folder)):
        _BUILD_ERROR.pop(name, None)
        logger.info("Built the vendored sub-agent {!r}", name)
        return
    tail = out.strip().splitlines()
    _BUILD_ERROR[name] = tail[-1] if tail else f"install.sh exited {code}"
    logger.warning("Building the vendored sub-agent {!r} failed: {}", name, _BUILD_ERROR[name])


async def subagents_build(params: dict) -> dict:
    """Start building one vendored folder's venv. Returns as soon as it is under way.

    Not awaited, unlike every other method here: one of these is a few hundred MB
    of downloads, so holding the request open would time it out on the client and
    leave the build running with nobody able to see it. The row carries
    ``building`` until it finishes, which is what the page watches.

    Idempotent while running -- a second call for the same name reports the one
    already in flight rather than starting a second `uv sync` against the same
    directory, which is how a venv ends up half-written.
    """
    from raven.agent.subagent.vendored_agents import installer_for, vendored_folder

    name = params.get("name", "")
    existing = _BUILDING.get(name)
    if existing is not None and not existing.done():
        return {"building": True, "detail": f"a build is already running for {name!r}"}

    folder = vendored_folder(name)
    if folder is None:
        raise SubagentNotFoundError(f"no vendored sub-agent named {name!r}", data={"name": name})
    installer = installer_for(folder)
    if installer is None:
        raise SubagentNotFoundError(f"the sub-agent tree holding {name!r} ships no install.sh", data={"name": name})

    _BUILD_ERROR.pop(name, None)
    task = asyncio.ensure_future(_build_vendored(name, folder, installer))
    _BUILDING[name] = task
    # Identity-checked on the way out, so a later call's entry is never removed
    # by an earlier call's completion.
    task.add_done_callback(lambda t: _BUILDING.pop(name, None) if _BUILDING.get(name) is t else None)
    return {"building": True, "detail": ""}


def _find(name: str, source: str) -> Any:
    """The config object a test should run against, by name and source.

    ``get_third_party_subagents`` re-validates the *whole* on-disk section, so a
    malformed entry anywhere in it (written by any client sharing this config,
    not necessarily this feature) would otherwise raise a bare ``ValidationError``
    out of a request to test one unrelated, perfectly healthy row.
    """
    try:
        pool = third_party_subagent_presets() if source == "preset" else get_agents(config_path=get_config_path())
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
    dispatcher.register("subagents.build", subagents_build)
    dispatcher.register("subagents.test", subagents_test)
    dispatcher.register("subagents.test_cancel", subagents_test_cancel)


__all__ = [
    "subagents_list",
    "subagents_probe",
    "subagents_add",
    "subagents_update",
    "subagents_toggle",
    "subagents_remove",
    "subagents_build",
    "subagents_test",
    "subagents_test_cancel",
    "register_subagents_methods",
]
