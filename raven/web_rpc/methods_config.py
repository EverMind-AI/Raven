"""Raven config-admin RPC methods for the web channel (P4).

These live on the gateway (which has raven + the live runtime), so a config
change is validated, written to ``~/.raven/config.json``, and hot-applied to the
running AgentLoop — no restart. The web backend reaches them by proxying over
the web WebSocket; the browser never talks to the gateway directly.

Registered on the web dispatcher via ``register_config_methods(dispatcher, agent=...)``.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


_pending_tasks: set[asyncio.Task] = set()

# How long ``raven.mcp.set`` waits on the reconcile it started before answering
# anyway. Long enough that an ordinary attach or detach has settled by the time
# the panel reloads, short enough that a handshake parked at the browser step --
# exempt from the 90s progress bound for up to 17 minutes -- does not hold the
# HTTP request open. The reconcile itself is never cancelled by this.
_MCP_APPLY_WAIT = 5.0


def _mcp_wire_entry(item: dict) -> dict:
    """Expand the manual-add wire shorthand into an MCP config entry."""
    entry = dict(item)
    address = str(entry.pop("address", "") or "").strip()
    if not address:
        return entry
    if entry.get("command") or entry.get("url"):
        raise ValueError("an MCP server cannot combine address with command or url")
    if address.startswith(("http://", "https://")):
        entry["url"] = address
        return entry
    argv = shlex.split(address)
    if not argv:
        raise ValueError("an MCP server address cannot be empty")
    entry["command"], entry["args"] = argv[0], argv[1:]
    return entry


def _reexec_process() -> None:  # pragma: no cover - replaces the running process image
    """Re-run the gateway's own argv in place, reloading config (and thus the IM
    channels). Mirrors the ``/restart`` channel command in gateway_commands."""
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _render_qr_png(text: str) -> str | None:
    """A scan payload as a PNG data URI, so the web UI shows the login QR with no
    client-side QR library.

    ``qrcode`` only ships with the QR-login channel extras, so an install that
    enabled such a channel some other way still has to degrade rather than 500:
    None tells the caller to fall back to handing the raw payload to the client.
    """
    import base64
    import io

    try:
        import qrcode
    except ImportError:
        return None

    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _serialize_cron_job(job: Any) -> dict:
    """Flatten a CronJob to a JSON-friendly dict for the web."""
    s = job.schedule
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "schedule": {"kind": s.kind, "expr": s.expr, "every_ms": s.every_ms, "at_ms": s.at_ms, "tz": s.tz},
        "message": job.payload.message,
        "channel": job.payload.channel,
        "to": job.payload.to,
        "next_run_at_ms": job.state.next_run_at_ms,
    }


def register_config_methods(
    dispatcher: "Dispatcher",
    *,
    agent: Any = None,
    cron: Any = None,
    config: Any = None,
    channel_manager: Any = None,
    raven_config: Any = None,
) -> None:
    """Register Raven config-admin methods on the dispatcher.

    - ``raven.subagents.{list,set,presets,instances,probe,test}`` — third-party
      sub-agents. ``probe`` is the free availability check over every configured
      agent and every preset (no subprocess, no chat completion); ``test``
      reaches a real verdict for one entry **by name** — it never accepts a
      command, which would be executed with nothing persisted. ``set``
      validates, writes, and (with ``agent``) hot-applies to the live AgentLoop.
      ``instances.delete`` drops a deleted chat session's instance records
      (their lifetime is the session's, with no time-based expiry). ``instances``
      also reconciles each ``running``/``pending`` row against what is actually
      still alive on ``agent`` (the manager's live handles, the DAG tool's active
      run ids), so a gateway restart or a dropped terminal write never leaves a
      row reported as live when it is not.
    - ``raven.subagents.dag.cancel`` / ``raven.subagents.instances.cancel`` — stop
      one in-flight DAG run / one CLI or DAG-node instance; both no-op to
      ``{"cancelled": False}`` when ``agent`` or the relevant live object is
      missing.
    - ``raven.subagents.dag.{get,node}`` — read one DAG run back out of its
      durable run dir: its structure + per-node state, and one node's rendered
      prompt + (truncated) output. The live ``dag_*`` progress events are never
      replayed, so this is what lets a reloaded page rebuild the graph.
    - ``raven.cron.{list,add,remove}`` — scheduled tasks on the live CronService
      (``cron``); changes take effect immediately (the service owns scheduling).
    - ``raven.session.model.{get,set}`` — a session's own model override
      (``Session.metadata["model"]``), read by the agent loop at turn time.
      ``set`` validates against ``config`` (the same object the running
      ``AgentLoop``/``ResolvingProvider`` was built from) before writing.
    - ``raven.session.workdir.{get,set}`` — a session's own working-directory
      override (``Session.metadata["workdir"]``), read by ``WorkdirResolver``
      ahead of the policy default on the *next* turn. ``set`` refuses while
      the session has a subagent in flight.
    """
    from raven.agent.subagent.backends import agent_meta
    from raven.agent.subagent.instances import get_registry, reconcile_instance_rows
    from raven.agent.subagent.presets import third_party_subagent_presets
    from raven.agent.subagent.probe import TestResult, probe_all, run_test
    from raven.agent.subagent.test_state import TestStateStore
    from raven.agent.subagent_dag._resume import read_run_reconciled
    from raven.agent.subagent_dag.live import cancel_run, live_run_ids
    from raven.agent.workdir import validate_override
    from raven.config.loader import get_config_path
    from raven.config.schema import SubagentsConfig
    from raven.config.update_subagents import (
        _raw_entries,
        get_agents,
        reject_builtin_transport_changes,
        reject_unsupported_acp_fields,
        reject_unsupported_openai_fields,
        set_agents,
    )
    from raven.rpc.errors import RpcError

    async def _list(params: dict) -> dict:
        agents = get_agents()
        # `statefulNames` is a sibling of `agents`, not a field inside each entry:
        # `_set` below runs `reject_unsupported_acp_fields`, which would reject
        # this computed value the moment it showed up inside an acp agent's own
        # object on a later save.
        stateful = [meta.name for cfg in _as_configs(agents) if (meta := agent_meta(cfg)).stateful]
        return {"agents": agents, "statefulNames": stateful}

    async def _set(params: dict) -> dict:
        agents = params.get("agents") or []
        # The schema coerces an openai `readsLocalFiles` away on load, so a
        # client that read its list from here never round-trips one. A payload
        # that still carries it was authored by the caller, who can act on the
        # error -- unlike on load, where raising would stop raven starting.
        reject_unsupported_openai_fields(agents)
        # Same split for the acp kind: warned about on load so a stored config
        # still starts raven, rejected here where the caller owns the value.
        reject_unsupported_acp_fields(agents)
        # A built-in row may be retuned but not re-transported: claiming its name
        # for a cli entry would leave the in-process agent unreachable under a name
        # stored playbooks already use. Refused here, where the caller holds the
        # value, for the same reason the two checks above are.
        reject_builtin_transport_changes(agents, existing=_raw_entries(get_config_path()))
        # Validate + write atomically (raises on bad schema / duplicate names;
        # the dispatcher surfaces the error to the client, nothing is applied).
        set_agents(agents)
        if agent is not None and hasattr(agent, "apply_agents"):
            agent.apply_agents(SubagentsConfig(agents=agents).agents)
        return {"ok": True, "count": len(agents)}

    async def _presets(params: dict) -> dict:
        return {"presets": third_party_subagent_presets()}

    def _as_configs(entries: list[dict]) -> list[Any]:
        return list(SubagentsConfig(agents=entries).agents)

    async def _probe(params: dict) -> dict:
        # Read config from disk rather than from the live AgentLoop: a save lands
        # there, so the next probe reflects it with nothing to invalidate.
        entries = [(cfg, "config") for cfg in _as_configs(get_agents())]
        entries += [(cfg, "preset") for cfg in _as_configs(third_party_subagent_presets())]
        verdicts = TestStateStore().load(entries)
        return {"results": [r.to_wire() for r in await probe_all(entries, verdicts=verdicts)]}

    async def _test(params: dict) -> dict:
        name = params.get("name") or ""
        source = params.get("source") or "config"
        if source not in ("config", "preset"):
            raise ValueError("source must be 'config' or 'preset'")
        # By name only, never a command from the params: a command here would be
        # executed immediately with nothing persisted, which is a wider surface
        # than the config plane that at least leaves a record of what can run.
        pool = third_party_subagent_presets() if source == "preset" else get_agents()
        match = next((e for e in pool if e.get("name") == name), None)
        if match is None:
            return {"result": TestResult(name, source, None, False, "no such subagent", None, 0).to_wire()}
        cfg = _as_configs([match])[0]
        result = await run_test(cfg, source=source)
        # Recorded here rather than inside run_test so probe.py stays free of file
        # I/O and the store stays the single owner of persistence.
        TestStateStore().record(cfg, source, ok=result.ok, detail=result.detail, tested_at_ms=int(time.time() * 1000))
        return {"result": result.to_wire()}

    def _dag_tool() -> Any:
        tools = getattr(agent, "tools", None) if agent is not None else None
        return tools.get("run_subagent_dag") if tools is not None else None

    def _subagent_manager() -> Any:
        return getattr(agent, "subagents", None) if agent is not None else None

    async def _instances(params: dict) -> dict:
        manager = _subagent_manager()
        return {
            "instances": reconcile_instance_rows(
                get_registry().list_instances(params.get("session_key")),
                live_handles=(lambda key: manager.live_handles(key) if manager is not None else set()),
                active_run_ids=(lambda: live_run_ids(agent)),
            )
        }

    async def _instances_delete(params: dict) -> dict:
        return {"removed": await get_registry().delete_session(params.get("session_key", ""))}

    async def _dag_cancel(params: dict) -> dict:
        # Across every instance: a playbook's run is dispatched by the engine's
        # private graph tool, so asking only the registered one reported False
        # for a run that was live -- the page's stop button did nothing.
        return {"cancelled": cancel_run(agent, params.get("run_id", ""))}

    async def _dag_get(params: dict) -> dict:
        tool = _dag_tool()
        if tool is None:
            raise RuntimeError("raven.subagents.dag.get requires a live run_subagent_dag tool")
        return {
            "run": await read_run_reconciled(
                tool,
                params.get("run_id", ""),
                params.get("session_key"),
                live_runs=lambda: live_run_ids(agent),
            )
        }

    async def _dag_node(params: dict) -> dict:
        tool = _dag_tool()
        if tool is None:
            raise RuntimeError("raven.subagents.dag.node requires a live run_subagent_dag tool")
        return {
            "node": await tool.read_node(
                params.get("run_id", ""),
                params.get("node", ""),
                max_output_chars=int(params.get("max_output_chars") or 20000),
                session_key=params.get("session_key"),
            )
        }

    async def _instances_cancel(params: dict) -> dict:
        manager = _subagent_manager()
        if manager is None:
            return {"cancelled": False}
        cancelled = await manager.cancel_by_instance(
            params.get("session_key", ""), params.get("agent", ""), params.get("handle", "")
        )
        return {"cancelled": bool(cancelled)}

    dispatcher.register("raven.subagents.list", _list)
    dispatcher.register("raven.subagents.set", _set)
    dispatcher.register("raven.subagents.presets", _presets)
    dispatcher.register("raven.subagents.probe", _probe)
    dispatcher.register("raven.subagents.test", _test)
    dispatcher.register("raven.subagents.instances", _instances)
    dispatcher.register("raven.subagents.instances.delete", _instances_delete)
    dispatcher.register("raven.subagents.dag.cancel", _dag_cancel)
    dispatcher.register("raven.subagents.dag.get", _dag_get)
    dispatcher.register("raven.subagents.dag.node", _dag_node)
    dispatcher.register("raven.subagents.instances.cancel", _instances_cancel)

    # Per-session model. MUST live here rather than in the web service: the
    # gateway's SessionManager caches Session objects and never re-reads the
    # file, so an out-of-process write is both invisible and liable to be
    # overwritten by the next save().
    def _sessions():
        sessions = getattr(agent, "sessions", None)
        if sessions is None:
            raise RuntimeError("raven.session.model.* requires a live agent loop")
        return sessions

    async def _session_model_get(params: dict) -> dict:
        session = _sessions().get_or_create(params.get("session_key", ""))
        # A session with no override routes its turns on agents.defaults.model, so
        # report that alongside -- a client showing only `model` cannot tell an
        # unset session apart from one with no model at all.
        return {
            "model": session.metadata.get("model"),
            "default": config.agents.defaults.model if config is not None else None,
        }

    async def _session_model_set(params: dict) -> dict:
        key = params.get("session_key", "")
        model = params.get("model")
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                # A blank/non-string value would still pass a bare truthiness
                # check downstream while logging as "set" -- reject here so a
                # value that validates always behaves the way it reads.
                raise ValueError("model must be a non-empty string, or null to clear")
            model = model.strip()
            if config is None:
                raise RuntimeError("raven.session.model.set requires a config to validate against")
            # A pinned agents.defaults.provider makes Config._match_provider
            # return that one provider for every model, so ResolvingProvider
            # would send the picked model to the pinned vendor -- and for a
            # gateway provider, prefix it onto that endpoint. Refuse rather
            # than store a choice that reads as applied but silently is not.
            pinned = config.agents.defaults.provider
            if pinned != "auto":
                raise ValueError(
                    f"per-session models need agents.defaults.provider='auto'; it is pinned to {pinned!r}, "
                    "which routes every model to that one provider"
                )
            if config.serving_provider_for_model(model) is None:
                raise ValueError(f"No configured provider can serve model {model!r}")
        session = _sessions().get_or_create(key)
        if model is None:
            session.metadata.pop("model", None)
        else:
            session.metadata["model"] = model
        _sessions().save(session)
        return {"ok": True, "model": model}

    dispatcher.register("raven.session.model.get", _session_model_get)
    dispatcher.register("raven.session.model.set", _session_model_set)

    async def _session_workdir_get(params: dict) -> dict:
        key = params.get("session_key", "")
        session = _sessions().get_or_create(key)
        return {
            "workdir": session.metadata.get("workdir"),
            "default": str(agent.peek_session_workdir(key)),
        }

    async def _session_workdir_set(params: dict) -> dict:
        key = params.get("session_key", "")
        raw = params.get("workdir")
        # A turn binds its directory at turn start and finishes in the one it
        # started with regardless of a later metadata change, and a sub-agent
        # captures its directory as data when it is spawned. So nothing is
        # stranded by a rebind -- what a rebind does is let in-flight work keep
        # landing in the previous directory while the UI already shows the new
        # one. Clearing rebinds the next turn exactly like setting does, so
        # both branches need the same guard.
        if agent.subagents.has_active(key):
            raise RpcError(f"session {key} has work in flight; retry when it is idle")
        if raw is not None:
            if not isinstance(raw, str) or not raw.strip():
                raise RpcError("workdir must be a non-empty string, or null to clear")
            try:
                resolved = validate_override(raw.strip(), agent.workspace)
                # Same refusal the turn would raise, applied here so a sandboxed
                # gateway rejects the override at set time rather than storing
                # one that every later turn fails on.
                agent.check_workdir_mounted(resolved)
            except ValueError as exc:
                raise RpcError(str(exc)) from exc
            raw = str(resolved)
        session = _sessions().get_or_create(key)
        if raw is None:
            session.metadata.pop("workdir", None)
        else:
            session.metadata["workdir"] = raw
        _sessions().save(session)
        return {"ok": True, "workdir": raw}

    dispatcher.register("raven.session.workdir.get", _session_workdir_get)
    dispatcher.register("raven.session.workdir.set", _session_workdir_set)

    # Channels (IM). Reuses raven.config.update_channels (reflected field specs +
    # secret redaction). Config is applied at gateway startup, so a change takes
    # effect on the next gateway restart (surfaced in the UI).
    from raven.config.update_channels import channel_field_specs, channel_names, get_channel_config, set_channel_fields

    async def _channels_list(params: dict) -> dict:
        channels = []
        for name in channel_names():
            cfg = get_channel_config(name)
            channels.append(
                {
                    "name": name,
                    "enabled": bool(cfg.get("enabled")),
                    "specs": channel_field_specs(name),
                    "config": cfg,
                }
            )
        return {"channels": channels}

    async def _channels_set(params: dict) -> dict:
        set_channel_fields(params.get("name", ""), params.get("fields") or {})
        return {"ok": True, "restart_required": True}

    dispatcher.register("raven.channels.list", _channels_list)
    dispatcher.register("raven.channels.set", _channels_set)

    async def _gateway_restart(params: dict) -> dict:
        """Re-exec the gateway to apply restart-required config (channels/skills).

        Replies before the exec so the caller learns the restart was accepted; the
        short delay lets that reply flush over the web channel before the process
        image is replaced (which drops every connection).
        """

        async def _do() -> None:
            await asyncio.sleep(0.5)
            _reexec_process()

        # Held in a module-level set: the loop keeps only a weak reference, so a
        # task that is merely local can be collected before it ever fires.
        task = asyncio.create_task(_do())
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)
        return {"ok": True}

    dispatcher.register("raven.gateway.restart", _gateway_restart)

    async def _channels_qr(params: dict) -> dict:
        """Login QR for a QR-login channel (weixin/whatsapp) as a PNG data URI,
        plus whether the account is actually paired. Empty when there is no live
        manager, no such channel, or nothing pending (e.g. already paired).

        ``connected`` reads the adapter's own login state, never "the task is
        running and no QR is pending yet" -- both adapters flip ``_running``
        before the first QR is fetched, so inferring it would report an unpaired
        channel as connected during that window and stop the UI from polling.
        ``qr_text`` carries the raw payload for the client to render when the
        gateway has no ``qrcode`` to rasterise with.
        """
        name = params.get("name", "")
        ch = channel_manager.get_channel(name) if channel_manager is not None else None
        qr = getattr(ch, "pending_qr", None) if ch is not None else None
        running = ch is not None and bool(getattr(ch, "is_running", False))
        png = _render_qr_png(qr) if qr else None
        # The rebind snapshot rides this poll rather than needing its own: the
        # page is already asking once a second for the code, and phase + code age
        # are what turn "a picture" into "scan it, it expires, we reissued".
        rebind = ch.rebind_state() if ch is not None and hasattr(ch, "rebind_state") else None
        return {
            "qr": png,
            "qr_text": qr if qr and png is None else None,
            "connected": running and bool(getattr(ch, "connected", False)),
            "running": running,
            "rebind": rebind,
        }

    dispatcher.register("raven.channels.qr", _channels_qr)

    # Rebinding: pairing a QR-login channel to a different account without
    # stopping it. The adapter keeps the account it has until a new scan is
    # confirmed, so these two methods are safe to call on a live channel and a
    # reader who walks away loses nothing.
    def _qr_channel(name: str):
        ch = channel_manager.get_channel(name) if channel_manager is not None else None
        return ch if ch is not None and hasattr(ch, "begin_rebind") else None

    async def _channels_rebind(params: dict) -> dict:
        """Start pairing ``name`` to a different account.

        Answers ``started: False`` with a reason rather than raising: "this
        channel does not support it", "it is not running" and "one is already in
        flight" are all states the page draws, not errors it reports.
        """
        ch = _qr_channel(params.get("name", ""))
        if ch is None:
            return {"started": False, "reason": "unsupported", "phase": "idle"}
        return await ch.begin_rebind()

    async def _channels_rebind_cancel(params: dict) -> dict:
        """Stop waiting for a scan; the account paired now stays paired."""
        ch = _qr_channel(params.get("name", ""))
        if ch is None:
            return {"phase": "idle", "detail": "unsupported"}
        return ch.cancel_rebind()

    dispatcher.register("raven.channels.rebind", _channels_rebind)
    dispatcher.register("raven.channels.rebind.cancel", _channels_rebind_cancel)

    async def _channels_live(params: dict) -> dict:
        """Every channel's runtime state in one round trip, for a client drawing
        a list of them.

        ``connected`` is deliberately three-valued. Only the QR adapters
        (weixin, whatsapp) know whether an account is paired; the rest expose
        ``is_running`` and nothing more. Reporting ``false`` for those would say
        "not connected" about a Telegram bot that is happily serving messages --
        the same lie as reading the config flag, wearing a different hat. Null
        means the channel does not report a pairing, and the caller must not
        render it as a negative.
        """
        out: dict[str, Any] = {}
        for name in channel_names():
            ch = channel_manager.get_channel(name) if channel_manager is not None else None
            if ch is None:
                out[name] = {"running": False, "connected": None, "qr_login": False}
                continue
            reports_pairing = hasattr(type(ch), "connected")
            out[name] = {
                "running": bool(getattr(ch, "is_running", False)),
                "connected": bool(getattr(ch, "connected", False)) if reports_pairing else None,
                "qr_login": hasattr(ch, "pending_qr"),
            }
        return {"channels": out}

    dispatcher.register("raven.channels.live", _channels_live)

    # Tool credentials: the Serper key behind ``web_search``, and the key/model of
    # each media tool. Reuses raven.config.update_tools. Both are read where the
    # AgentLoop is built -- ``web_search`` is withheld without a key, and a media
    # tool is registered only once its model or key is set -- so a write here
    # takes effect on the next gateway restart, which is what ``set`` reports.
    from raven.agent.tools.media_gen import ImageGenerateTool, SpeechGenerateTool, VideoGenerateTool
    from raven.config.update_tools import MEDIA_TOOLS, get_media, get_web_search, set_media, set_web_search

    _media_classes = {"image": ImageGenerateTool, "speech": SpeechGenerateTool, "video": VideoGenerateTool}
    # Asked of a tool built with no config rather than repeated as a literal: the
    # default endpoint lives in media_gen, and a second copy here would keep
    # claiming the old one after that module moved on. All three share it.
    _media_default_base = ImageGenerateTool().api_base

    def _registered_tools() -> set[str]:
        """Names the live loop actually offers the model right now.

        This is the difference between "configured" and "working": the config
        file can hold a key that the running process was started without.
        """
        return set(getattr(getattr(agent, "tools", None), "tool_names", ()) or ())

    def _openrouter_config_key() -> bool:
        """Whether ``providers.openrouter.apiKey`` is set, read from the file.

        Not from ``config``: that object was validated at startup, and a key the
        user has just saved on the credentials page would not be in it.
        """
        from raven.config.update_providers import get_provider_config

        return get_provider_config("openrouter").get("api_key") == "****set****"

    def _web_search_row(registered: set[str]) -> dict:
        stored = get_web_search()
        own = stored["api_key"] == "****set****"
        env = bool(os.environ.get("SERPER_API_KEY"))
        return {
            "kind": "web_search",
            "tool": "web_search",
            "registered": "web_search" in registered,
            "settingPath": "tools.web.search.apiKey",
            "envKey": "SERPER_API_KEY",
            "apiKey": stored["api_key"],
            "maxResults": stored["max_results"],
            # Which source will actually supply the key, in the order the tool
            # resolves them. ``none`` is what the page turns into a prompt.
            "keySource": "own" if own else "env" if env else "none",
        }

    def _media_row(kind: str, registered: set[str]) -> dict:
        cls = _media_classes[kind]
        stored = get_media(kind)
        own = stored["api_key"] == "****set****"
        # The registration rule, restated from AgentLoop: a media tool the user
        # never configured stays off even with an OpenRouter key set for chat.
        configured = own or bool(stored["model"])
        if own:
            source = "own"
        elif _openrouter_config_key():
            source = "openrouter"
        elif os.environ.get("OPENROUTER_API_KEY"):
            source = "env"
        else:
            source = "none"
        return {
            "kind": kind,
            "tool": cls.name,
            "registered": cls.name in registered,
            "configured": configured,
            "settingPath": f"tools.media.{kind}.apiKey",
            "envKey": "OPENROUTER_API_KEY",
            "apiKey": stored["api_key"],
            "apiBase": stored["api_base"],
            "defaultApiBase": _media_default_base,
            "model": stored["model"],
            "defaultModel": cls.default_model,
            "keySource": source,
        }

    async def _tools_list(params: dict) -> dict:
        registered = _registered_tools()
        return {"tools": [_web_search_row(registered), *(_media_row(k, registered) for k in MEDIA_TOOLS)]}

    async def _tools_set(params: dict) -> dict:
        kind = params.get("kind") or ""
        fields = params.get("fields") or {}
        if kind == "web_search":
            set_web_search(fields)
        else:
            set_media(kind, fields)  # raises KeyError on an unknown kind
        return {"ok": True, "restart_required": True}

    dispatcher.register("raven.tools.list", _tools_list)
    dispatcher.register("raven.tools.set", _tools_set)

    # Skills (SkillForge). Reuses raven.config.update_skills. Config is applied at
    # gateway startup, so a change takes effect on the next gateway restart.
    from raven.config.update_skills import get_skillforge, list_skills, set_skillforge

    async def _skills_get(params: dict) -> dict:
        return {"skillforge": get_skillforge()}

    async def _skills_set(params: dict) -> dict:
        set_skillforge(params.get("fields") or {})
        return {"ok": True, "restart_required": True}

    async def _skills_list(params: dict) -> dict:
        return {"skills": list_skills()}

    dispatcher.register("raven.skills.get", _skills_get)
    dispatcher.register("raven.skills.set", _skills_set)
    dispatcher.register("raven.skills.list", _skills_list)

    # EverOS memory models (llm/embedding/rerank/multimodal). These live in
    # ``~/.everos/raven/everos.toml`` (a channel EverOS owns), not raven's
    # config.json, so they get their own reader/writer. Applied when the memory
    # backend boots, i.e. on the next gateway restart. ``test`` reuses the
    # onboard wizard's headless probes (never raise) to validate a key + model.
    from raven.config.update_everos import (
        WRITABLE_SECTIONS,
        clear_everos_section,
        load_everos_config,
        set_everos_section,
    )

    _everos_redact_set = "****set****"
    _everos_redact_empty = "(empty)"

    def _everos_redacted() -> dict:
        cfg = load_everos_config()
        out: dict[str, Any] = {}
        for section in WRITABLE_SECTIONS:
            sec = dict(cfg.get(section) or {})
            sec["api_key"] = _everos_redact_set if sec.get("api_key") else _everos_redact_empty
            out[section] = sec
        return out

    async def _everos_get(params: dict) -> dict:
        """Model sections plus the backend actually in force. Configuring these
        models is inert while ``memory.backend`` names something else (or None,
        which is native Markdown memory), so the caller must be able to say so
        rather than call a fully-filled form ready.

        The backend must come from ``RavenConfig``, not from ``config``.
        ``memory`` is one of ``loader.EXTENSION_KEYS``, so it is popped before
        the base ``Config`` validates and that class never declares it -- read
        off the object the gateway passes, the answer is always None and the
        readiness card can never leave "markdown". The gateway hands its own
        already-loaded copy in as ``raven_config``, which is the one it built
        the live backend from.
        """
        active = raven_config
        if active is None:
            from raven.config.raven import load_raven_config

            active = load_raven_config()
        backend = getattr(getattr(active, "memory", None), "backend", None)
        return {"everos": _everos_redacted(), "backend": backend}

    def _borrowed_key(section: str | None) -> str | None:
        """A sibling role's stored key. The web never sees a key in the clear
        (reads are redacted), so copying one across roles has to happen here.

        Restricted to the sections the writer accepts. ``set_everos_section``
        raises on anything else, and a reader more permissive than the writer
        lets ``reuse_key_from`` name any table that happens to sit in
        everos.toml rather than only the four roles this page owns.
        """
        if not section or section not in WRITABLE_SECTIONS:
            return None
        return (load_everos_config().get(section) or {}).get("api_key")

    def _credential_key(slug: str | None) -> str | None:
        """The key the Credentials page already holds for this provider, so a
        user who configured it there is not asked for the same secret twice."""
        if not slug:
            return None
        from raven.config.update_providers import get_provider_config

        try:
            return (get_provider_config(slug, redact_secrets=False) or {}).get("api_key") or None
        except KeyError:
            return None

    def _trusted_endpoint(base_url: str | None) -> bool:
        """Whether a *stored* key may be sent to this endpoint.

        The probes take ``base_url`` from the request and the key from
        server-side storage, so without this a caller who can reach the config
        API can name any host and have a key it never supplied delivered there
        as a Bearer token -- turning "can edit the config" into "can read the
        provider key". Reads are redacted precisely so that cannot happen.

        Both catalog URL fields count: a vendor's rerank endpoint sometimes
        sits on a different host from its chat endpoint. A caller that supplies
        its own key is not restricted -- the only secret at risk is its own.
        """
        from raven.cli.onboard_everos import _EVEROS_PROVIDERS

        target = (base_url or "").rstrip("/")
        if not target:
            return False
        return any(
            (provider.get(field) or "").rstrip("/") == target
            for provider in _EVEROS_PROVIDERS
            for field in ("base_url", "rerank_base_url")
        )

    def _stored_key(params: dict, section: str) -> str | None:
        """Whichever stored key this request is entitled to: the role's own
        first, then a sibling role, then the provider's credential."""
        return (
            _borrowed_key(section)
            or _borrowed_key(params.get("reuse_key_from"))
            or _credential_key(params.get("credential_provider"))
        )

    def _resolve_key(params: dict, section: str, base_url: str | None) -> str | None:
        """The stored key, withheld when it would leave for an unlisted host.

        Only an actual key triggers the guard: an endpoint with no key to send
        -- a local vLLM, say -- has nothing to leak and must stay probeable at
        whatever address it lives on. See :func:`_trusted_endpoint`.

        The pairing that is not new is exempt, matching the predicate
        :func:`_everos_set` already uses. A role's own key sent to that role's
        own stored ``base_url`` reveals nothing: the two already sit next to
        each other on disk and the memory backend delivers one to the other on
        every boot. Gating it on the catalog instead made a self-hosted
        deployment -- unlisted by nature, and the primary way EverOS runs --
        unprobeable, and left ``set`` accepting what ``test`` refused. The
        guard still applies in full to a sibling key, a credential key, and any
        ``base_url`` that differs from the stored one.
        """
        target = str(base_url or "").rstrip("/")
        own = _borrowed_key(section)
        if own and target:
            stored_url = str((load_everos_config().get(section) or {}).get("base_url") or "").rstrip("/")
            if stored_url and target == stored_url:
                return own
        stored = _stored_key(params, section)
        if stored and not _trusted_endpoint(base_url):
            return None
        return stored

    async def _everos_set(params: dict) -> dict:
        fields = dict(params.get("fields") or {})
        # A redacted placeholder means "leave the stored key untouched", and so
        # does a null; both reduce to "the request carries no key".
        if fields.get("api_key") in (_everos_redact_set, _everos_redact_empty, None):
            fields.pop("api_key", None)
        # An explicit empty string is a request to remove the key, so it must
        # not be read as "none supplied" and quietly filled from elsewhere --
        # that turned "clear it" into "swap in the credentials page's key".
        section = params.get("section", "")
        stored = load_everos_config().get(section) or {}
        typed = "api_key" in fields
        borrowed = ""
        if not typed:
            borrowed = _borrowed_key(params.get("reuse_key_from")) or _credential_key(params.get("credential_provider"))
            if borrowed:
                fields["api_key"] = borrowed

        # The probe guard closed the immediate leak; this closes the one a
        # restart later. Whatever the section holds after this write is what the
        # memory backend sends to its base_url on the next boot, so a key the
        # caller never supplied must not come to rest against an endpoint the
        # caller chose -- whether it arrives by borrowing, or by re-aiming a key
        # that was already stored.
        #
        # Gated on the pairing being *new*, not on the endpoint being unlisted:
        # a self-hosted deployment is unlisted by nature, and editing the model
        # on one has to keep working. Supplying the key in the same request
        # lifts the guard, since that secret is the caller's own to spend.
        target = fields.get("base_url", stored.get("base_url"))
        moved = "base_url" in fields and str(target or "").rstrip("/") != str(stored.get("base_url") or "").rstrip("/")
        if not typed and (borrowed or moved) and (fields.get("api_key") or stored.get("api_key")):
            if not _trusted_endpoint(target):
                raise RuntimeError("enter the api key: a key you did not supply is only written for a catalog endpoint")
        set_everos_section(section, fields)
        return {"ok": True, "restart_required": True}

    async def _everos_clear(params: dict) -> dict:
        clear_everos_section(params.get("section", ""))
        return {"ok": True, "restart_required": True}

    async def _everos_test(params: dict) -> dict:
        section = params.get("section", "")
        fields = params.get("fields") or {}
        model = fields.get("model")
        base_url = fields.get("base_url")
        api_key = fields.get("api_key")
        # A redacted key means "probe with whatever is stored", falling back to
        # the role this one is set to borrow from when it has none of its own.
        if api_key in (_everos_redact_set, _everos_redact_empty, None):
            api_key = _resolve_key(params, section, base_url)
            if api_key is None and _stored_key(params, section):
                # Said plainly rather than probing unauthenticated and echoing
                # the endpoint's 401, which reads as "the key is wrong".
                return {
                    "ok": False,
                    "detail": "enter the api key: a stored key is only sent to a catalog endpoint",
                }

        # Probes run blocking httpx; keep them off the event loop.
        from raven.cli.onboard_everos import (
            _REQUIRED_EMBEDDING_DIM,
            _probe_embedding_dim,
            _probe_everos_chat,
            _probe_rerank,
        )

        if section == "embedding":
            if not base_url or not model:
                return {"ok": False, "detail": "no base_url or model configured"}
            url = base_url.rstrip("/") + "/embeddings"
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            result = await asyncio.to_thread(_probe_embedding_dim, url, headers, model)
            if result == _REQUIRED_EMBEDDING_DIM:
                return {"ok": True, "detail": f"ok ({result}-dim)"}
            if isinstance(result, int):
                return {"ok": False, "detail": f"dimension {result}, EverOS requires {_REQUIRED_EMBEDDING_DIM}"}
            return {"ok": False, "detail": str(result)}
        if section == "rerank":
            ok, detail = await asyncio.to_thread(
                _probe_rerank, model, api_key=api_key, base_url=base_url, rerank_provider=fields.get("provider")
            )
            return {"ok": ok, "detail": detail}
        # llm / multimodal both speak chat/completions.
        ok, detail = await asyncio.to_thread(_probe_everos_chat, model, api_key=api_key, base_url=base_url)
        return {"ok": ok, "detail": detail}

    async def _everos_providers(params: dict) -> dict:
        """The curated provider catalog (name, base_url, which roles it serves,
        rerank specifics) so the web page can offer a provider picker that
        pre-fills the base URL — mirrors the onboard wizard's list.

        ``chat_models`` is the registry's hand-maintained shortlist for that
        provider, so the llm and multimodal pickers start with ids that belong
        to the chosen endpoint instead of one global suggestion borrowed from
        some other provider. That catalogue is chat-only, so the embedding and
        rerank roles have nothing to seed from and rely on a live fetch.
        """

        def _catalog() -> list[dict]:
            # common_models_for imports LiteLLM on first use (seconds), so the
            # whole list is built off the event loop.
            from raven.cli.onboard_everos import _EVEROS_PROVIDERS
            from raven.providers.common_models import common_models_for

            def _shortlist(slug: str) -> list[str]:
                # Registry ids are provider-qualified ("openai/gpt-5.5"); an
                # endpoint wants what follows its own slug, which for a gateway
                # like OpenRouter is itself a vendor-qualified id.
                return [m.removeprefix(f"{slug}/") for m in common_models_for(slug)]

            return [
                {
                    "name": p["name"],
                    "label": p["label"],
                    "label_zh": p.get("label_zh", p["label"]),
                    "base_url": p["base_url"],
                    "supports": sorted(p.get("supports", [])),
                    "rerank_provider": p.get("rerank_provider"),
                    "rerank_base_url": p.get("rerank_base_url"),
                    "chat_models": _shortlist(p["name"]),
                    "has_credential": bool(_credential_key(p["name"])),
                }
                for p in _EVEROS_PROVIDERS
            ]

        return {"providers": await asyncio.to_thread(_catalog)}

    async def _everos_models(params: dict) -> dict:
        """Live model ids a provider exposes for one role, so the page can turn
        the model field into a dropdown. Empty list on any failure (never
        raises); the caller keeps its recommended default and custom entry."""
        section = params.get("section", "llm")
        base_url = params.get("base_url")
        api_key = params.get("api_key")
        if api_key in (_everos_redact_set, _everos_redact_empty, None):
            api_key = _resolve_key(params, section, base_url)

        from raven.cli.onboard_everos import _fetch_everos_models

        models = await asyncio.to_thread(
            _fetch_everos_models,
            base_url,
            api_key,
            section=section,
            provider_name=params.get("provider_name"),
        )
        return {"models": models or []}

    dispatcher.register("raven.everos.get", _everos_get)
    dispatcher.register("raven.everos.set", _everos_set)
    dispatcher.register("raven.everos.clear", _everos_clear)
    dispatcher.register("raven.everos.test", _everos_test)
    dispatcher.register("raven.everos.providers", _everos_providers)
    dispatcher.register("raven.everos.models", _everos_models)

    # MCP servers. The config is the source of truth for what the chat agent can
    # reach; the live AgentLoop adds whether each connection is actually up and
    # which tools it registered. Both come from the connection manager, which is
    # the only thing that knows -- a server can be attached, detached or
    # reconnected while raven runs, so neither the registry's key shapes nor a
    # process-wide "MCP came up once" flag answers this per server.
    from raven.config.update_mcp import get_mcp_servers, set_mcp_servers

    def _mcp_manager():
        """The live connection manager, or None if no loop has built one.

        Read off ``_mcp_manager`` rather than the ``mcp_manager`` property on
        purpose: the property *creates* one, and a read-only status call must
        not bring a subsystem into being as a side effect.
        """
        return getattr(agent, "_mcp_manager", None) if agent is not None else None

    def _mcp_live(name: str, snaps: dict[str, dict], tools_by_server: dict[str, dict[str, str]]) -> dict:
        """Connection state + tool names for one server, per the origin index.

        Takes the two maps rather than reading them, so listing N servers walks
        the index once instead of N times.

        The bare tool name is read from the index, not recovered from the
        registered one. Stripping an ``mcp_<server>_`` prefix looks equivalent
        and is not: sanitising rewrites the server segment, the cap truncates,
        and a collision appends a hash, so a server whose name carries an
        illegal character had every one of its tools shown under the full
        registered name instead.
        """
        snap = snaps.get(name)
        # ``state`` and nothing more. The connection's error string is
        # deliberately not forwarded: the entry already carries an ``error``
        # meaning "this config entry does not validate, saving over it is
        # refused", and a failed handshake landing in that field would tell the
        # user to fix a config that is fine.
        return {
            "connected": bool(snap and snap["connected"]),
            "state": (snap or {}).get("state", "disconnected"),
            "tools": sorted(tools_by_server.get(name, {}).values()),
        }

    async def _mcp_list(params: dict) -> dict:
        manager = _mcp_manager()
        snaps = {s["name"]: s for s in manager.status()} if manager is not None else {}
        configured = get_mcp_servers()
        tools_by_server = manager.tools_by_server() if manager is not None else {}
        servers = [{**s, **_mcp_live(s["name"], snaps, tools_by_server)} for s in configured]
        return {
            "servers": servers,
            # Whether anything is connected right now, not whether the one-shot
            # startup connect happened to have run.
            "connected": any(s["connected"] for s in snaps.values()),
        }

    async def _mcp_set(params: dict) -> dict:
        """Write the server set, then hand it to the live connections.

        No restart: the connection manager attaches, detaches and reconnects
        per server, and touches only the ones whose config actually changed.
        ``applied`` says whether the live loop was reachable to be handed the
        new set -- a gateway with no agent loop yet has nothing to reconcile,
        and the next loop reads the config we just wrote.

        The reconcile is not awaited to completion, and that is the whole point
        of the shape below: it can legitimately take a quarter of an hour. A
        handshake parked at the browser-authorization step is exempt from the
        90s progress bound for as long as it stays parked, up to
        ``OAUTH_FLOW_TIMEOUT + _AUTH_PARK_GRACE`` -- 17 minutes. This call
        answers an HTTP PUT from a settings panel, so awaiting that means the
        browser hangs on someone else's consent page. ``plughub.kick_sync``
        made the same call for the same reason.

        The bounded wait that remains is a courtesy, not the contract: an
        ordinary reconcile settles well inside it, so the panel's own reload
        right after this returns sees the real state instead of ``connecting``.
        A slow one keeps running and the panel shows ``connecting`` until it is
        reopened -- which is honest, and better than a hung request.
        """
        set_mcp_servers([_mcp_wire_entry(item) for item in params.get("servers") or []])
        applied = False
        if agent is not None and hasattr(agent, "apply_mcp_config"):
            from loguru import logger

            from raven.config.loader import load_config

            def _report(t: asyncio.Task) -> None:
                # The task outlives this handler, so nothing else would ever
                # retrieve its exception.
                if not t.cancelled() and t.exception() is not None:
                    logger.warning("MCP config saved but the reconcile failed: {}", t.exception())

            try:
                servers = load_config().tools.mcp_servers
            except Exception as e:  # noqa: BLE001 -- a config we cannot read must not lose the write
                logger.warning("MCP config saved but not applied to the live loop: {}", e)
            else:
                task = asyncio.create_task(agent.apply_mcp_config(servers))
                _pending_tasks.add(task)
                task.add_done_callback(_pending_tasks.discard)
                task.add_done_callback(_report)
                applied = True
                # shield() so the timeout gives up waiting rather than
                # cancelling a reconcile that is mid-handshake.
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=_MCP_APPLY_WAIT)
                except Exception:  # noqa: BLE001 -- a timeout is the expected path; _report owns a failure
                    pass
        return {"ok": True, "applied": applied, "restart_required": False}

    dispatcher.register("raven.mcp.list", _mcp_list)
    dispatcher.register("raven.mcp.set", _mcp_set)

    # Skill operations (Req2/Req3, phase 1): view body + Skill Hub test/search/
    # install. Fetch/read only — no config write, so no restart_required.
    from raven.config import skill_ops

    async def _skills_body(params: dict) -> dict:
        # An installed hub skill lives on disk — its source label stays "hub"
        # but the body is local, so read locally first. Only fall back to a
        # remote hub fetch for catalog items that were never installed
        # (``read_local_body`` returns None for those).
        body = skill_ops.read_local_body(
            name=params.get("name"),
            skill_id=params.get("id"),
            source=params.get("source"),
        )
        if body is not None:
            return body
        if params.get("source") == "hub":
            return await skill_ops.hub_get_body(params.get("id") or params.get("name") or "")
        raise ValueError("skill not found")

    async def _skills_hub_test(params: dict) -> dict:
        return await skill_ops.hub_test(
            endpoint=params.get("endpoint"),
            api_key=params.get("apiKey"),
        )

    async def _skills_hub_search(params: dict) -> dict:
        items = await skill_ops.hub_search(
            params.get("q") or "",
            category=params.get("category"),
            sort=params.get("sort"),
            limit=int(params.get("limit") or 20),
        )
        return {"items": items}

    async def _skills_hub_install(params: dict) -> dict:
        return await skill_ops.hub_install(params.get("id") or "")

    async def _skills_remove(params: dict) -> dict:
        return skill_ops.remove_installed(
            name=params.get("name"),
            skill_id=params.get("id"),
            source=params.get("source"),
        )

    async def _skills_zip(params: dict) -> dict:
        return skill_ops.zip_local(
            name=params.get("name"),
            skill_id=params.get("id"),
            source=params.get("source"),
        )

    dispatcher.register("raven.skills.body", _skills_body)
    dispatcher.register("raven.skills.hub.test", _skills_hub_test)
    dispatcher.register("raven.skills.hub.search", _skills_hub_search)
    dispatcher.register("raven.skills.hub.install", _skills_hub_install)
    dispatcher.register("raven.skills.remove", _skills_remove)
    dispatcher.register("raven.skills.zip", _skills_zip)

    if cron is not None:
        from raven.proactive_engine.schedulers.cron.types import CronSchedule

        async def _cron_list(params: dict) -> dict:
            return {"jobs": [_serialize_cron_job(j) for j in cron.list_jobs(include_disabled=True)]}

        async def _cron_add(params: dict) -> dict:
            sched = params.get("schedule") or {}
            cs = CronSchedule(
                kind=sched.get("kind", "cron"),
                at_ms=sched.get("at_ms"),
                every_ms=sched.get("every_ms"),
                expr=sched.get("expr"),
                tz=sched.get("tz"),
            )
            job = cron.add_job(
                name=params.get("name", "task"),
                schedule=cs,
                message=params.get("message", ""),
                channel=params.get("channel"),
                to=params.get("to"),
            )
            return {"job": _serialize_cron_job(job)}

        async def _cron_remove(params: dict) -> dict:
            return {"removed": bool(cron.remove_job(params.get("job_id", "")))}

        dispatcher.register("raven.cron.list", _cron_list)
        dispatcher.register("raven.cron.add", _cron_add)
        dispatcher.register("raven.cron.remove", _cron_remove)


__all__ = ["register_config_methods"]
