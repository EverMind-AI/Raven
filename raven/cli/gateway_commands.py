"""Top-level ``gateway`` command.

Spawns the Raven gateway: agent loop + channel manager + cron service
+ heartbeat + sentinel stack (optional). The bulk of the wiring lives in
this command body.

``commands.py`` registers it via :func:`register`.
"""

from __future__ import annotations

import asyncio
import signal
import time
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING

import typer
from loguru import logger
from rich.console import Console

from raven import __logo__
from raven.cli._helpers import (
    load_runtime_config,
    parse_fake_now,
    print_config_migration_notices,
    print_deprecated_memory_window_notice,
    report_dropped_memory_writes,
)
from raven.core.provider_stack import build_model_routing
from raven.providers.factory import make_resolving_provider
from raven.utils import asyncio_runner as bounded_asyncio
from raven.utils.workspace import sync_workspace_templates

if TYPE_CHECKING:
    from raven.config.schema import GatewayPageConfig

console = Console()

# Minimum spacing between accepted generation swaps (each one cancels every
# in-flight turn and reconnects MCP); see SwapCoordinator.
_SWAP_MIN_INTERVAL_S = 5.0


def _risk_banner(config) -> str | None:
    """Startup banner for the dangerous default combo: no sandbox + a channel
    open to anyone. Returns the banner text, or None when either leg is safe.

    Printed, not gated: gateways run unattended, so blocking on a confirm
    would strand headless restarts -- visibility is the fix here.
    """
    if getattr(config.tools.sandbox, "backend", None) != "none":
        return None

    open_channels = []
    for name, section in config.channels.channel_entries().items():
        if not section.enabled:
            continue
        if "*" in (section.allow_from or []):
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

    The gateway owns cron jobs for its IM channels. It does NOT claim ``tui``
    jobs: those fire in the interactive process that created them, so a
    TUI-set reminder delivers to the TUI rather than racing the gateway and
    being forwarded to an IM channel -- fire at origin, no trigger-time
    re-routing. The trade-off is no cross-process fallback while that process
    is down.

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


def _format_question_body(params: dict) -> str:
    """Render one ``clarify.request`` as chat text.

    A chat channel has no dialog to hold a batch, so the question's position in
    it has to ride in the message body or the user cannot tell how many more are
    coming.
    """
    body = str(params.get("question", ""))
    total = int(params.get("total", 1) or 1)
    if total > 1:
        body = f"({int(params.get('index', 0)) + 1}/{total}) {body}"
    choices = params.get("choices") or []
    if choices:
        body += "\n" + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(choices))
    return body


async def _deliver_question_to_channel(frame: dict, *, sources: dict, hub) -> None:
    """Put a question on its conversation's channel, or say it cannot be put.

    The live turn's real inbound Source is still in ``sources`` (keyed by
    conversation id), so reuse it -- a topic / thread address is exact that way,
    where one reconstructed from the conversation id is not.

    Raises :class:`QuestionUndeliverableError` rather than returning when there is no
    live source: a silent drop left the broker waiting out its whole budget on a
    question that was never rendered.

    Only a question is rendered. This sink is the broker's entire surface here, so
    every frame it emits arrives -- including ``clarify.closed``, which carries no
    question and whose whole purpose is retracting a prompt a chat channel never
    held one of. Rendering it put an empty message in the user's chat on every
    timed-out or cancelled question. A whitelist rather than a skip-list so the
    next notification the broker grows is dropped here too, not delivered blank.
    """
    from raven.rpc.question_broker import CLARIFY_REQUEST_METHOD, QuestionUndeliverableError
    from raven.spine import Text

    if frame.get("method") != CLARIFY_REQUEST_METHOD:
        return
    params = frame.get("params", {})
    qcid = params.get("conversation_id", "")
    source = sources.get(qcid)
    if source is None:
        raise QuestionUndeliverableError(f"conversation {qcid!r} has no live source")
    await hub.dispatch(Text(content=_format_question_body(params), source=source))


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


def page_target(page_config: "GatewayPageConfig", page_port: int | None) -> int | None:
    """Which port to serve the browser page on, or None for a channel-only run.

    Two callers, and the flag is the one that wins. ``gateway.page.enabled`` is
    the operator's standing preference; ``--page-port`` is `raven web` saying it
    started this process FOR the page and its open tab is on that port -- so the
    flag both mounts the page where config had it off (a supervisor with no page
    is a process nobody asked for) and pins the port, because a page that comes
    back on a different one strands the tab it came back for.
    """
    if page_port is not None:
        return page_port
    return page_config.port if page_config.enabled else None


def register(app: typer.Typer) -> None:
    """Attach the ``gateway`` group to ``app``: the daemon as the bare command,
    the control-plane verbs (reload / status / stop) as sub-commands."""
    from raven.cli import gateway_control_commands

    gateway_app = typer.Typer(
        invoke_without_command=True,
        no_args_is_help=False,
        help="Start the Raven gateway, or drive a running one (reload / status / stop).",
    )
    app.add_typer(gateway_app, name="gateway")
    gateway_control_commands.register(gateway_app)

    @gateway_app.callback()
    def gateway(
        ctx: typer.Context,
        port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
        page_port: int | None = typer.Option(
            None,
            "--page-port",
            help=(
                "Serve the browser page on this port, whatever gateway.page.enabled says. "
                "Used by `raven web`, which pins the page to the port its open tab is on."
            ),
        ),
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
        if ctx.invoked_subcommand is not None:
            return
        from raven.agent.loop.bundles import HostWiring, TurnPolicy
        from raven.agent.loop.recovery import limits_from_defaults
        from raven.agent.workdir import WorkdirPolicy, WorkdirResolver, validate_override
        from raven.config.raven import load_raven_config
        from raven.core.cron_stack import build_cron_service
        from raven.gateway.manager import ChannelManager
        from raven.session.manager import SessionManager

        # load_runtime_config must run FIRST: it calls set_config_path() so
        # that subsequent load_raven_config() reads from --config, not the
        # default ~/.raven/config.json. Otherwise skill_forge / sentinel
        # from --config are silently ignored.
        config_path_arg = config
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

        from raven.gateway.lock import GatewayAlreadyRunningError, acquire, publish_control_endpoint

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
        print_deprecated_memory_window_notice(config)
        print_config_migration_notices()
        port = port if port is not None else config.gateway.port

        console.print(f"{__logo__} Starting Raven gateway on port {port}...")
        console.print(f"[dim]📝 Logs → {log_path}[/dim]")
        banner = _risk_banner(config)
        if banner is not None:
            console.print(banner, style="bold red", markup=False)
        sync_workspace_templates(config.workspace_path, notify=lambda m: console.print(f"  [dim]{m}[/dim]"))
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
        cron = build_cron_service(allowed_channels=_build_gateway_channels(config))

        # Create model router (and, for the knn backend, wrap the provider).
        router, provider = build_model_routing(config, provider)

        # Build Sentinel stack (enabled iff sentinel.enabled).
        # NudgeInjector serves as the AgentLoop response_modifier;
        # SentinelRunner.on_user_inbound tracks reply engagement.
        # These bindings must happen BEFORE AgentLoop construction.
        from raven.core.proactive_stack import (
            attach_sentinel_decision_consumer,
            attach_sentinel_spawn,
            build_sentinel_stack,
            sentinel_hooks,
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
        from raven.core.cron_stack import chain_cron_activity_reset

        on_user_inbound = chain_cron_activity_reset(cron, inner=sentinel_on_user_inbound)

        # Gateway-side memory-backend wiring. Mirrors the one-shot agent
        # bootstrap (cli/agent_commands.py). Returns ``None`` when no
        # plugin contributes the configured backend — AgentLoop then runs
        # without a memory backend. Lifecycle (start /
        # stop) lands inside the run-loop coroutine below.
        # One registry shared by both contribution points, so plugins are
        # discovered and activated once rather than per consumer.
        from raven.core.runtime import build_runtime

        runtime = build_runtime(
            config,
            ec_config,
            provider=provider,
            session_manager=session_manager,
            router=router,
            workdir_resolver=workdir_resolver,
            policy=TurnPolicy(
                max_iterations=config.agents.defaults.max_tool_iterations,
                empty_recovery=limits_from_defaults(config.agents.defaults),
                interactive=False,
                now_fn=parse_fake_now(fake_now),
            ),
            host=HostWiring(
                notify=lambda m: console.print(m, style="yellow", markup=False),
                cron_service=cron,
                channels_config=config.channels,
                hooks=sentinel_hooks(on_user_inbound, sentinel_response_modifier),
            ),
        )
        agent = runtime.loop
        backend = runtime.backend
        # The web surface serves this same store; generations that follow
        # receive it back through _prepare_swap, so the handle is stable.
        deliverables = runtime.deliverables

        # Sentinel attach and the wake hook are generation wiring: they bind to
        # the live agent object, so they happen in _bind_generation below.

        # ChannelManager must be built before make_on_cron_job — the
        # closure captures channels.enabled_channels for trigger-time
        # delivery resolution.
        channels = ChannelManager(config)

        # Late-bind so the discovery resolver can read enabled_channels.
        if sentinel_runner is not None:
            sentinel_runner.set_channel_manager(channels)

        from raven.core.cron_stack import make_on_cron_job

        # Event wake: in-process producers (cron completions) can end the
        # heartbeat sleep early instead of waiting for the next interval.
        # Busy check covers spine-dispatched turns only (user messages) —
        # exactly the lane a wake must never compete with.
        hb_cfg = config.gateway.heartbeat
        from raven.core.proactive_stack import build_heartbeat, build_wake

        wake, system_events = build_wake(hb_cfg, is_busy=lambda: agent.is_processing)

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
            # `raven web --stop` and a systemd stop deliver SIGTERM. Parity
            # with Ctrl-C means cancelling THIS task in-loop so the graceful
            # chain below runs: the stdlib runner converts only SIGINT into a
            # main-task cancel, and a handler that raises KeyboardInterrupt
            # escapes run_until_complete without cancelling anything --
            # teardown then fell to the runner's 2-second sweep.
            main_task = asyncio.current_task()
            term_signalled = False

            def _on_term() -> None:
                nonlocal term_signalled
                if term_signalled or main_task is None:
                    return
                term_signalled = True
                main_task.cancel()

            with suppress(NotImplementedError, RuntimeError, ValueError):
                asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _on_term)
            health_server = None
            gw_teardown = None
            gw_scheduler = None
            heartbeat = None
            question_broker = None
            control = None
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

            async def _bind_generation():
                nonlocal gw_teardown, gw_scheduler, question_broker, page_mount, heartbeat

                # Generation wiring: everything here binds to the live agent
                # object and is torn down and re-run at a generation swap.
                # Sentinel's ProactiveSpawn wraps the AgentLoop's
                # SubagentManager; the decision consumer needs agent.tools +
                # agent.subagents, so both attach post-construction.
                attach_sentinel_spawn(sentinel_runner, agent)
                decision_consumer = attach_sentinel_decision_consumer(sentinel_runner, agent, sentinel_cfg=sentinel_cfg)
                if wake is not None:
                    agent.on_turn_complete.append(wake.on_turn_complete)

                # Spine assembly for the gateway's host sources (cron submits
                # through it, replies route to channels via a per-channel outlet).
                # Built here, inside the running loop, not in the sync command
                # prologue: Scheduler pins its home loop at construction (submit
                # must come from that loop), and the prologue has no loop yet.
                from raven.gateway.spine import build_gateway

                gw_scheduler, gw_hub, gw_readback_texts, gw_sources, gw_teardown = build_gateway(
                    agent,
                    channels.channels,
                    user_pool=config.gateway.user_pool,
                    cut_by_reload=lambda: swaps.in_flight,
                    system_pool=config.gateway.system_pool,
                    send_max_retries=config.gateway.send_max_retries,
                    shutdown_grace=config.gateway.shutdown_grace,
                )

                # A channel enabled while the gateway runs gets its outlet too:
                # build_gateway registers one per channel that existed at
                # launch, and without this a hot-started channel could receive
                # but every reply to it was dropped by the hub.
                from raven.gateway.outlet import ChannelOutletAdapter

                channels.on_started = lambda ch: gw_hub.register(ChannelOutletAdapter(ch))
                # And retired when it stops, so a channel disabled and enabled
                # again is not left replying through the adapter it dropped.
                channels.on_stopped = gw_hub.retire

                # Proactive target (cron / sentinel / heartbeat / subagent /
                # deep_research): the gateway spine. Its hub delivers to the IM
                # channels and, while a page is mounted, to the page.
                pro_submit = gw_scheduler.submit
                pro_hub = gw_hub
                pro_readback = gw_readback_texts
                pro_channel = "cli"
                pro_heartbeat_target: tuple[str, str] | None = None

                cron.on_job = make_on_cron_job(
                    submit=pro_submit,
                    readback_texts=pro_readback,
                    default_channel=pro_channel,
                    system_events=system_events,
                    wake=wake,
                    cron_service=cron,
                )
                # Missed-reminder observer: past-due tui one-shots whose
                # session closed before firing surface once through the same
                # system-event -> heartbeat wake path as cron completions.
                # Needs the event-wake plumbing; without it there is no sink,
                # so the observer stays off (as it does with notify_missed
                # false). Wired before cron.start() — the start-time check is
                # the first observation pass.
                if system_events is not None and wake is not None and config.cron.notify_missed:
                    from raven.core.cron_stack import make_on_missed_foreign

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

                heartbeat = build_heartbeat(
                    config,
                    provider,
                    model=agent.model,
                    on_execute=on_heartbeat_execute,
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
                if decision_consumer is not None and getattr(decision_consumer, "executor", None) is not None:
                    decision_consumer.executor.set_submit(pro_submit)
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

                async def _question_to_channel(frame: dict) -> None:
                    await _deliver_question_to_channel(frame, sources=gw_sources, hub=gw_hub)

                question_broker = QuestionBroker(
                    send_frame=_question_to_channel,
                    timeout_s=config.tools.ask_user.timeout,
                )
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
                #
                # Which port, and whether at all, is `page_target` above.
                page_on = page_target(config.gateway.page, page_port)
                if page_on is not None:
                    from raven.cli._gateway_page import mount_page

                    try:
                        page_mount = await mount_page(agent, page_on)
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
                    from raven.rpc.cron_events import build_cron_callback_spine

                    cron.on_job = build_cron_callback_spine(
                        cron.on_job,
                        page_mount.emitter,
                        default_channel=pro_channel,
                        direct_targets=page_mount.direct_targets,
                    )
                    console.print(f"[green]✓[/green] Page: {page_mount.url} (rpc: {page_mount.url}/rpc)")

                # Channel inbound runs through the spine: a permitted
                # message is submitted as a USER turn. /stop and /restart are
                # control commands: intercepted here rather than submitted as
                # turns, or the agent would reply to the text. The cid matches
                # the lane key (conversation or channel:chat_id).
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

            from raven.core.runtime import SwapCandidate, SwapCoordinator

            # Spacing between accepted swaps is its own number, not the
            # teardown grace: a grace of 0 must not switch the storm guard off.
            swaps = SwapCoordinator(min_interval_s=_SWAP_MIN_INTERVAL_S)

            async def _unbind_generation():
                # The generation slice of the shutdown path, in the same
                # order; the process-lifetime transports (channels, cron,
                # sentinel, control plane, health) stay up through a swap.
                nonlocal gw_teardown, page_mount
                if heartbeat is not None:
                    heartbeat.stop()
                if question_broker is not None:
                    question_broker.cancel_all()
                # Cancel sub-agents before the spines tear down, same reason
                # as the shutdown path: a result re-injection into a spine
                # already gone would record as a failure rather than a stop.
                # dispose() cancels again, which is an idempotent no-op.
                await agent.subagents.cancel_all()
                if page_mount is not None:
                    await page_mount.teardown()
                    page_mount = None
                # The claim followed the mount: the next bind re-adds "tui"
                # iff the next generation mounts a page, so a reload that
                # turns the page off stops claiming tui cron jobs the hub
                # could then only drop. discard() is safe when no page ever
                # mounted.
                cron.allowed_channels.discard("tui")
                if gw_teardown is not None:
                    await gw_teardown()
                    gw_teardown = None
                await runtime.dispose()

            async def _request_swap() -> dict:
                # The one guarded entry for every trigger. BUILD N+1 comes
                # first, while generation N keeps serving: a config that fails
                # to load or assemble gives the slot back and leaves N
                # untouched. Only a fully built candidate stops the loop --
                # which happens within about a second; in-flight turns then
                # get shutdown_grace before they are cancelled.
                refused = swaps.begin()
                if refused is not None:
                    return {"ok": False, "reason": refused}
                try:
                    # The same overrides the launch used: --config is sticky
                    # in the loader, --home is not, and a swapped generation
                    # must not quietly move the workspace.
                    new_config = load_runtime_config(config_path_arg, home=home)
                    new_ec = load_raven_config()
                    new_provider = make_resolving_provider(new_config)
                    new_router, new_provider = build_model_routing(new_config, new_provider)
                    nxt = build_runtime(
                        new_config,
                        new_ec,
                        provider=new_provider,
                        session_manager=session_manager,
                        router=new_router,
                        workdir_resolver=workdir_resolver,
                        deliverables=deliverables,
                        policy=TurnPolicy(
                            max_iterations=new_config.agents.defaults.max_tool_iterations,
                            empty_recovery=limits_from_defaults(new_config.agents.defaults),
                            interactive=False,
                            now_fn=parse_fake_now(fake_now),
                        ),
                        host=HostWiring(
                            notify=lambda m: console.print(m, style="yellow", markup=False),
                            cron_service=cron,
                            channels_config=new_config.channels,
                            hooks=sentinel_hooks(on_user_inbound, sentinel_response_modifier),
                        ),
                    )
                except Exception as exc:
                    swaps.abort()
                    _logger.exception(
                        "generation swap aborted: rebuild from config failed; the running generation keeps serving",
                    )
                    return {"ok": False, "reason": "build_failed", "error": f"{type(exc).__name__}: {exc}"}
                swaps.stage(SwapCandidate(new_config, new_ec, new_provider, new_router, nxt))
                agent.stop()
                return {
                    "ok": True,
                    "generation": swaps.generation + 1,
                    "swap": "pending",
                    "grace_s": config.gateway.shutdown_grace,
                }

            def _on_sighup() -> None:
                # A signal has no reply channel, so it is the forced form of
                # gateway.reload; a refusal is logged since nobody else hears it.
                async def _signalled() -> None:
                    reply = await _reload(force=True)
                    if not reply.get("ok"):
                        _logger.warning("SIGHUP reload refused: {}", reply.get("reason"))

                swaps.track(asyncio.create_task(_signalled()))

            async def _serve_generations():
                # One iteration per generation: agent.run() returning with a
                # built candidate staged is a swap; returning without one is
                # shutdown. DISPOSE of N happens only after N+1 is in hand.
                nonlocal config, ec_config, provider, router, runtime, agent, backend
                while True:
                    # Release right before run(): the slot stays claimed
                    # through wiring so no trigger can stop a loop that has
                    # not started yet.
                    swaps.release()
                    await agent.run()
                    swap = swaps.take()
                    if swap is None:
                        return
                    await _unbind_generation()
                    config, ec_config, provider, router, runtime = (
                        swap.config,
                        swap.ec_config,
                        swap.provider,
                        swap.router,
                        swap.runtime,
                    )
                    agent = runtime.loop
                    backend = runtime.backend
                    if backend is not None:
                        try:
                            await backend.start()
                        except Exception:
                            _logger.exception(
                                "memory backend start failed; continuing with legacy memory path",
                            )
                    try:
                        await _bind_generation()
                    except Exception:
                        # BUILD covered assembly, not wiring; a wiring failure
                        # here is fatal for the process because generation N is
                        # already disposed -- say so instead of exiting silently.
                        _logger.exception(
                            "generation swap failed while wiring generation {}; the gateway cannot continue",
                            swaps.generation + 1,
                        )
                        raise
                    await heartbeat.start()
                    _logger.info("generation {} wired; starting", swaps.generation + 1)

            # The control plane: process-lifetime, registered once. Nothing on
            # it depends on the generation, so nothing here rebinds at a swap;
            # the endpoint is published only after the site actually bound.
            import os
            import secrets

            from raven.rpc.control import ControlPlaneServer, register_control_methods
            from raven.rpc.dispatcher import Dispatcher
            from raven.rpc.transports.ws import pick_port

            started_at = time.time()
            shutdown_requested = False
            main_task = asyncio.current_task()

            def _status() -> dict:
                from raven.config.loader import get_config_path

                return {
                    "pid": os.getpid(),
                    "started_at": started_at,
                    "generation": swaps.generation,
                    "swap_in_flight": swaps.in_flight,
                    "config_path": str(get_config_path()),
                    "page": {"mounted": page_mount is not None, "url": getattr(page_mount, "url", None)},
                }

            async def _shutdown() -> None:
                nonlocal shutdown_requested
                shutdown_requested = True
                if main_task is not None:
                    # One tick later, so the {ok: true} reply leaves before
                    # the teardown closes the control socket.
                    asyncio.get_running_loop().call_soon(main_task.cancel)

            async def _reload(force: bool) -> dict:
                # A swap cancels every in-flight turn, sub-agent and pending
                # question; refuse while there is work unless told to force.
                if not force:
                    questions = question_broker.pending_count() if question_broker is not None else 0
                    if page_mount is not None:
                        questions += page_mount.question_broker.pending_count()
                    subagents = agent.subagents.get_running_count()
                    in_flight = agent.is_processing or (gw_scheduler is not None and gw_scheduler.has_running())
                    if in_flight or questions or subagents:
                        return {"ok": False, "reason": "busy", "subagents": subagents, "questions": questions}
                return await _request_swap()

            control_dispatcher = Dispatcher()
            register_control_methods(
                control_dispatcher,
                channel_manager=channels,
                request_swap=_reload,
                status=_status,
                shutdown=_shutdown,
            )
            # Never unauthenticated and never configured: the token is minted
            # per boot and dies with the process; the port is probed forward
            # from the historical default. Local clients read both from the
            # lock payload, the same way `doctor` finds the gateway.
            control_token = secrets.token_urlsafe(24)

            try:
                control = ControlPlaneServer(await pick_port(8765), auth_token=control_token)
                control.bind(control_dispatcher)
                bound_host, bound_port = await control.start()
                publish_control_endpoint(bound_host, bound_port, control_token)
                console.print(f"[green]✓[/green] Control plane: ws://{bound_host}:{bound_port}/ws")
                await _bind_generation()
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
                # SIGHUP = rebuild from config and swap at the next turn
                # boundary. Unavailable on platforms without signal-handler
                # support in the event loop; the gateway serves fine without.
                import signal as _signal

                try:
                    asyncio.get_running_loop().add_signal_handler(_signal.SIGHUP, _on_sighup)
                except (AttributeError, NotImplementedError, RuntimeError):
                    pass
                coros = [
                    _serve_generations(),
                    channels.start_all(),
                    _delayed_discover_trigger_drain(),
                ]
                if health_server is not None:
                    coros.append(health_server.serve_forever())
                coros.append(control.serve())
                await asyncio.gather(*coros)
            except KeyboardInterrupt:
                console.print("\nShutting down...")
            except asyncio.CancelledError:
                # gateway.shutdown cancels this task on purpose, and so does
                # the SIGTERM handler above; anything else cancelling it is
                # not ours to swallow.
                if term_signalled:
                    console.print("\nShutting down...")
                elif shutdown_requested:
                    console.print("\nShutting down (control plane)...")
                else:
                    raise
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
                if control is not None:
                    await control.stop()
                from raven.acp_client.client import begin_drain
                from raven.acp_client.pool import close_pool

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
                        dropped = await agent.drain_backend_stores()
                        report_dropped_memory_writes(dropped, console)
                        await backend.stop()
                    except Exception:
                        _logger.exception(
                            "memory backend stop failed; continuing shutdown",
                        )

        bounded_asyncio.run(run())


__all__ = ["register"]
