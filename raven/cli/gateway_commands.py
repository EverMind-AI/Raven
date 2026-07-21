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
    load_runtime_config,
    make_provider,
    parse_fake_now,
    print_deprecated_memory_window_notice,
)
from raven.cli._plugin_stack import maybe_build_memory_backend
from raven.utils.helpers import sync_workspace_templates

console = Console()


def build_model_routing(config, provider):
    """Return ``(router, provider)`` for the configured routing backend.

    - ``knn``: build a :class:`KNNModelRouter` and wrap ``provider`` in a
      :class:`PerModelProvider` so routed model names reach their endpoints
      (other models fall back to ``provider`` unchanged).
    - ``ecoclaw``: build the PinchBench :class:`ModelRouter`.
    - routing disabled, or ecoclaw with no API key: return ``(None, provider)``.
    """
    if not config.routing.enabled:
        return None, provider

    if config.routing.backend == "knn":
        from raven.providers.per_model_provider import PerModelProvider
        from raven.routing.knn_router import KNNModelRouter

        router = KNNModelRouter(config.routing, default_model=config.agents.defaults.model)
        return router, PerModelProvider(config.routing.models, fallback=provider)

    from raven.routing.router import ModelRouter

    api_key = config.routing.api_key or config.providers.openrouter.api_key or ""
    if not api_key:
        console.print("[yellow]⚠[/yellow] Routing enabled but no OpenRouter API key found — routing disabled")
        return None, provider

    from raven.routing.types import RoutingProfileName

    profile: RoutingProfileName = config.routing.profile  # type: ignore[assignment]
    router = ModelRouter(api_key=api_key, profile=profile, fallback_model=config.agents.defaults.model)
    return router, provider


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


def _build_gateway_channels(config) -> set[str]:
    """Build the ``allowed_channels`` set used by gateway's ``CronService`` — the
    enabled IM channels only.

    The gateway owns cron jobs for its IM channels. It does NOT claim ephemeral
    ``tui``/``cli`` jobs: those are fired by the interactive process that created
    them (the TUI / ``raven agent`` session), so a TUI-set reminder always
    delivers to the TUI rather than racing the gateway and being forwarded to an
    IM channel. The trade-off is no cross-process fallback while that process is
    down; restoring "fire at origin, hand off only after the origin exits" is a
    deferred cron-delivery-ownership design, not this set.
    """
    return {name for name in _GATEWAY_IM_CHANNELS if getattr(getattr(config.channels, name, None), "enabled", False)}


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
        workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
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
        config = load_runtime_config(config, workspace)

        from raven.cli._log_file import redirect_loguru_to_file

        log_cfg = config.gateway.log
        log_path = redirect_loguru_to_file(
            "gateway.log",
            rotation=log_cfg.rotation,
            retention=log_cfg.retention,
            file_level="DEBUG" if verbose else log_cfg.level,
            terminal_level="DEBUG" if verbose else log_cfg.console_level,
        )

        from raven.cli._gateway_lock import GatewayAlreadyRunningError, acquire

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
        port = port if port is not None else config.gateway.port

        console.print(f"{__logo__} Starting Raven gateway on port {port}...")
        console.print(f"[dim]📝 Logs → {log_path}[/dim]")
        sync_workspace_templates(config.workspace_path)
        provider = make_provider(config)
        session_manager = SessionManager(config.workspace_path)

        # Create cron service first (callback set after agent creation).
        #
        # Restrict to channels gateway has adapters for. This prevents the
        # gateway from racing REPL and stealing cli-origin reminders that REPL
        # can deliver but gateway can't (REPL stdout is owned by the REPL
        # process, gateway has no cli channel). Without this, you'd see
        # "Unknown channel: cli" warnings + lost REPL reminders when both
        # processes are running.
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

        # Gateway-side memory-backend wiring. Mirrors the REPL
        # bootstrap (cli/agent_commands.py). Returns ``None`` when no
        # plugin contributes the configured backend — AgentLoop falls
        # back to its legacy ``self.memory`` path. Lifecycle (start /
        # stop) lands inside the run-loop coroutine below.
        backend = maybe_build_memory_backend(config.workspace_path, ec_config)

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
            mcp_servers=config.tools.mcp_servers,
            disabled_tools=config.tools.disabled_tools,
            tool_search_config=config.tools.tool_search,
            sandbox_config=config.tools.sandbox,
            channels_config=config.channels,
            router=router,
            skill_forge_config=skill_forge_cfg,
            context_config=ec_config.context,
            runtime_config=ec_config.runtime,
            # Gateway sessions are inherently multi-turn (each RPC session
            # gets a key and can receive a recovery block on its next call).
            interactive=True,
            response_modifier=sentinel_response_modifier,
            on_user_inbound=sentinel_on_user_inbound,
            backend=backend,
            memory_config=ec_config.memory,
            skill_forge_router_config=ec_config.skill_forge.router,
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
                cron.on_job = make_on_cron_job(
                    agent,
                    gw_hub,
                    submit=gw_scheduler.submit,
                    readback_texts=gw_readback_texts,
                    channel_manager=channels,
                    session_manager=session_manager,
                    default_channel="cli",
                    system_events=system_events,
                    wake=wake,
                )

                from raven.spine import ChatType, Origin, Source, TurnRequest

                async def on_heartbeat_execute(tasks: str) -> str:
                    """Run heartbeat tasks as a HEARTBEAT-origin turn; the
                    hub delivers the reply to the picked channel. Deliver-only — no
                    one reads the reply back (HeartbeatService is wired on_notify=
                    None, the hub already delivered), so the return is unused."""
                    channel, chat_id = _pick_heartbeat_target()
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
                    await gw_scheduler.submit(req).result()
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
                    sentinel_runner.dispatcher.set_post(gw_hub.post)
                if sentinel_runner is not None and sentinel_runner.task_discoverer is not None:
                    sentinel_runner.task_discoverer.set_submit(gw_scheduler.submit)
                if (
                    agent.decision_consumer is not None
                    and getattr(agent.decision_consumer, "executor", None) is not None
                ):
                    agent.decision_consumer.executor.set_submit(gw_scheduler.submit)
                # Subagent result re-injection submits a SUBAGENT-origin turn.
                agent.subagents.set_submit(gw_scheduler.submit)
                # Deep research (channel/async) delivers its finished answer back
                # via a deliver_text turn; wiring submit here (gateway only) is
                # what flips the tool from its synchronous path to the async one.
                if agent.deep_research_manager is not None:
                    agent.deep_research_manager.set_submit(gw_scheduler.submit)

                # ask_user round-trip on the channel side: the QuestionBroker
                # renders the agent's clarify.request as an outbound Text to the
                # conversation's channel; the inbound gate (below) routes the
                # user's next message back via reply(). The question fires mid-turn,
                # so the live turn's real inbound Source is still in gw_sources
                # (keyed by conversation id) — reuse it so a topic / thread address
                # is exact, rather than reconstructing it from the conversation id.
                from raven.spine import Text as _Text
                from raven.tui_rpc.question_broker import QuestionBroker

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
                if (ask_tool := agent.tools.get("ask_user")) is not None and hasattr(ask_tool, "set_broker"):
                    ask_tool.set_broker(question_broker)

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
                if gw_teardown is not None:
                    await gw_teardown()
                await agent.close_mcp()
                agent.stop()
                await channels.stop_all()
                # Stop the memory-backend plugin last so any
                # in-flight backend.store / backend.feedback calls
                # spawned during AgentLoop teardown can complete.
                if backend is not None:
                    try:
                        await backend.stop()
                    except Exception:
                        _logger.exception(
                            "memory backend stop failed; continuing shutdown",
                        )

        asyncio.run(run())


__all__ = ["register"]
