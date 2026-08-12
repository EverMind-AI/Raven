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
import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher


_pending_tasks: set[asyncio.Task] = set()


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
    from raven.agent.subagent.instances import get_registry
    from raven.agent.subagent.presets import third_party_subagent_presets
    from raven.agent.subagent.probe import TestResult, probe_all, run_test
    from raven.agent.subagent.test_state import TestStateStore
    from raven.agent.subagent_dag._resume import read_run_reconciled
    from raven.agent.workdir import validate_override
    from raven.config.schema import SubagentsConfig
    from raven.config.update_subagents import (
        get_third_party_subagents,
        reject_unsupported_openai_fields,
        set_third_party_subagents,
    )
    from raven.tui_rpc.errors import RpcError

    async def _list(params: dict) -> dict:
        return {"agents": get_third_party_subagents()}

    async def _set(params: dict) -> dict:
        agents = params.get("agents") or []
        # The schema coerces an openai `readsLocalFiles` away on load, so a
        # client that read its list from here never round-trips one. A payload
        # that still carries it was authored by the caller, who can act on the
        # error -- unlike on load, where raising would stop raven starting.
        reject_unsupported_openai_fields(agents)
        # Validate + write atomically (raises on bad schema / duplicate names;
        # the dispatcher surfaces the error to the client, nothing is applied).
        set_third_party_subagents(agents)
        if agent is not None and hasattr(agent, "apply_third_party_subagents"):
            agent.apply_third_party_subagents(SubagentsConfig(third_party=agents).third_party)
        return {"ok": True, "count": len(agents)}

    async def _presets(params: dict) -> dict:
        return {"presets": third_party_subagent_presets()}

    def _as_configs(entries: list[dict]) -> list[Any]:
        return list(SubagentsConfig(third_party=entries).third_party)

    async def _probe(params: dict) -> dict:
        # Read config from disk rather than from the live AgentLoop: a save lands
        # there, so the next probe reflects it with nothing to invalidate.
        entries = [(cfg, "config") for cfg in _as_configs(get_third_party_subagents())]
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
        pool = third_party_subagent_presets() if source == "preset" else get_third_party_subagents()
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
        rows = get_registry().list_instances(params.get("session_key"))

        manager = _subagent_manager()
        dag_tool = _dag_tool()
        active_run_ids = set(dag_tool.active_run_ids()) if dag_tool is not None else set()
        live_by_session: dict[str, set[tuple[str, str]]] = {}

        def _reconciled(row: dict) -> dict:
            # Never mutate the registry's cached record -- build a new dict when a
            # row needs rewriting, or the next read would see the rewrite as if it
            # had come from disk.
            kind = row.get("kind")
            if kind == "cli" and row.get("status") in ("pending", "running"):
                session_key = row.get("sessionKey", "")
                if session_key not in live_by_session:
                    live_by_session[session_key] = manager.live_handles(session_key) if manager is not None else set()
                live = live_by_session[session_key]
                if (row.get("agent"), row.get("handle")) not in live:
                    return {**row, "status": "interrupted"}
            elif kind == "dag-node" and row.get("status") in ("pending", "running"):
                if row.get("runId") not in active_run_ids:
                    return {**row, "status": "interrupted"}
            return row

        return {"instances": [_reconciled(row) for row in rows]}

    async def _instances_delete(params: dict) -> dict:
        return {"removed": await get_registry().delete_session(params.get("session_key", ""))}

    async def _dag_cancel(params: dict) -> dict:
        tool = _dag_tool()
        if tool is None:
            return {"cancelled": False}
        return {"cancelled": bool(tool.request_cancel(params.get("run_id", "")))}

    async def _dag_get(params: dict) -> dict:
        tool = _dag_tool()
        if tool is None:
            raise RuntimeError("raven.subagents.dag.get requires a live run_subagent_dag tool")
        return {"run": await read_run_reconciled(tool, params.get("run_id", ""), params.get("session_key"))}

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
        return {
            "qr": png,
            "qr_text": qr if qr and png is None else None,
            "connected": running and bool(getattr(ch, "connected", False)),
            "running": running,
        }

    dispatcher.register("raven.channels.qr", _channels_qr)

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
    # reach; the live AgentLoop only adds whether the connection came up and
    # which tools it registered. MCP tools are registered once, lazily, on the
    # first turn that needs them, and there is no teardown that unregisters
    # them, so a config change takes effect on the next gateway restart -- the
    # same contract as skills above, reported the same way.
    from raven.config.update_mcp import get_mcp_servers, set_mcp_servers

    def _mcp_live(name: str, server_names: list[str]) -> dict:
        """Connection state + tool names for one server, read off the registry.

        A tool goes to the *longest* server name that prefixes it: registered
        names are ``mcp_<server>_<tool>`` and the separator is legal inside a
        server name, so with both ``foo`` and ``foo_bar`` configured a plain
        ``startswith`` would let ``foo`` claim ``foo_bar``'s tools.
        """
        if agent is None:
            return {"connected": False, "tools": []}
        tools = []
        for n in getattr(agent.tools, "tool_names", None) or []:
            owner = max(
                (s for s in server_names if n.startswith(f"mcp_{s}_")),
                key=len,
                default=None,
            )
            if owner == name:
                tools.append(n[len(f"mcp_{name}_") :])
        return {
            "connected": bool(getattr(agent, "_mcp_connected", False)) and bool(tools),
            "tools": sorted(tools),
        }

    async def _mcp_list(params: dict) -> dict:
        servers = get_mcp_servers()
        names = [s["name"] for s in servers]
        return {
            "servers": [{**s, **_mcp_live(s["name"], names)} for s in servers],
            # False before the first turn connects them, which is why a server
            # can be configured, valid, and still show no tools.
            "connected": bool(getattr(agent, "_mcp_connected", False)),
        }

    async def _mcp_set(params: dict) -> dict:
        set_mcp_servers(params.get("servers") or [])
        return {"ok": True, "restart_required": True}

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
                deliver=params.get("deliver", True),
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
