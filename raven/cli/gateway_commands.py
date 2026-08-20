"""Top-level ``gateway`` command.

Spawns the Raven gateway: agent loop + channel manager + cron service
+ heartbeat + sentinel stack (optional). The bulk of the wiring lives in
this command body.

``commands.py`` registers it via :func:`register`.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import typer
from loguru import logger
from rich.console import Console

from raven import __logo__
from raven.cli._helpers import (
    build_model_routing,
    load_runtime_config,
    make_resolving_provider,
    parse_fake_now,
    print_config_migration_notices,
    print_deprecated_memory_window_notice,
)
from raven.cli._plugin_stack import build_plugin_registry, build_plugin_tools, maybe_build_memory_backend
from raven.utils.helpers import sync_workspace_templates

console = Console()


_GATEWAY_IM_CHANNELS: tuple[str, ...] = (
    "whatsapp",
    "telegram",
    "discord",
    "feishu",
    "mochat",
    "dingtalk",
    "email",
    "slack",
    "qq",
    "matrix",
    "wecom",
    "weixin",
)


def _risk_banner(config) -> str | None:
    """Startup banner for the dangerous default combo: no sandbox + a channel
    open to anyone. Returns the banner text, or None when either leg is safe.

    Printed, not gated: gateways run unattended, so blocking on a confirm
    would strand headless restarts -- visibility is the fix here.
    """
    if getattr(config.tools.sandbox, "backend", None) != "none":
        return None

    open_channels = []
    for name in type(config.channels).model_fields:
        section = getattr(config.channels, name, None)
        if section is None or not getattr(section, "enabled", False):
            continue
        if "*" in (getattr(section, "allow_from", None) or []):
            open_channels.append(name)
    if not open_channels:
        return None

    lines = [
        "!! SECURITY WARNING: dangerous configuration combination",
        "   - sandbox.backend = none (agent tools run with full host privileges)",
    ]
    for name in sorted(open_channels):
        lines.append(f"   - channels.{name}.allow_from contains '*' (anyone can command this agent)")
    lines.append("   Restrict senders:  raven channels set <name> --allow-from <id1,id2>")
    lines.append("   Enable a sandbox:  set tools.sandbox.backend to 'auto' or 'boxlite' in your config")
    return "\n".join(lines)


def _build_gateway_channels(config) -> set[str]:
    """Build the ``allowed_channels`` set used by gateway's ``CronService`` — the
    enabled IM channels, and only those (field-driven via
    ``enabled_channel_names``, so a channel added to ``ChannelsConfig`` is
    covered without touching this module).

    The gateway owns cron jobs for its IM channels. It does NOT claim
    ``tui``/``cli`` jobs: those fire in the interactive process that created
    them (the TUI / ``raven agent`` session), so a TUI-set reminder always
    delivers to the TUI rather than racing the gateway and being forwarded to an
    IM channel — fire-at-origin, no trigger-time re-routing. The trade-off is no
    cross-process fallback while that process is down; restoring "fire at origin,
    hand off only after the origin exits" is a deferred cron-delivery-ownership
    design, not this set.

    ``tui`` is deliberately NOT derived from ``gateway.page.enabled`` here. The
    partition has to follow the mount's outcome, not the config's intent:
    ``mount_page`` yields the page to a resident standalone `raven serve`, so
    "enabled but not mounted" is a routine state in which this process has no
    ``tui`` outlet at all. Claiming ``tui`` from config would then burn a whole
    model turn on a reminder the hub drops, and a restart would delete a
    past-due one-shot the serve process could still deliver. The caller adds
    ``tui`` once a live page exists — see the ``page_mount is not None`` block
    in :func:`register`, which runs before ``cron.start()``.
    """
    return config.channels.enabled_channel_names()


def _build_deliverable_store(config):
    """Build the store backing ``deliver_files``, gated on the web channel.

    The web UI is the only surface with a download box for delivered files, so
    the store (and, through it, the tool) exists only when
    ``config.gateway.web.enabled`` is true. Returns ``None`` otherwise, which
    keeps the tool unregistered everywhere else.
    """
    if not config.gateway.web.enabled:
        return None

    from raven.agent.tools._deliverables import DeliverableStore
    from raven.config.paths import get_deliverables_path

    return DeliverableStore(get_deliverables_path())


async def _health_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Answer any request with a 200 ``{"status":"ok"}`` liveness body."""
    try:
        await reader.readline()
        body = b'{"status":"ok"}'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\nConnection: close\r\n\r\n%b" % (len(body), body)
        )
        await writer.drain()
    finally:
        writer.close()


def register(app: typer.Typer) -> None:
    """Attach the ``gateway`` command to ``app``."""

    @app.command()
    def gateway(
        port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
        workspace: str | None = typer.Option(
            None,
            "--workspace",
            "-w",
            help="Root for per-channel working directories (default: ~/.raven/tmp)",
        ),
        home: str | None = typer.Option(
            None,
            "--home",
            help="Agent home directory (memory, skills, transcripts)",
        ),
        verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
        config: str | None = typer.Option(None, "--config", help="Path to config file"),
        fake_now: str | None = typer.Option(
            None,
            "--fake-now",
            help=(
                "ISO-8601 timestamp to freeze 'now' for the Sentinel stack. "
                "Used by the proactivity-eval subprocess harness; leave unset "
                "for normal operation."
            ),
        ),
    ):
        """Start the Raven gateway."""
        from raven.agent.loop import AgentLoop
        from raven.agent.loop.recovery import limits_from_defaults
        from raven.agent.workdir import WorkdirPolicy, WorkdirResolver, validate_override
        from raven.channels.manager import ChannelManager
        from raven.config.paths import get_cron_dir
        from raven.config.raven import load_raven_config
        from raven.proactive_engine.schedulers.cron.service import CronService
        from raven.proactive_engine.schedulers.heartbeat.service import HeartbeatService
        from raven.session.manager import SessionManager

        # load_runtime_config must run FIRST: it calls set_config_path() so
        # that subsequent load_raven_config() reads from --config, not the
        # default ~/.raven/config.json. Otherwise skill_forge / sentinel
        # from --config are silently ignored.
        config = load_runtime_config(config, home=home)

        from raven.cli._log_file import redirect_loguru_to_file

        log_cfg = config.gateway.log
        log_path = redirect_loguru_to_file(
            "gateway.log",
            rotation=log_cfg.rotation,
            retention=log_cfg.retention,
            file_level="DEBUG" if verbose else log_cfg.level,
            terminal_level="DEBUG" if verbose else log_cfg.console_level,
        )

        from raven.cli._gateway_lock import GatewayAlreadyRunningError, acquire, publish_web_endpoint

        # Held for the whole process; closing/GC of this handle releases the lock.
        try:
            _lock_handle = acquire(now=time.time())
        except GatewayAlreadyRunningError as exc:
            since = datetime.fromtimestamp(exc.info.started_at).strftime("%Y-%m-%d %H:%M:%S")
            console.print(
                f"[red]✗[/red] Raven gateway already running for this instance "
                f"(pid {exc.info.pid}, since {since}).\n"
                f"  Stop it first, or use --config to run a separate instance."
            )
            raise typer.Exit(code=1)

        ec_config = load_raven_config()
        sentinel_cfg = ec_config.sentinel
        skill_forge_cfg = ec_config.skill_forge
        print_deprecated_memory_window_notice(config)
        print_config_migration_notices()
        port = port if port is not None else config.gateway.port

        console.print(f"{__logo__} Starting Raven gateway on port {port}...")
        console.print(f"[dim]📝 Logs → {log_path}[/dim]")
        banner = _risk_banner(config)
        if banner is not None:
            console.print(banner, style="bold red", markup=False)
        sync_workspace_templates(config.workspace_path)
        provider = make_resolving_provider(config)
        session_manager = SessionManager(config.workspace_path)
        session_root = None
        if workspace:
            try:
                # Guards the root landing inside a protected subtree (e.g.
                # -w <home>/skills, which would put every channel's files under
                # skills/), and agent home itself, which would scatter channel
                # directories through the tree the default root exists to keep
                # them out of.
                session_root = validate_override(workspace, config.workspace_path)
            except ValueError as e:
                raise typer.BadParameter(str(e)) from e
        workdir_resolver = WorkdirResolver(
            WorkdirPolicy.PER_CHANNEL,
            agent_home=config.workspace_path,
            session_root=session_root,
            sessions=session_manager,
            channel_workspaces=config.channel_workspaces(),
        )

        # Create cron service first (callback set after agent creation).
        #
        # Restrict to channels gateway has adapters for. This prevents the
        # gateway from racing the TUI and stealing tui-bound reminders that
        # the TUI can deliver but gateway can't (gateway has no tui outlet).
        # Without this, you'd see "Unknown channel: tui" warnings + lost TUI
        # reminders when both processes are running.
        cron_store_path = get_cron_dir() / "jobs.json"
        gateway_channels = _build_gateway_channels(config)
        cron = CronService(cron_store_path, allowed_channels=gateway_channels)

        # Create model router (and, for the knn backend, wrap the provider).
        router, provider = build_model_routing(config, provider)

        # Build Sentinel stack (enabled iff sentinel.enabled).
        # NudgeInjector serves as the AgentLoop response_modifier;
        # SentinelRunner.on_user_inbound tracks reply engagement.
        # These bindings must happen BEFORE AgentLoop construction.
        from raven.cli._proactive_stack import (
            attach_sentinel_decision_consumer,
            attach_sentinel_spawn,
            build_sentinel_stack,
        )

        sentinel_runner, sentinel_response_modifier, sentinel_on_user_inbound = build_sentinel_stack(
            config,
            sentinel_cfg,
            session_manager,
            provider,
            now_fn=parse_fake_now(fake_now),
        )

        # Anti-runaway reset: genuine user activity on a (channel, chat_id)
        # zeroes the silent-fire counters of the jobs bound to it. Chained
        # after the Sentinel engagement hook, not replacing it.
        from raven.cli._cron_handler import chain_cron_activity_reset

        on_user_inbound = chain_cron_activity_reset(cron, inner=sentinel_on_user_inbound)

        # Gateway-side memory-backend wiring. Mirrors the REPL
        # bootstrap (cli/agent_commands.py). Returns ``None`` when no
        # plugin contributes the configured backend — AgentLoop falls
        # back to its legacy ``self.memory`` path. Lifecycle (start /
        # stop) lands inside the run-loop coroutine below.
        # One registry shared by both contribution points, so plugins are
        # discovered and activated once rather than per consumer.
        plugin_registry = build_plugin_registry(ec_config)
        backend = maybe_build_memory_backend(config.workspace_path, ec_config, registry=plugin_registry)
        # The ``tools`` contribution point, at parity with agent / tui. Without
        # it a plugin-contributed tool (everos's understand_media, which reads
        # the attachments only the web UI can upload) is missing from the web UI
        # while present in both terminal surfaces.
        plugin_tools = build_plugin_tools(config.workspace_path, ec_config, registry=plugin_registry)

        # Built before AgentLoop (registration happens in its constructor) so
        # the same instance can also be handed to the web JSON-RPC server
        # further down, once it is built inside run().
        deliverables = _build_deliverable_store(config)

        # Create agent with cron service
        agent = AgentLoop(
            provider=provider,
            now_fn=parse_fake_now(fake_now),
            workspace=config.workspace_path,
            model=config.agents.defaults.model,
            max_iterations=config.agents.defaults.max_tool_iterations,
            empty_recovery=limits_from_defaults(config.agents.defaults),
            context_window_tokens=config.agents.defaults.context_window_tokens,
            max_concurrent_subagents=config.agents.defaults.max_concurrent_subagents,
            max_subagent_spawns_per_hour=config.agents.defaults.max_subagent_spawns_per_hour,
            brave_api_key=config.tools.web.search.api_key or None,
            jina_api_key=config.tools.web.jina_api_key or None,
            web_proxy=config.tools.web.proxy or None,
            media_config=config.effective_media_config(),
            deep_research_config=config.tools.deep_research,
            exec_config=config.tools.exec,
            cron_service=cron,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            session_manager=session_manager,
            workdir_resolver=workdir_resolver,
            mcp_servers=config.tools.mcp_servers,
            disabled_tools=config.tools.disabled_tools,
            tool_search_config=config.tools.tool_search,
            sandbox_config=config.tools.sandbox,
            channels_config=config.channels,
            deliverables=deliverables,
            router=router,
            skill_forge_config=skill_forge_cfg,
            context_config=ec_config.context,
            runtime_config=ec_config.runtime,
            # Gateway sessions are inherently multi-turn (each RPC session
            # gets a key and can receive a recovery block on its next call).
            interactive=True,
            response_modifier=sentinel_response_modifier,
            on_user_inbound=on_user_inbound,
            backend=backend,
            plugin_tools=plugin_tools,
            memory_config=ec_config.memory,
            skill_forge_router_config=ec_config.skill_forge.router,
            agents=config.subagents.agents,
            playbook_config=config.playbooks,
        )
        agent.configure_personalization(config.agents.defaults.enable_personalization)

        # Sentinel's ProactiveSpawn wraps the AgentLoop's SubagentManager; wire it
        # now that agent is constructed.
        attach_sentinel_spawn(sentinel_runner, agent)
        # Wire DecisionRouter / ActionExecutor / DecisionConsumer
        # (ActionExecutor needs agent.tools + agent.subagents, so this also
        # has to happen post-AgentLoop construction).
        attach_sentinel_decision_consumer(sentinel_runner, agent, sentinel_cfg=sentinel_cfg)

        # ChannelManager must be built before make_on_cron_job — the
        # closure captures channels.enabled_channels for trigger-time
        # delivery resolution.
        channels = ChannelManager(config)

        # Late-bind so the discovery resolver can read enabled_channels.
        if sentinel_runner is not None:
            sentinel_runner.set_channel_manager(channels)

        from raven.cli._cron_handler import make_on_cron_job

        # Event wake: in-process producers (cron completions) can end the
        # heartbeat sleep early instead of waiting for the next interval.
        # Busy check covers spine-dispatched turns only (user messages) —
        # exactly the lane a wake must never compete with.
        hb_cfg = config.gateway.heartbeat
        wake = None
        system_events = None
        if hb_cfg.event_wake:
            from raven.proactive_engine.system_events import SystemEventQueue
            from raven.proactive_engine.wake import WakeScheduler

            system_events = SystemEventQueue()
            wake = WakeScheduler(
                is_busy=lambda: agent.is_processing,
                min_interval_s=hb_cfg.event_wake_min_interval_s,
            )
            agent.on_turn_complete.append(wake.on_turn_complete)

        def _pick_heartbeat_target() -> tuple[str, str]:
            """Pick a routable channel/chat target for heartbeat-triggered messages."""
            enabled = set(channels.enabled_channels)
            # Prefer the most recently updated non-internal session on an enabled channel.
            for item in session_manager.list_sessions():
                key = item.get("key") or ""
                if ":" not in key:
                    continue
                channel, chat_id = key.split(":", 1)
                if channel in {"cli", "system"}:
                    continue
                if channel in enabled and chat_id:
                    return channel, chat_id
            # Fallback keeps prior behavior but remains explicit.
            return "cli", "direct"

        # The heartbeat service is assembled inside run() (it submits HEARTBEAT
        # turns through the gateway scheduler, which is built there).

        if channels.enabled_channels:
            console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
        else:
            console.print("[yellow]Warning: No channels enabled[/yellow]")

        cron_status = cron.status()
        if cron_status["jobs"] > 0:
            console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

        console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

        if sentinel_runner is not None:
            console.print(
                f"[green]✓[/green] Sentinel: tick every {sentinel_runner.interval_s}s "
                f"(inject={sentinel_cfg.inject_enabled}, defer={sentinel_cfg.defer_enabled})"
            )
        else:
            console.print("[dim]Sentinel: disabled (set sentinel.enabled=true to activate)[/dim]")

        async def _delayed_discover_trigger_drain():
            """Drain CLI-queued ``discover-now`` triggers ~2s after gateway
            startup. The delay lets each channel adapter build its
            sendable client before the dispatcher gets the drain msg —
            otherwise the feishu adapter drops with "client not initialized"."""
            if sentinel_runner is None:
                return
            await asyncio.sleep(2)
            try:
                await sentinel_runner.consume_pending_triggers()
            except Exception as exc:
                logger.warning(
                    "startup discover-trigger drain failed: {}: {}",
                    type(exc).__name__,
                    exc,
                )

        async def run():
            health_server = None
            gw_teardown = None
            heartbeat = None
            question_broker = None
            web_server = None
            web_teardown = None
            page_mount = None
            # Bring the memory backend online before any turn
            # runs. ``backend`` is ``None`` when no plugin is wired;
            # the start / stop awaits are then skipped entirely.
            from loguru import logger as _logger  # local import: gateway

            # doesn't have a module-
            # level logger
            if backend is not None:
                try:
                    await backend.start()
                except Exception:
                    _logger.exception(
                        "memory backend start failed; continuing with legacy memory path",
                    )
            try:
                # Spine assembly for the gateway's host sources (cron submits
                # through it, replies route to channels via a per-channel outlet).
                # Built here, inside the running loop, not in the sync command
                # prologue: Scheduler pins its home loop at construction (submit
                # must come from that loop), and the prologue has no loop yet.
                from raven.cli._gateway_spine import build_gateway

                gw_scheduler, gw_hub, gw_readback_texts, gw_sources, gw_teardown = build_gateway(
                    agent,
                    channels.channels,
                    user_pool=config.gateway.user_pool,
                    system_pool=config.gateway.system_pool,
                    send_max_retries=config.gateway.send_max_retries,
                )

                # Web-app channel (ui-webui P1): its own streaming spine + a
                # WebSocket JSON-RPC server the web backend connects to as a
                # client, built alongside the gateway's spine and sharing this
                # agent_loop. The gateway runner is non-streaming (proactive
                # replies are one Text); the web UI wants token streaming, so it
                # gets its own streaming runner (build_web). Built here, BEFORE
                # the proactive wiring, so proactive producers can target it.
                web_cfg = config.gateway.web
                web_scheduler = None
                web_hub = None
                # Always on now, not only when the operator turned it on. This
                # is the only place a live adapter can be asked whether a
                # channel is actually paired, and `raven serve` has to be able
                # to ask -- otherwise every surface but ui-webui reports the
                # config file's `enabled` flag as if it were a connection.
                #
                # Opted in (`gateway.web.enabled`) keeps the configured port and
                # token, because ui-webui is pointed at them. Otherwise the port
                # is probed forward from the default and the token is minted for
                # this boot: neither is written to config, so no stale secret
                # outlives the process. Loopback either way.
                import secrets

                from raven.rpc.dispatcher import Dispatcher
                from raven.rpc.methods.turn import clear_active
                from raven.rpc.subscriptions import SubscriptionEmitter
                from raven.rpc.transports.ws import pick_port
                from raven.web_rpc.methods import register_web_methods
                from raven.web_rpc.server import WebSocketRpcServer
                from raven.web_rpc.spine import build_web

                web_host = web_cfg.host if web_cfg.enabled else "127.0.0.1"
                web_port = web_cfg.port if web_cfg.enabled else await pick_port(web_cfg.port)
                # Never unauthenticated: the server only checks a token when
                # one is set, and this port can drive the agent.
                web_token = (web_cfg.auth_token or None) if web_cfg.enabled else secrets.token_urlsafe(24)

                web_readback_texts: dict[str, str] = {}
                web_server = WebSocketRpcServer(
                    host=web_host,
                    port=web_port,
                    auth_token=web_token,
                    deliverables=deliverables,
                )
                web_emitter = SubscriptionEmitter(send_frame=web_server.broadcast)
                # One map, two readers: `turn.send` records a direct chat's
                # addressee here and the outlet reads it back to tag that
                # lane's events with it. Built here so both get the same
                # object, as raven/rpc/bootstrap.py does for the TUI.
                web_direct_targets: dict[str, dict[str, str]] = {}
                web_scheduler, web_hub, web_turn_ids, web_teardown = build_web(
                    agent,
                    web_emitter,
                    on_turn_end=clear_active,
                    readback_texts=web_readback_texts,
                    direct_targets=web_direct_targets,
                    user_pool=config.gateway.user_pool,
                    system_pool=config.gateway.system_pool,
                )
                web_dispatcher = Dispatcher()
                register_web_methods(
                    web_dispatcher,
                    emitter=web_emitter,
                    scheduler=web_scheduler,
                    turn_ids=web_turn_ids,
                    direct_targets=web_direct_targets,
                    agent=agent,
                    cron=cron,
                    config=config,
                    channel_manager=channels,
                    raven_config=ec_config,
                )
                web_server.bind(web_dispatcher)
                # Published beside the lock rather than in config: a client finds
                # the gateway the same way `doctor` does, and the credential dies
                # with the process instead of outliving it in a settings file.
                publish_web_endpoint(web_host, web_port, web_token or "")

                # Fan run_subagent_dag progress (dag_run_started / _node_updated
                # / _run_completed) to the turn's conversation on the web
                # channel, as a "custom" wire event the service translates to
                # an AgentScope CustomEvent (lights up the web UI's DAG graph).
                async def _dag_progress_to_web(conversation: str, name: str, payload: dict) -> None:
                    await web_emitter.emit(conversation, {"type": "custom", "name": name, "payload": payload})

                agent.set_dag_progress_sink(_dag_progress_to_web)

                # Fan per-turn SkillForge-injected skills to the web UI's
                # skill panel as a "skills_injected" custom event (same
                # translation path as DAG progress above).
                async def _skills_to_web(conversation: str, name: str, payload: dict) -> None:
                    await web_emitter.emit(conversation, {"type": "custom", "name": name, "payload": payload})

                agent.set_skills_sink(_skills_to_web)
                console.print(f"[green]✓[/green] Web channel: ws://{web_cfg.host}:{web_cfg.port}/ws")

                # Proactive target (cron / sentinel / heartbeat / subagent /
                # deep_research). Single-user + web-primary (P1.2): when the web
                # channel is on, proactive output is routed to it (source.channel
                # == "web") so it surfaces in the web UI; otherwise it stays on the
                # gateway spine (IM/cli). Channel *inbound* and the ask_user
                # round-trip stay on the gateway spine regardless (P1.2 does not
                # bridge ask_user to the web client yet).
                if web_cfg.enabled:
                    pro_submit = web_scheduler.submit
                    pro_hub = web_hub
                    pro_readback = web_readback_texts
                    pro_channel = "web"
                    pro_heartbeat_target: tuple[str, str] | None = ("web", "default")
                else:
                    pro_submit = gw_scheduler.submit
                    pro_hub = gw_hub
                    pro_readback = gw_readback_texts
                    pro_channel = "cli"
                    pro_heartbeat_target = None

                cron.on_job = make_on_cron_job(
                    submit=pro_submit,
                    readback_texts=pro_readback,
                    default_channel=pro_channel,
                    system_events=system_events,
                    wake=wake,
                    cron_service=cron,
                )
                # Missed-reminder observer: past-due tui/cli one-shots whose
                # session closed before firing surface once through the same
                # system-event -> heartbeat wake path as cron completions.
                # Needs the event-wake plumbing; without it there is no sink,
                # so the observer stays off (as it does with notify_missed
                # false). Wired before cron.start() — the start-time check is
                # the first observation pass.
                if system_events is not None and wake is not None and config.cron.notify_missed:
                    from raven.cli._cron_handler import make_on_missed_foreign

                    cron.on_missed_foreign = make_on_missed_foreign(system_events, wake)

                from raven.spine import ChatType, Origin, Source, TurnRequest

                async def on_heartbeat_execute(tasks: str) -> str:
                    """Run heartbeat tasks as a HEARTBEAT-origin turn; the
                    hub delivers the reply to the picked channel. Deliver-only — no
                    one reads the reply back (HeartbeatService is wired on_notify=
                    None, the hub already delivered), so the return is unused."""
                    channel, chat_id = pro_heartbeat_target or _pick_heartbeat_target()
                    req = TurnRequest(
                        origin=Origin.HEARTBEAT,
                        source=Source(
                            channel=channel,
                            chat_id=chat_id,
                            sender_id="heartbeat",
                            chat_type=ChatType.DM,
                        ),
                        text=tasks,
                        conversation="heartbeat",
                    )
                    await pro_submit(req).result()
                    return ""

                heartbeat = HeartbeatService(
                    workspace=config.workspace_path,
                    provider=provider,
                    model=agent.model,
                    on_execute=on_heartbeat_execute,
                    on_notify=None,
                    interval_s=hb_cfg.interval_s,
                    enabled=hb_cfg.enabled,
                    wake=wake,
                    system_events=system_events,
                )
                # Late-bind the spine submit into Sentinel's turn-injection
                # sites (built in the sync prologue, before the scheduler
                # existed): the supersede notice (task_discoverer) and the
                # menu-pick execution (decision_consumer's ActionExecutor).
                if sentinel_runner is not None and sentinel_runner.dispatcher is not None:
                    sentinel_runner.dispatcher.set_post(pro_hub.post)
                if sentinel_runner is not None and sentinel_runner.task_discoverer is not None:
                    sentinel_runner.task_discoverer.set_submit(pro_submit)
                if (
                    agent.decision_consumer is not None
                    and getattr(agent.decision_consumer, "executor", None) is not None
                ):
                    agent.decision_consumer.executor.set_submit(pro_submit)
                # Subagent result re-injection submits a SUBAGENT-origin turn.
                agent.subagents.set_submit(pro_submit)
                # Deep research (channel/async) delivers its finished answer back
                # via a deliver_text turn; wiring submit here (gateway only) is
                # what flips the tool from its synchronous path to the async one.
                # Goes through the loop so a manager built later by promotion (a
                # mid-session enable) inherits the handle too, not just this one.
                agent.set_deep_research_submit(pro_submit)

                # ask_user round-trip on the channel side: the QuestionBroker
                # renders the agent's clarify.request as an outbound Text to the
                # conversation's channel; the inbound gate (below) routes the
                # user's next message back via reply(). The question fires mid-turn,
                # so the live turn's real inbound Source is still in gw_sources
                # (keyed by conversation id) — reuse it so a topic / thread address
                # is exact, rather than reconstructing it from the conversation id.
                from raven.rpc.question_broker import QuestionBroker
                from raven.spine import Text as _Text

                async def _question_to_channel(frame: dict) -> None:
                    params = frame.get("params", {})
                    qcid = params.get("conversation_id", "")
                    source = gw_sources.get(qcid)
                    if source is None:
                        logger.warning(
                            "ask_user question for {} has no live source — dropping",
                            qcid,
                        )
                        return
                    body = params.get("question", "")
                    choices = params.get("choices") or []
                    if choices:
                        body += "\n" + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(choices))
                    await gw_hub.dispatch(_Text(content=body, source=source))

                question_broker = QuestionBroker(send_frame=_question_to_channel)
                # Wire the broker into the mid-turn askers. deep_research goes
                # through the loop so a tool built later by promotion (a mid-session
                # enable) inherits the broker too, not just the startup one.
                if (ask_tool := agent.tools.get("ask_user")) is not None and hasattr(ask_tool, "set_broker"):
                    ask_tool.set_broker(question_broker)
                agent.set_deep_research_broker(question_broker)

                # The served page, on this same engine. Mounted after the broker
                # wiring above on purpose: build_rpc_stack rebinds the streaming
                # sinks (dag progress, mcp events) to the page's emitter, and
                # while the page is mounted it is the surface that renders those.
                # The question brokers are re-bound below to a routing shim over
                # both surfaces, not left last-write-wins. mount_page returns
                # None when a live standalone `raven serve` already owns the
                # page; the IM round-trip above is then the wiring, untouched.
                if config.gateway.page.enabled:
                    from raven.cli._gateway_page import mount_page

                    try:
                        page_mount = await mount_page(agent, config.gateway.page.port)
                    except OSError as exc:
                        logger.warning("page mount failed ({}); gateway continues without the page", exc)
                if page_mount is not None:
                    # One shared loop, two question surfaces. build_rpc_stack
                    # bound the page's broker over the channel broker wired
                    # above (AskUserTool._broker is process-wide, last write
                    # wins), which would leave an IM ask_user emitting to the
                    # browser and its answer starting a fresh turn. Re-bind a
                    # shim that routes by conversation: page/tui sessions
                    # (`tui:<id>`) to the page broker, everything else back to
                    # the channel broker. deep_research clarify rides the same
                    # shim, through the loop for the same promotion reason as
                    # above.
                    from raven.rpc.question_broker import RoutingQuestionBroker

                    routed_broker = RoutingQuestionBroker(page=page_mount.question_broker, channel=question_broker)
                    if (ask_tool := agent.tools.get("ask_user")) is not None and hasattr(ask_tool, "set_broker"):
                        ask_tool.set_broker(routed_broker)
                    agent.set_deep_research_broker(routed_broker)
                    # Route channel="tui" outbounds from the gateway's own
                    # spines (a tui cron job's reply, a subagent announce whose
                    # conversation lives on the page) to the page.
                    gw_hub.register(page_mount.outlet)
                    web_hub.register(page_mount.outlet)
                    # Only now is this process a tui surface, so only now may it
                    # claim tui cron jobs. Deciding the partition here rather
                    # than from gateway.page.enabled is what keeps a gateway
                    # that yielded the page to a standalone `raven serve` from
                    # running a page-set reminder the hub then has to drop, and
                    # from deleting a past-due one-shot the serve can still
                    # deliver. _owns_channel reads the set live and cron.start()
                    # (which sweeps past-due one-shots) is still ahead.
                    cron.allowed_channels.add("tui")
                    # A delivering tui cron job's reply also fans out as a
                    # cron.delivered event to the page's sessions, the same
                    # path serve and the TUI use. Only tui jobs: an IM job's
                    # reply is already delivered on its own channel, and the
                    # page has no claim on it.
                    from raven.cli.tui_commands import _build_cron_callback_spine

                    cron.on_job = _build_cron_callback_spine(
                        cron.on_job, page_mount.emitter, default_channel=pro_channel
                    )
                    console.print(f"[green]✓[/green] Page: {page_mount.url} (rpc: {page_mount.url}/rpc)")

                # Channel inbound runs through the spine: a permitted
                # message is submitted as a USER turn. /stop and /restart are
                # control commands (the bus drainer's job) — intercepted here, not
                # submitted as turns (else the agent would reply to the text). cid
                # matches the lane key (conversation or channel:chat_id), the same
                # session key the bus path's _handle_stop used.
                from dataclasses import replace

                from raven.spine import Text
                from raven.spine.turn import BusyPolicy

                async def _inbound_dispatch(req) -> None:
                    cmd = req.text.strip().lower()
                    cid = req.conversation or f"{req.source.channel}:{req.source.chat_id}"
                    if cmd == "/stop":
                        stopped = gw_scheduler.cancel_conversation(cid)
                        stopped += await agent.subagents.cancel_by_session(cid)
                        content = f"Stopped {stopped} task(s)." if stopped else "No active task to stop."
                        await gw_hub.dispatch(Text(content=content, source=req.source))
                    elif cmd == "/restart":
                        await gw_hub.dispatch(Text(content="Restarting...", source=req.source))

                        async def _do_restart() -> None:
                            import os
                            import sys

                            await asyncio.sleep(1)
                            os.execv(sys.executable, [sys.executable] + sys.argv)

                        asyncio.create_task(_do_restart())
                    elif question_broker.pending_req(cid) is not None:
                        # This conversation is blocked on an ask_user question —
                        # route the answer to the broker (resolving the awaiting
                        # tool) instead of starting or injecting a turn.
                        question_broker.reply(cid, req.text)
                    elif gw_scheduler.has_inflight(cid):
                        # A turn is already running this conversation — submit as
                        # BusyPolicy.INJECT so the loop merges this message at its
                        # next iteration instead of queuing a fresh turn.
                        gw_scheduler.submit(replace(req, busy=BusyPolicy.INJECT))
                    else:
                        gw_scheduler.submit(req)  # fire-and-forget (no readback)

                for _ch in channels.channels.values():
                    _ch.intake.set_submit(_inbound_dispatch)

                await cron.start()
                await heartbeat.start()
                if sentinel_runner is not None:
                    await sentinel_runner.start()
                try:
                    health_server = await asyncio.start_server(_health_handler, "127.0.0.1", port)
                    console.print(f"[green]✓[/green] Health: http://127.0.0.1:{port}/health")
                except OSError as exc:
                    logger.warning(
                        "health endpoint unavailable on 127.0.0.1:{} ({}); gateway continues without it",
                        port,
                        exc,
                    )
                coros = [
                    agent.run(),
                    channels.start_all(),
                    _delayed_discover_trigger_drain(),
                ]
                if health_server is not None:
                    coros.append(health_server.serve_forever())
                if web_server is not None:
                    coros.append(web_server.serve_forever())
                await asyncio.gather(*coros)
            except KeyboardInterrupt:
                console.print("\nShutting down...")
            finally:
                if health_server is not None:
                    health_server.close()
                # Stop the proactive producers before tearing down the scheduler
                # they submit through: a cron timer firing during teardown would
                # otherwise submit to an already-shut scheduler.
                cron.stop()
                if heartbeat is not None:
                    heartbeat.stop()
                if sentinel_runner is not None:
                    await sentinel_runner.stop()
                if question_broker is not None:
                    question_broker.cancel_all()  # release any turn blocked on ask_user
                if page_mount is not None:
                    await page_mount.teardown()
                if web_server is not None:
                    await web_server.stop()
                from raven.agent.acp.client import begin_drain
                from raven.agent.acp.pool import close_pool

                # Before anything tears a transport down: an ACP connection
                # closed first fails every pending turn with a connection error,
                # which records as a failure rather than as the stop it is. A
                # CLI subagent's process group is detached from the gateway's
                # own (start_new_session=True), so nothing else reaches it
                # either. Draining first, because the pool close below kills
                # every ACP server anyway and no cancelled turn is worth waiting
                # on when its process is about to go.
                begin_drain()
                await agent.subagents.cancel_all()
                if web_teardown is not None:
                    await web_teardown()
                if gw_teardown is not None:
                    await gw_teardown()
                # ACP agents are launched with start_new_session, so they do not
                # get this process's signals and outlive it unless the pool is
                # closed.
                await close_pool()
                await agent.close_mcp()
                agent.stop()
                await channels.stop_all()
                # Stop the memory-backend plugin last so any
                # in-flight backend.store / backend.feedback calls
                # spawned during AgentLoop teardown can complete.
                if backend is not None:
                    try:
                        await agent.drain_backend_stores()
                        await backend.stop()
                    except Exception:
                        _logger.exception(
                            "memory backend stop failed; continuing shutdown",
                        )

        asyncio.run(run())


__all__ = ["register"]
