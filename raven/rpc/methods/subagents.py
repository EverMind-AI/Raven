"""``subagents.*`` RPC handlers: configure third-party sub-agents from the TUI.

Thin adapters only. The config write path, the preset templates, the probe and
the persisted test verdicts all live in ``raven.config`` / ``raven.agent.subagent``,
and the served page reaches them through these same handlers; duplicating any of
that logic here would let the TUI and the page disagree about what "installed"
means or which fields a write is allowed to touch.

The install group is computed here rather than in the client because a client
that computes it is how the rule drifts: a second copy in the TUI, or in the
page, would be one more place for it to go stale. For `kind == "acp"` the rule
is whether the executable -- and, for a shim-launched preset, the agent the shim
drives -- is on the login shell's PATH, which is the same question `ui-web/`'s
agent rows gate on (`probe_status === "missing"`).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, NoReturn

from loguru import logger
from pydantic import ValidationError

from raven.agent.subagent.backends import acp_snapshot_for, agent_meta
from raven.agent.subagent.presets import (
    THIRD_PARTY_SUBAGENT_PRESETS,
    third_party_subagent_preset,
    third_party_subagent_presets,
)
from raven.agent.subagent.probe import (
    ProbeResult,
    capabilities_wanted,
    ping_agent,
    probe_all,
    record_capabilities,
    run_test,
)
from raven.agent.subagent.probe_state import TestStateStore
from raven.config.loader import get_config_path
from raven.config.schema import SubagentsConfig
from raven.config.update_subagents import (
    get_agents,
    reject_unsupported_openai_fields,
    remove_agent,
    set_agents,
    validate_agents,
)
from raven.rpc.errors import (
    ConfigFieldReadonlyError,
    ConfigValidationError,
    SubagentNotFoundError,
    SubagentNotReadyError,
)

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
    if getattr(cfg, "kind", None) == "acp":
        # An acp row reaches "ready" only from a recorded capability snapshot, and
        # ``_test_acp`` records one only for a configured entry: a preset is a
        # template, so testing one deliberately writes nothing. Keying acp on
        # "ready" therefore pinned every acp preset to uninstalled for good, and
        # the overlay's uninstalled section is view-only for an unconfigured row --
        # the Test that was the only way out was unreachable from the only place
        # the row appeared. "attention" already means the executable was found, so
        # the row belongs with the ones a user can act on, and the caveat it
        # carries stays on the row in ``probe_detail``.
        return "uninstalled" if probe_status in ("missing", "unknown") else "installed"
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
    # A preset is also withheld when a configured row already occupies the name it
    # ships under, which a hand-written row may do without being that preset at
    # all. Two rows of one name is unusable: every verb here addresses a row by
    # name, so the second is unreachable and the model is offered a duplicate.
    #
    # Deliberately here and not in the provenance backfill, which is where this
    # started: `preset` is read at runtime (`session_mcp_for`, the transport
    # upgrade hint), so inferring it from a name would hand one preset's measured
    # policy to a row that only shares its label -- and a name is the field the
    # overlay lets its owner edit. What is a presentation collision stays a
    # presentation rule.
    taken_names = {getattr(c, "name", "") for c in configured}
    presets = [
        p for p in third_party_subagent_presets() if p.get("preset") not in claimed and p.get("name") not in taken_names
    ]
    preset_cfgs = _as_configs(presets)

    # The built-in rows, merged the way the runtime merges them, so this list shows
    # the same table the model dispatches against -- including a user's override of
    # one. They are reported as ``source == "builtin"`` rather than "config",
    # because "configured" drives the delete button and a seed row cannot be
    # deleted: not writing one is what "use the default" means.
    from raven.agent.subagent.builtin_agents import (
        canonical_agent_name,
        is_builtin_agent_name,
        merge_builtin_seeds,
    )
    from raven.agent.subagent.vendored_agents import (
        discover_product_rows,
        merge_product_seeds,
        product_state,
    )

    # Composed exactly the way ``AgentRegistry.apply`` composes it, and for the
    # same reason the registry exists at all: this list is what a human reads to
    # answer "what can the model dispatch to", so a view assembled from a
    # different subset of the sources is a view that disagrees with the runtime.
    # It did: the discovered rows were on the table and absent here, which reads
    # as "the agents did not install" on the one screen built to tell you they
    # had.
    discovered = discover_product_rows()
    discovered_names = {getattr(c, "name", "") for c in discovered}
    unready = product_state()
    merged = merge_product_seeds(configured, discovered)

    # One merge, so the rows rendered here are the rows the runtime dispatches
    # against -- including an acp row of a built-in name, where the merge fills
    # the host's `raven acp` command and the list must show that command, not the
    # empty one the config file holds. The Test button probes the command it is
    # shown, so a view that keeps the empty one would test a different launch.
    merged_all = merge_builtin_seeds(merged)
    builtin_cfgs = [c for c in merged_all if getattr(c, "kind", None) == "builtin"]
    # Filtered by the rule the table applies, not merely split on kind. A built-in
    # name with a cli / openai kind is ignored at runtime (``merge_builtin_seeds``
    # refuses to displace the mandatory seed), so listing it here put a second row
    # of that name on the roster, indistinguishable from a live agent and reported
    # enabled. An acp row of a built-in name is the supported transport switch and
    # IS live, so it is listed as config.
    external = [
        c
        for c in merged_all
        if getattr(c, "kind", None) != "builtin"
        and not (is_builtin_agent_name(getattr(c, "name", "") or "") and getattr(c, "kind", None) in ("cli", "openai"))
        # A hidden row is reached only through another row's routes; the page
        # shows the roster the model reads, and this row is not on it.
        and not getattr(c, "hidden", False)
    ]
    configured_names = {canonical_agent_name(getattr(c, "name", "")) for c in configured}

    entries: list[tuple[Any, str]] = [(c, "builtin") for c in builtin_cfgs]
    # A discovered row is reported as its own source, not as "config": there is no
    # config entry to delete, and the delete button reads ``configured``. Removing
    # one means removing its folder. A row the user has also written by hand is
    # theirs, so it keeps the config source and its delete button.
    # "vendored" is the wire name for a discovered row (the clients and the
    # schema read it); renaming it is a schema change, not a refactor.
    entries += [
        (c, "config" if getattr(c, "name", "") in configured_names else "vendored")
        if getattr(c, "name", "") in discovered_names
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
        # The probe launches nothing: it checks the command's first token, which
        # for one of these is the interpreter and always exists. So a folder
        # whose launcher or engine is gone would probe "ready" while the roster
        # -- correctly -- refused to advertise it. The readiness verdict
        # overrides the probe for a discovered row, and carries the reason,
        # because "not ready" without "why" sends the reader looking. Always
        # `attention`, never `missing`: `missing` is what the page reads as
        # "offer Install", and no state of a discovered row is installable from
        # here -- a launcher ships with the wheel and an engine installs as a
        # wheel of its own. That is also why a probe miss on a *ready* row is
        # demoted below: readiness checks the command's absolute paths and the
        # probe `which`es its first token, so a relative `SUBAGENT_PYTHON`
        # can be ready by one rule and missing by the other -- and rendering
        # `missing` would offer an Install whose click does nothing.
        if source == "vendored":
            if (verdict := unready.get(cfg.name)) and not verdict.ready:
                result = replace(result, status="attention", detail=verdict.detail)
            elif result.status == "missing":
                result = replace(result, status="attention")
        last = result.last_test
        task = _RUNNING.get(cfg.name)
        # One store read per acp row, handed to `agent_meta` so it is not read
        # twice. Not cached across rows or listings: a cache is what keeps
        # serving a stale verdict after a verify has already fixed it.
        snapshot = acp_snapshot_for(cfg) if cfg.kind == "acp" else None
        meta = agent_meta(cfg, snapshot=snapshot)
        # One of raven's own, whichever way this install registered it: the
        # built-in row, a product discovered under `agents/`, or a config row
        # whose acp handshake named raven -- the shipped installer writes a
        # product as a plain config row, and that row is still raven's.
        own = source in ("builtin", "vendored") or getattr(snapshot, "agent_name", "") == "raven"
        rows.append(
            {
                "name": cfg.name,
                "preset": getattr(cfg, "preset", None),
                "kind": cfg.kind,
                "description": getattr(cfg, "description", "") or "",
                # A built-in row is always on (its switch is the package's, not
                # config's); a preset row has none until it is configured.
                "enabled": bool(getattr(cfg, "enabled", True)) if source != "preset" else False,
                "configured": source == "config",
                # Discovered under ``agents/`` rather than written anywhere.
                # The page needs to tell the two apart: one is deletable, the
                # other is a folder on disk. "vendored" is the wire name.
                "vendored": source == "vendored",
                # Nothing is buildable in the product tree (launchers ship with
                # the wheel; engines install as wheels), so no build is ever in
                # flight. The key stays for wire compatibility.
                "building": False,
                # Read from the same derivation the roster and the DAG pre-check
                # use, never from `kind`: an acp row's statefulness comes from its
                # own capability snapshot and an openai row's from a declaration.
                "stateful": meta.stateful,
                "builtin": source == "builtin",
                "own": own,
                "group": _group(cfg, result.status),
                "upgrade_to": _upgrade_transport(cfg, source),
                "probe_status": result.status,
                "probe_detail": result.detail,
                "has_api_key": bool((getattr(cfg, "api_key", "") or "").strip()),
                # Measured by the handshake, not guessed from the status: an
                # `attention` row is equally "wants a credential" and "installed
                # but never verified", and those ask the reader for opposite
                # things. Always present, never omitted -- a client cannot tell
                # a missing key from a false one, and one day it will mean
                # "this server predates the field".
                "needs_auth": bool(getattr(snapshot, "needs_auth", False)),
                "mcps": list(getattr(cfg, "mcps", None) or []),
                "allow_mcp_secrets": bool(getattr(cfg, "allow_mcp_secrets", False)),
                "last_test_ok": None if last is None else last.ok,
                "last_test_detail": None if last is None else last.detail,
                "last_test_at_ms": None if last is None else last.tested_at_ms,
                "test_running": task is not None and not task.done(),
                "model": getattr(cfg, "model", None),
                "model_choices": [{"value": c.value, "name": c.name, "group": c.group} for c in meta.model_choices],
                # The row's editing rule, not ownership: what `subagents.update`
                # accepts for `model` on this row. `own` is the ownership mark.
                "model_source": _model_rule(cfg, snapshot, meta),
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

    Only presentation, credentials and MCP policy fields come from the caller;
    every execution field (command, resumeCommand, idSource, transcriptFormat,
    ...) comes from the preset, which is already correct and version-verified.

    An entry that would land enabled and carries a pinged kind is proved first:
    the same one real prompt `subagents.toggle` sends, through this entry's own
    backend, and a refusal in the agent's own words when nothing answers. So
    this spends one call on that agent's own quota, and can hold the add for up
    to `_ENABLE_PING_TIMEOUT_SECONDS`. `force: true` bypasses it, as on the
    switch. A refusal stores nothing at all -- not the row disabled -- because
    the surface that calls this offers one verb per row: an added row is
    connected, and a row that could not be proved has to stay one Connect away
    rather than becoming a second thing to switch on.
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
    if params.get("mcps") is not None:
        entry["mcps"] = list(params["mcps"])
    if params.get("allow_mcp_secrets") is not None:
        entry["allowMcpSecrets"] = params["allow_mcp_secrets"]
    # Every preset ships `enabled: true`, but an openai entry with no key cannot
    # answer: advertising it to the model would produce a sub-agent that fails on
    # first dispatch. Added disabled instead, so the user enables it once the key
    # is in. A preset of a pinged kind lands enabled because the gate below proves
    # it before the write -- the roster's entry criterion for those kinds is that
    # the agent answers now, and this add establishes it rather than leaving a
    # second, ungated way onto the roster.
    if entry.get("kind") == "openai" and not (entry.get("apiKey") or "").strip():
        entry["enabled"] = False
    # Every refusal the write owes, before the gate spends anything: a name
    # already taken, or an entry the schema will not have. Without this the ping
    # would run for an add that could never land -- and for a duplicate name it
    # would run against the *stored* row of that name, because the schema keeps
    # the first of two, so it would prove a row this call is not writing.
    try:
        reject_unsupported_openai_fields([entry])
        entries = [*_read_agents(), entry]
        validate_agents(entries, config_path=get_config_path())
    except (ValueError, ValidationError) as exc:
        _raise_config_error(exc)
    # `force is not True` for the same reason the switch reads it that way: it is
    # declared a boolean, and `"no"` / `"0"` / `"false"` are all truthy in Python
    # while reading as a refusal to whoever sent them.
    if entry.get("enabled") and params.get("force") is not True:
        await _refuse_unless_it_answers(entries, str(entry["name"]), refusal="so it was not added")
        # Re-read over the ping, the same way the switch does: the list read
        # before it would revert every other `subagents.*` write that landed
        # during it. Only this call's own entry is carried across.
        entries = [*_read_agents(), entry]
    try:
        set_agents(entries, config_path=get_config_path())
    except (ValueError, ValidationError) as exc:
        _raise_config_error(exc)
    _hot_apply(agent_loop_factory)
    return {"added": True, "name": entry["name"]}


def _factory_description(name: str, preset_name: str | None) -> str:
    """The shipped text a cleared description reverts to.

    Tried in the order a row could actually own one: its own preset (the
    add-time provenance field), or a discovered product's manifest (a vendored
    row, which carries no preset). A hand-written entry matching neither has
    nothing to fall back to, and blanking it is the honest answer -- the same
    fallback ``add`` already uses.

    A built-in row is deliberately not looked up: its factory text is the
    package seed, and ``""`` is the schema's own way of saying "the seed's" --
    the merge drops a field at its default and lets the seed govern. Copying
    today's seed text in would pin it, and the row would stop following the
    package from then on.
    """
    if preset_name and preset_name in THIRD_PARTY_SUBAGENT_PRESETS:
        return third_party_subagent_preset(preset_name)["description"]
    from raven.agent.subagent.vendored_agents import discover_product_rows

    discovered = next((cfg for cfg in discover_product_rows() if getattr(cfg, "name", None) == name), None)
    return (getattr(discovered, "description", "") or "") if discovered is not None else ""


def _model_rule(cfg: Any, snapshot: Any, meta: Any) -> str:
    """What ``subagents.update`` accepts for ``model`` on this row.

    Defined once and read by both the listing and the write, so the menu the
    page draws is the menu the write offers -- two expressions of it are two
    places for the pair to drift apart. It is the menu, not the whole
    vocabulary: one of raven's own also takes a host-qualified id under either
    rule, since it runs on raven's providers whatever it advertised.

    The rule is the row's, not its kind's: a third-party acp row picks from the
    choices its handshake advertised, and one of raven's own picks from raven's
    own provider catalogue whatever it advertised. An own row runs on this
    host's providers -- a product installed beside this raven inherits them --
    so the catalogue is the live list of what it can serve, while its handshake
    is a launch-time capture of the same list: measured once on a probe
    session, capped per provider, and stale from the first credential edit
    after it. Drawing that capture beside the composer's live picker put two
    different menus on one catalogue. A third party that advertised none is
    taken at its word: its own credentials decide what it can run, and raven's
    ids would be refused by the agent itself.
    """
    if cfg.kind == "builtin":
        return "raven"
    # A product whose folder carries its own chat credential is not on this
    # host's catalogue at all: its launcher takes that key with the provider and
    # model beside it and never reads what the host would have lent. Nothing
    # here can name what it answers with, and a pick made from raven's ids would
    # be pushed at a session whose own config has never heard of them -- so the
    # model is the folder's, the way an openai row's is its section's.
    from raven.agent.subagent.vendored_agents import product_llm_key

    if product_llm_key(getattr(cfg, "name", "") or ""):
        return "fixed"
    if cfg.kind != "acp":
        return "fixed"
    return "raven" if getattr(snapshot, "agent_name", "") == "raven" else "agent"


def _host_pair(model: str) -> str | None:
    """The id a built-in row stores for ``model``, or ``None`` when no provider
    of raven's can serve it.

    The built-in row runs in this process, on raven's providers, so its model is
    checked against the pairing the dispatch will make -- ``ProviderPool.bind_pin``
    with the provider the stored id names, read by ``stored_provider_name``, the
    same function the pin reads it with -- rather than against any agent's menu,
    and decided here so a write that lands is a write that runs: the id names a
    provider raven knows, by its prefix or by appearing in a configured section's
    own model list (a passthrough vendor no spec matches, stored naming that
    section, which then serves only the ids it lists), and that provider's
    section holds a usable credential. Answered from config alone. The picker's
    own listing (``model.options``) also asks the codex account and a local
    runtime what they hold, which is seconds of network a write has no reason to
    wait on.

    A config that cannot be read raises out of here: that is the server's
    failure, not a fact about the reader's model or key, and the refusal this
    answer feeds would have blamed both.
    """
    from raven.config.loader import load_config
    from raven.config.schema import section_has_credentials
    from raven.providers.registry import find_by_name, split_model_id
    from raven.providers.wire import stored_model_id, stored_provider_name

    providers = load_config().providers

    def usable(provider: str) -> bool:
        section = providers.get(provider)
        return section is not None and bool(section_has_credentials(section, find_by_name(provider)))

    def listed(provider: str, model_id: str) -> bool:
        return model_id in (getattr(providers.get(provider), "models", None) or [])

    provider = stored_provider_name(model, providers=providers)
    if provider is not None:
        if find_by_name(provider) is None and not listed(provider, split_model_id(model)[1]):
            return None
        return stored_model_id(provider, model) if usable(provider) else None
    names = [*type(providers).model_fields, *(providers.model_extra or {})]
    listing = next((n for n in names if listed(n, model)), None)
    if listing is None or not usable(listing):
        return None
    return stored_model_id(listing, model)


async def subagents_update(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Change editable presentation, credential, MCP policy and model fields."""
    name = params.get("name")
    try:
        entries = get_agents(config_path=get_config_path())
    except ValidationError as exc:
        _raise_config_error(exc)
    from raven.agent.subagent.builtin_agents import canonical_agent_name, is_builtin_agent_name

    if is_builtin_agent_name(name or ""):
        # Resolved the way the table resolves it: a seed's override is merged
        # under the canonical name whatever spelling config stored it in, and an
        # acp row of that name is the supported transport switch -- either is
        # the row the reader is editing, and the edit lands on it in place. A
        # cli or openai row of the name is one the table ignores
        # (`merge_builtin_seeds` refuses to displace the seed), so an edit there
        # would change nothing anyone sees, and a second row of the name would
        # not be written beside it either.
        canonical = canonical_agent_name(name or "")
        of_name = [e for e in entries if canonical_agent_name(e.get("name") or "") == canonical]
        target = next((e for e in of_name if e.get("kind") in ("builtin", "acp")), None)
        if target is None and of_name:
            raise ConfigFieldReadonlyError(
                f"{name!r} is a built-in agent, and config holds a {of_name[0].get('kind')!r} row of that name "
                "the table ignores; remove that row before editing the built-in one",
                data={"field": "name", "name": name},
            )
    else:
        target = next((e for e in entries if e.get("name") == name), None)
    materialized_discovered = target is None
    if target is None:
        from raven.agent.subagent.vendored_agents import discover_product_rows

        discovered = next((cfg for cfg in discover_product_rows() if getattr(cfg, "name", None) == name), None)
        if discovered is not None:
            target = discovered.model_dump(by_alias=True)
            entries.append(target)
        elif is_builtin_agent_name(name or ""):
            # A built-in row has no config entry until its first edit -- writing
            # one here is how "retune this agent" is spelled, on the same terms
            # a `skills` override already is (see `builtin_agents.merge_builtin_seeds`).
            from raven.config.schema import BuiltinAgentConfig

            target = BuiltinAgentConfig(name=canonical_agent_name(name)).model_dump(by_alias=True)
            entries.append(target)
        else:
            raise SubagentNotFoundError(f"no configured sub-agent named {name!r}", data={"name": name})
    # Read before anything below moves them: the two fields whose new value the
    # agent has never been asked about. Everything else this call can change is
    # presentation or policy, which cannot alter what the agent answers. The
    # name goes with them because a rename moves it too, and the re-read below
    # has to find the row this call started from under whichever spelling it
    # was stored as.
    key_before = target.get("apiKey")
    model_before = target.get("model")
    name_before = target.get("name")
    row_before = dict(target)
    new_name = _clean_name(params.get("new_name"), field="new_name")
    if new_name:
        if materialized_discovered and new_name != name:
            # A row written here from a shipped launcher or a package seed is
            # bound to it by name; renamed, it would become a second agent.
            raise ConfigFieldReadonlyError(
                "a discovered or built-in sub-agent override cannot be renamed; its name binds it to what it overrides",
                data={"field": "new_name", "name": name},
            )
        target["name"] = new_name
    if "description" in params:
        description = params["description"]
        # Blank, whitespace-only or explicit ``null`` all revert to the factory
        # text, same as add: a cleared description would otherwise strip the
        # agent's only description from the `spawn` roster the dispatching
        # model reads.
        target["description"] = (
            description
            if description is not None and description.strip()
            else _factory_description(name, target.get("preset"))
        )
    # Blank/absent means keep the stored key: the caller is never shown it, so
    # an empty field is "unchanged", never "clear it".
    if (params.get("api_key") or "").strip():
        target["apiKey"] = params["api_key"]
    if params.get("mcps") is not None:
        target["mcps"] = list(params["mcps"])
    if params.get("allow_mcp_secrets") is not None:
        target["allowMcpSecrets"] = params["allow_mcp_secrets"]
    if params.get("clear_model") or params.get("model") is not None:
        cfg_for_meta = _as_configs([target])[0]
        snapshot = acp_snapshot_for(cfg_for_meta) if cfg_for_meta.kind == "acp" else None
        meta = agent_meta(cfg_for_meta, snapshot=snapshot)
        rule = _model_rule(cfg_for_meta, snapshot, meta)
        if rule == "fixed":
            raise ConfigFieldReadonlyError(
                f"model is not editable for kind {cfg_for_meta.kind!r}: it has no menu this call can pick from",
                data={"field": "model", "name": name},
            )
        if params.get("clear_model"):
            target["model"] = None
        else:
            proposed = str(params["model"])
            choices = [c.value for c in meta.model_choices]
            own_acp = cfg_for_meta.kind == "acp" and getattr(snapshot, "agent_name", "") == "raven"
            # An id the agent advertised is one it serves, whichever menu the
            # page drew. Not keyed on the rule: that names the menu, and keying
            # the write to it meant an own row stopped taking its own
            # handshake's ids the moment its menu became raven's catalogue.
            if proposed in choices:
                target["model"] = proposed
            elif rule == "agent" and not own_acp:
                # Mirrors ``SubagentManager.set_instance_model``'s own message: the
                # values are opaque provider-qualified ids, so a refusal names how
                # many the agent offers rather than leaving a reader to guess at
                # the vocabulary.
                raise ConfigValidationError(
                    f"{name!r} has no model {proposed!r}"
                    + (f"; it offers {len(choices)}" if choices else "; it offers none"),
                    data={"field": "model", "name": name},
                )
            else:
                # One of raven's own reaches here under either rule, and so takes
                # either vocabulary: it runs on this host's providers, so a host id
                # is a model it can serve whatever its handshake advertised. The
                # rule the listing showed is a menu, not a gate -- a re-measure
                # that gave the row its menu between the read and the write would
                # otherwise refuse the pick the reader was offered.
                #
                # Stored naming its provider, the way `config.set model` stores the
                # host's: a bare id is claimed by keyword matching at dispatch, and
                # that sends it wherever those rules land rather than to the
                # section the reader picked it under.
                provider = str(params.get("provider") or "").strip()
                if provider:
                    from raven.providers.wire import stored_model_id

                    proposed = stored_model_id(provider, proposed)
                stored = _host_pair(proposed)
                if stored is None and own_acp:
                    raise ConfigValidationError(
                        f"{name!r} has no model {proposed!r}: it is none of the {len(choices)} its handshake "
                        "advertised, and it runs on raven's own providers, none of which can serve it either",
                        data={"field": "model", "name": name},
                    )
                if stored is None:
                    raise ConfigValidationError(
                        f"{name!r} runs on raven's own providers, and none of them can serve {proposed!r}: "
                        "it names no provider raven knows, or that provider has no usable credentials",
                        data={"field": "model", "name": name},
                    )
                target["model"] = stored
    # A credential or a model swapped under a row that is already on is a
    # connect nobody gated: the row goes on serving dispatches with something
    # nothing has tried, and the first real task is what discovers the typo. So
    # it is asked here, the same question the switch asks, and only when one of
    # the two actually moved -- the sheet posts whatever is in its field, so an
    # unchanged form would otherwise spend a call on every save. A row that is
    # off is left alone: nothing is serving, and the switch that turns it on is
    # already gated, so asking here would buy the same answer twice.
    if bool(target.get("enabled")) and (target.get("apiKey") != key_before or target.get("model") != model_before):
        await _refuse_unless_it_answers(entries, str(target["name"]), refusal="so it was not changed")
        entries, target = _merged_over_the_ping(target, row_before, name_before, materialized=materialized_discovered)
    try:
        reject_unsupported_openai_fields([target])
        set_agents(entries, config_path=get_config_path())
    except (ValueError, ValidationError) as exc:
        _raise_config_error(exc)
    _hot_apply(agent_loop_factory)
    return {"updated": True, "name": target["name"]}


def _merged_over_the_ping(
    target: dict, before: dict, stored_name: str | None, *, materialized: bool
) -> tuple[list[dict], dict]:
    """Re-read the agent list across the gate's await, keeping both authors.

    The list read before a ping of up to a minute is stale in two ways, and
    they want different answers.

    Rows this call never touched are simply whatever disk says now, so the list
    is read again -- writing back the one read before the ping would revert
    every other `subagents.*` write that landed during it.

    The row this call *is* editing has two authors by then: this call, whose
    fields are the point of the write, and whoever else wrote to the same row
    while the agent was being asked. Carrying the pre-await copy across keeps
    the first and silently restores the second over the top of a call that has
    already answered success. So only the fields this call actually changed are
    replayed onto the freshly read row. `subagents.update` only ever sets
    fields, never removes one, which is what makes a comparison against the
    pre-mutation copy a complete account of what it did; a removal added later
    would have to be replayed here too.

    A row that is gone under the name this call read it as was renamed or
    removed meanwhile, and there is nothing left to merge onto: the change is
    refused rather than resurrecting a row somebody deleted. The exception is a
    row this call materialized itself -- a discovered folder or a built-in
    getting its first stored entry -- which was never on disk to be found.
    """
    entries = _read_agents()
    current = next((e for e in entries if e.get("name") == stored_name), None)
    if current is None:
        if materialized:
            entries.append(target)
            return entries, target
        raise SubagentNotFoundError(
            f"sub-agent {stored_name!r} was renamed or removed while it was being proved, so it was not changed",
            data={"name": stored_name, "field": "name"},
        )
    current.update({key: value for key, value in target.items() if before.get(key) != value})
    return entries, current


def _is_switch_row(stored: dict, discovered: dict) -> bool:
    """Does this stored row carry nothing but the switch?

    Three ways of telling, because a row has to survive being rewritten by a
    raven that does not know every field in it:

    * ``switchOnly`` says so outright, and is what this build writes.
    * An empty ``command`` says so structurally: the row declares no launcher,
      so it is not a definition of anything. ``command`` is a field every
      version knows, so it comes back unchanged from an older rewrite -- and it
      says nothing about the folder, so a manifest that has moved on cannot
      make the row stop looking like a switch.
    * Being the discovered entry with nothing but its flag changed comes to the
      same thing, and covers a row written before either of the above.
    """
    if stored.get("switchOnly") or stored.get("switch_only"):
        return True
    # `"command" in stored`, not a falsy read of it: an openai row has no
    # command field at all, and reading one as empty made every one of them look
    # like a switch for a folder -- which dropped it from the roster.
    if "command" in stored and not str(stored["command"] or "").strip():
        return True
    drop = {"enabled", "switchOnly", "switch_only"}
    return {k: v for k, v in stored.items() if k not in drop} == {k: v for k, v in discovered.items() if k not in drop}


def _discovered_entry(name: str) -> dict | None:
    """One discovered row as a config entry, or ``None`` if nothing is named that.

    Dumped by alias, which is the form ``get_agents`` hands back and
    ``set_agents`` validates -- the two must be the same shape or a materialized
    row would fail the write that stores it.

    The command it carries is already resolved to this machine's paths, exactly
    as each folder's ``install.py`` resolves it: a stored row is a command line,
    and ``merge_product_seeds`` has its own guard (``_launcher_is_gone``) for a
    stored path a later upgrade moved.
    """
    from raven.agent.subagent.vendored_agents import discover_product_rows

    for row in discover_product_rows():
        if row.name == name:
            return row.model_dump(by_alias=True)
    return None


_PINGED_KINDS = ("cli", "acp", "openai")
"""Kinds whose readiness is settled by running them, which is every kind but one.

`builtin` is the only name absent, because it is this process: no command to
launch, no endpoint to reach, and no connect to gate. Every other kind is asked
the same question in the same way -- one prompt, and an answer required -- since
nothing short of that separates an agent that is configured from one that works.

`openai` was exempt on the grounds that the free `/models` probe had already
settled its credential. That probe runs on the listing and on an explicit test,
never on this path, so the key an add carries has not been probed: it did not
exist when the listing last ran. The exemption was reasoning about a check that
happens somewhere else.

The cost is bounded by who reaches the gate: only a write that leaves the row
enabled, which for an endpoint means one that came with a key. A keyless openai
add lands disabled and is never asked.
"""


async def _refuse_unless_it_answers(entries: list[dict], name: str, *, refusal: str) -> None:
    """Layer 2, on the path of every enable it gates: enable only what replies.

    Run live rather than read from a recorded verdict, because the question is
    whether the agent works *now* -- a pass recorded before a token expired would
    put a dead agent back on the roster.

    Composed from ``entries`` -- this call's own post-mutation, in-memory list --
    the same way `subagents_list` composes the roster it renders, rather than a
    fresh disk read: a fresh read only ever sees this call's own resolution as it
    stood *before* this write, which is exactly wrong at the three points this
    gate exists to cover. A discovered folder enabled for the first time has no
    stored row yet, so a fresh read finds nothing and answers "not configured"
    before a ping ever runs. A folder switched off and back on has a stub of
    `command: ""` on disk from the off; this call has already dropped that stub
    from `entries`, but a fresh read still sees it and pings an empty command
    that can never pass. And an add's entry is not on disk at all yet -- the
    whole point of gating it there is that a failure never puts it there.

    ``refusal`` is what this caller did not do, and every caller states it,
    because the two differ in what a failure leaves behind: the switch leaves
    the row it declined to turn on, the add leaves no row at all.
    """
    from raven.agent.subagent.builtin_agents import merge_builtin_seeds
    from raven.agent.subagent.vendored_agents import discover_product_rows, merge_product_seeds

    merged = merge_builtin_seeds(merge_product_seeds(_as_configs(entries), discover_product_rows()))
    cfg = next((c for c in merged if getattr(c, "name", None) == name), None)
    if cfg is None:
        # The merge dropping the row means the roster will not dispatch it
        # either, so there is nothing here to prove -- and refusing would break
        # bookkeeping this helper is not responsible for.
        return
    if getattr(cfg, "kind", None) not in _PINGED_KINDS:
        return
    result = await ping_agent(cfg)
    if result.ok:
        if getattr(cfg, "kind", None) == "acp" and capabilities_wanted(cfg):
            # It answered, so it can be measured: the record its statefulness,
            # menu and modes are read from is written now rather than left to
            # the Test button or the next restart. Best effort -- the agent has
            # already proved itself, and a handshake that fails afterwards is
            # not a reason to refuse the connect.
            try:
                await record_capabilities(cfg)
            except Exception as exc:  # noqa: BLE001 - the connect stands on the ping
                logger.warning("subagents: {!r} answered but its capabilities could not be recorded: {}", name, exc)
        return
    # `detail` is repeated inside `data` deliberately: the dispatcher fills
    # `data` from `detail` only when a handler passed no `data` of its own, so a
    # call site passing both drops the human-readable half.
    detail = f"sub-agent {name!r} did not answer a test message, {refusal}: {result.detail}"
    raise SubagentNotReadyError(
        detail,
        data={"name": name, "field": "enabled", "detail": detail},
    )


def _read_agents() -> list[dict]:
    """The editable agent list, or the config error the malformed section is.

    A helper because the toggle and the add both read the list twice -- once to
    compose the row they ping, once to write -- and every read owes the same
    error translation.
    """
    try:
        return get_agents(config_path=get_config_path())
    except ValidationError as exc:
        _raise_config_error(exc)


def _resolve_toggle(entries: list[dict], name: Any, enabled: bool) -> tuple[list[dict], bool]:
    """Apply one switch to a freshly read agent list, in memory only.

    Extracted from ``subagents_toggle`` so it can run twice over two
    independently read lists: the enable gate has to ping before the write, and
    a list held across that await would revert whatever else wrote config in the
    meantime. Each pass mutates only the dicts of the list it was handed, so two
    passes cannot contaminate each other.

    Returns the list to write and the flag that was actually resolved, which is
    not always the flag that was asked for.
    """
    # A built-in row may also exist in config (as a field-level override of the
    # seed), and writing the switch onto that row would report success for a
    # change ``merge_builtin_seeds`` then discards -- a seed row's switch is the
    # package's. The one exception is the acp redeclaration, which is a real
    # config row with its own switch.
    from raven.agent.subagent.builtin_agents import is_builtin_agent_name

    if name and is_builtin_agent_name(name):
        target = next((e for e in entries if e.get("name") == name), None)
        if target is None or target.get("kind") != "acp":
            raise ConfigFieldReadonlyError(
                f"sub-agent {name!r} is a built-in agent and cannot be switched off: an unnamed spawn "
                "and a dag node with no sub-agent both dispatch to it",
                data={"field": "enabled", "name": name},
            )
    target = next((e for e in entries if e.get("name") == name), None)
    discovered = _discovered_entry(str(name or ""))
    if target is None and discovered is None:
        raise SubagentNotFoundError(f"no configured sub-agent named {name!r}", data={"name": name})
    # A discovered folder's switch is asymmetric, and deliberately so.
    #
    # Off writes a row, because there is nowhere else for a "no" to live -- one
    # list, one ``enabled`` field, rather than a second switch in the shipped
    # manifest, which is package content and not a user's decision.
    #
    # On *removes* the row instead of writing ``enabled: true``, because for
    # these rows no row is the answer: the folder governs. A stored row saying
    # true would outlive its folder and override a later readiness failure --
    # putting a name that cannot start back on the dispatch roster.
    #
    # Which row may be dropped is asked two ways, because neither holds alone:
    # the marker the off direction writes survives a manifest that has moved on,
    # where a comparison stops recognising the row; and the comparison survives a
    # raven old enough not to know the field, which drops it silently on the next
    # rewrite of this list. A row that is neither marked nor a copy of the folder
    # is somebody's real override -- an ``install.py`` entry, a hand edit -- and
    # is left where it is.
    if discovered is not None and enabled and (target is None or _is_switch_row(target, discovered)):
        entries = [e for e in entries if e.get("name") != name]
    elif target is None:
        # Name, kind and the flag. Not a copy of the folder's entry: a copy is a
        # definition, and a definition of a vendored agent outlives the manifest
        # it was taken from -- which is how the row came back, after a downgrade
        # had dropped its marker, looking like somebody's override of a folder
        # that had moved on. This row declares no launcher because it defines
        # nothing; the folder still defines the agent, and the merge reads only
        # the flag from here.
        entries.append(
            {"name": name, "kind": discovered.get("kind", "cli"), "enabled": enabled, "command": "", "switchOnly": True}
        )
    elif enabled and discovered is None and "command" in target and not str(target["command"] or "").strip():
        # Asked to enable a row that names no launcher and no folder: there is
        # nothing to start, so the switch answers what is true rather than
        # writing a yes the roster would have to overrule. This is a switch stub
        # whose marker an older rewrite dropped and whose folder has since gone.
        target["enabled"] = False
        enabled = False
    else:
        target["enabled"] = enabled
    return entries, enabled


async def subagents_toggle(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Set `enabled` on one entry - the flag the roster filter reads.

    Switching a row *on* first sends one real prompt through that row's own
    backend and refuses the enable, in the agent's own words, when nothing
    answers: the roster's entry criterion is that the agent works now, not that
    it is installed. So this spends one call on that agent's own quota and can
    hold the switch for up to `_ENABLE_PING_TIMEOUT_SECONDS`. Exempt: switching
    off, a `builtin` row (this process, with no backend to reach), and
    `force: true`, the operator's override for an agent whose provider is
    briefly down. A refusal writes nothing.
    """
    name = params.get("name")
    requested = bool(params.get("enabled"))
    entries, enabled = _resolve_toggle(_read_agents(), name, requested)
    # `force is not True`, not a falsy read of a truthy value: it is declared a
    # boolean and is the one documented way past this gate, and `"no"` / `"0"` /
    # `"false"` are all truthy in Python while reading as a refusal to whoever
    # sent them.
    if enabled and params.get("force") is not True:
        await _refuse_unless_it_answers(entries, str(name), refusal="so it was not switched on")
        # Resolve again over a fresh read. The ping is the only await this
        # handler has, and writing the list read before it would revert every
        # other ``subagents.*`` write that landed during it; re-reading here
        # leaves no await between the read and ``set_agents``, which is what
        # made the pre-gate handler's read-modify-write atomic. The row that
        # was pinged is therefore up to one ping-duration stale -- deliberately,
        # because the ping asks whether the agent is healthy, not what the
        # config bytes currently say.
        entries, enabled = _resolve_toggle(_read_agents(), name, requested)
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


async def subagents_build(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Report that there is nothing to build. Kept for wire compatibility.

    The fork-era tree had venvs this method's installer could produce; the
    product tree has none. A launcher ships with the wheel, and a missing
    engine installs as a wheel of its own -- an install this server must not
    perform into its own running environment. The row's readiness detail
    already names the wheel, so this answers with that reason rather than
    starting anything; a client that still calls it learns why in `detail`.
    """
    from raven.agent.subagent.vendored_agents import product_folder, product_state

    name = params.get("name", "")
    # `detail` is repeated inside `data` deliberately: the dispatcher fills
    # `data` from `detail` only when a handler passed no `data` of its own, so a
    # call site passing both drops the human-readable half.
    if product_folder(name) is None:
        detail = f"no discovered sub-agent named {name!r}"
        raise SubagentNotFoundError(detail, data={"name": name, "detail": detail})
    verdict = product_state().get(name)
    if verdict is not None and not verdict.ready:
        return {"building": False, "detail": verdict.detail}
    return {"building": False, "detail": f"nothing to build: {name!r} is ready"}


def _find(name: str, source: str) -> Any:
    """The config object a test should run against, by name and source.

    ``get_third_party_subagents`` re-validates the *whole* on-disk section, so a
    malformed entry anywhere in it (written by any client sharing this config,
    not necessarily this feature) would otherwise raise a bare ``ValidationError``
    out of a request to test one unrelated, perfectly healthy row.

    A discovered row lives in neither pool: its folder is the only place it
    exists, and it is already a config object.
    """
    if source == "vendored":
        from raven.agent.subagent.vendored_agents import discover_product_rows

        cfg = next((c for c in discover_product_rows() if getattr(c, "name", None) == name), None)
        if cfg is None:
            raise SubagentNotFoundError(f"no {source} sub-agent named {name!r}", data={"name": name})
        return cfg
    try:
        pool = third_party_subagent_presets() if source == "preset" else get_agents(config_path=get_config_path())
    except ValidationError as exc:
        _raise_config_error(exc)
    entry = next((e for e in pool if e.get("name") == name), None)
    if entry is None:
        raise SubagentNotFoundError(f"no {source} sub-agent named {name!r}", data={"name": name})
    return _as_configs([entry])[0]


async def subagents_test(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
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
    # Exactly the case where `_test_acp` wrote a capability snapshot. What an acp
    # test changes is measured capability, which lives in that store -- and the
    # roster reads `stateful` off a snapshot taken the last time `apply` ran, so
    # leaving the table alone strands the agent on the old measurement:
    # `subagents.list` re-reads the store and reports the new one, while
    # `create_instance` reads the table and refuses the very agent the picker just
    # offered. Same door a build takes after a filesystem change, for the same
    # reason -- a write to durable truth the table has not been re-derived from.
    if result.kind == "acp" and source != "preset":
        try:
            _hot_apply(agent_loop_factory)
        except Exception as exc:  # noqa: BLE001 - the measurement is already recorded
            # The snapshot is on disk, so the verdict the caller asked for is real
            # whatever the table does with it, and the next hot-apply picks it up.
            # Raising would show a successful measurement as a failed test -- and
            # the door re-reads a config file any other client may be writing.
            logger.warning("subagent tested but the agent table was not re-composed: {}", exc)
    return {
        "ok": result.ok,
        "detail": result.detail,
        "elapsed_ms": result.elapsed_ms,
        "reply": result.reply,
        "cancelled": False,
    }


async def subagents_test_cancel(params: dict) -> dict:
    """Cancel an in-flight test. Only the asyncio task is cancelled here.

    What that reaps belongs to the measurement it interrupts: a cli test unwinds
    into the backend, which kills the agent's process group; an acp test unwinds
    into the closes its handshake and its ping each hold, and each of those ends
    the child it launched. Neither leaves a process behind.
    """
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

    async def _build(params: dict) -> dict:
        # Still wrapped like the writes above so the signature stays uniform;
        # nothing is buildable in the product tree, and the handler says so.
        return await subagents_build(params, agent_loop_factory=agent_loop_factory)

    async def _test(params: dict) -> dict:
        # Wrapped like the writes above: an acp test measures capabilities, and
        # the table has to be re-derived from the snapshot it just recorded.
        return await subagents_test(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("subagents.add", _add)
    dispatcher.register("subagents.update", _update)
    dispatcher.register("subagents.toggle", _toggle)
    dispatcher.register("subagents.remove", _remove)
    dispatcher.register("subagents.build", _build)
    dispatcher.register("subagents.test", _test)
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
