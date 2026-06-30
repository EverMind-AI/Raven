"""Top-level ``agent`` command + its dedicated helpers.

This module owns:

- The interactive ``raven agent`` REPL command body (multiline paste,
  history, agent-loop wiring).
- A small bundle of helpers used only by that command: prompt-toolkit
  session init, terminal restore, TTY-flush, response rendering, exit
  detection.

``commands.py`` registers the command via :func:`register`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from raven import __logo__
from raven.cli._helpers import (
    load_runtime_config,
    make_provider,
    parse_fake_now,
    print_deprecated_memory_window_notice,
    warn_about_pending_cli_reminders,
)
from raven.cli._plugin_stack import (
    build_plugin_registry,
    build_plugin_tools,
    maybe_build_memory_backend,
)
from raven.utils.helpers import sync_workspace_templates

console = Console()


# ---------------------------------------------------------------------------
# Module-level state (interactive REPL only)
# ---------------------------------------------------------------------------

EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


# ---------------------------------------------------------------------------
# Helpers (private to this module)
# ---------------------------------------------------------------------------

def _stdout_isatty() -> bool:
    """Whether stdout is an interactive TTY (seam for the onboarding gate test;
    CliRunner swaps ``sys.stdout`` for a non-TTY buffer)."""
    return sys.stdout.isatty()


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios

        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from raven.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,  # Enter submits (single line mode)
    )


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} Raven[/cyan]")
    console.print(body)
    console.print()


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS






async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        # raw=True passes ANSI escape sequences through verbatim. Without
        # this, background coroutines (cron fires, Sentinel nudges) that
        # print rich-styled output while the user sits at this prompt get
        # their ESC bytes mangled — visible as ?[36m...?[0m garbage.
        with patch_stdout(raw=True):
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    """Attach the ``agent`` command to ``app``."""
    @app.command()
    def agent(
        message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
        session_id: str | None = typer.Option(
            None,
            "--session",
            "-s",
            help=(
                "Full session key (channel:chat_id), any channel. By default "
                "a fresh cli session is minted per invocation. The legacy "
                "'direct' session remains reachable via --resume direct."
            ),
        ),
        continue_: bool = typer.Option(False, "--continue", "-c", help="Continue the most recent cli session"),
        resume: str | None = typer.Option(None, "--resume", "-r", help="Resume session by bare id or unique prefix"),
        workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
        config: str | None = typer.Option(None, "--config", help="Config file path"),
        markdown: bool = typer.Option(
            True, "--markdown/--no-markdown", help="Render assistant output as Markdown"
        ),
        logs: bool = typer.Option(
            False, "--logs/--no-logs", help="Show Raven runtime logs during chat"
        ),
        wait_skill_extract: bool = typer.Option(
            False,
            "--wait-skill-extract/--no-wait-skill-extract",
            help=(
                "Block exit until in-flight everos extraction tasks finish. "
                "Off by default — extraction is fire-and-forget, so the CLI "
                "returns as soon as the agent responds and any in-flight "
                "boundary-detection / case-extraction LLM call may be "
                "cancelled by interpreter shutdown. When on (without "
                "--flush-skill-buffer), the per-session pending-turn buffer "
                "is left intact for the next CLI invocation, which is the "
                "mode you want for scripted multi-turn boundary-detection "
                "testing (multiple ``-m`` calls sharing the same ``-s``)."
            ),
        ),
        flush_skill_buffer: bool = typer.Option(
            False,
            "--flush-skill-buffer/--no-flush-skill-buffer",
            help=(
                "Send a ``session_end`` signal for this session before exit, "
                "draining whatever turns are sitting in the everos "
                "boundary-detection buffer through case + skill extraction. "
                "Pair with --wait-skill-extract to actually block on the "
                "resulting LLM calls (a flush without --wait-skill-extract "
                "schedules the drain but won't survive interpreter "
                "shutdown). Use on the final ``-m`` of a scripted "
                "multi-turn session, or to force extraction after a single "
                "``-m`` turn (a lone turn never trips a boundary on its own)."
            ),
        ),
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
        """Interact with the agent directly."""
        if sum((session_id is not None, continue_, resume is not None)) > 1:
            raise typer.BadParameter(
                "--session, --continue and --resume are mutually exclusive"
            )

        # Startup gate: when the required config (a provider key + default
        # model) is missing, run the onboarding wizard first. Only on an
        # interactive TTY — scripted one-shots (`-m`) and non-TTY pipes must
        # fail loudly later rather than block on prompts.
        from raven.cli.onboard_commands import _is_config_populated

        if message is None and _stdout_isatty() and not _is_config_populated():
            from raven.cli.onboard_commands import ensure_configured_or_onboard

            ensure_configured_or_onboard()

        from loguru import logger

        from raven.agent.loop import AgentLoop
        from raven.agent.loop.recovery import limits_from_defaults
        from raven.cli._cron_handler import make_on_cron_job
        from raven.cli._proactive_stack import (
            attach_sentinel_decision_consumer,
            attach_sentinel_spawn,
            build_sentinel_stack,
        )
        from raven.config.raven import load_raven_config
        from raven.config.paths import get_cron_dir
        from raven.proactive_engine.schedulers.cron.service import CronService
        from raven.session.manager import SessionManager, new_chat_id

        # load_runtime_config must run FIRST: it calls set_config_path() so
        # that subsequent load_raven_config() reads from --config, not the
        # default ~/.raven/config.json. Otherwise skill_forge / sentinel
        # from --config are silently ignored.
        config = load_runtime_config(config, workspace)
        ec_config = load_raven_config()
        sentinel_cfg = ec_config.sentinel
        skill_forge_cfg = ec_config.skill_forge
        print_deprecated_memory_window_notice(config)
        sync_workspace_templates(config.workspace_path)

        provider = make_provider(config)
        session_manager = SessionManager(config.workspace_path)

        # New-session-by-default: independent one-shots don't bleed into each other.
        if resume is not None:
            from raven.cli.session_commands import resolve_session
            session_id = resolve_session(session_manager, resume)
        elif continue_:
            recent = session_manager.find_most_recent_chat_id("cli")
            if recent is None:
                console.print("[dim]no previous cli session — starting fresh[/dim]")
                recent = new_chat_id()
            session_id = f"cli:{recent}"
        elif session_id is None:
            session_id = f"cli:{new_chat_id()}"
        else:
            from raven.cli.session_commands import resolve_session_cross_channel
            session_id = resolve_session_cross_channel(session_manager, session_id)

        # Create cron service (callback set below once the agent exists).
        # allowed_channels={"cli"} prevents this REPL from claiming reminders
        # created in Feishu/Telegram/etc. — those should be delivered by the
        # gateway which has the real channel adapters wired up.
        cron_store_path = get_cron_dir() / "jobs.json"
        cron = CronService(
            cron_store_path, allowed_channels={"cli"},
            now_fn=parse_fake_now(fake_now),
        )

        # Build Sentinel stack if enabled — same wiring gateway uses, so the two
        # processes share state via ~/.raven/sentinel/state.json. Discover
        # triggers are dispatcher-side: only the gateway has real channel
        # adapters, so REPL must NOT drain them or feishu/slack triggers get
        # consumed without delivery.
        sentinel_runner, sentinel_response_modifier, sentinel_on_user_inbound = build_sentinel_stack(
            config, sentinel_cfg, session_manager, provider,
            now_fn=parse_fake_now(fake_now),
            include_discover_triggers=False,
        )

        if logs:
            logger.enable("raven")
        else:
            logger.disable("raven")

        # Build the plugin-provided memory backend (the bundled
        # everos backend by default). Returns ``None`` when no plugin
        # contributes the configured backend name — AgentLoop falls
        # back to its legacy ``self.memory`` path. Lifecycle (start /
        # stop) is handled in ``run_once`` so the awaits land in the
        # right event loop context.
        # Build the plugin registry once and reuse it for both the memory
        # backend and the plugin-contributed tools so discovery/activation
        # runs a single time.
        plugin_registry = build_plugin_registry(ec_config)
        backend = maybe_build_memory_backend(
            config.workspace_path, ec_config, registry=plugin_registry,
        )
        plugin_tools = build_plugin_tools(
            config.workspace_path, ec_config, registry=plugin_registry,
        )

        agent_loop = AgentLoop(
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
            exec_config=config.tools.exec,
            cron_service=cron,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            session_manager=session_manager,
            mcp_servers=config.tools.mcp_servers,
            disabled_tools=config.tools.disabled_tools,
            sandbox_config=config.tools.sandbox,
            channels_config=config.channels,
            skill_forge_config=skill_forge_cfg,
            context_config=ec_config.context,
            runtime_config=ec_config.runtime,
            # ``-m "..."`` is a one-shot — no next turn for recovery
            # injection, so the ``"interactive"`` policy skips the
            # checkpoint here. REPL (``message is None``) is multi-turn.
            interactive=message is None,
            response_modifier=sentinel_response_modifier,
            on_user_inbound=sentinel_on_user_inbound,
            backend=backend,
            memory_config=ec_config.memory,
            skill_forge_router_config=ec_config.skill_forge.router,
            plugin_tools=plugin_tools,
        )
        agent_loop.configure_personalization(config.agents.defaults.enable_personalization)
        attach_sentinel_spawn(sentinel_runner, agent_loop)
        attach_sentinel_decision_consumer(sentinel_runner, agent_loop, sentinel_cfg=sentinel_cfg)
        # REPL has no real ChannelManager — provide a minimal shim that
        # reports "cli" as the sole enabled channel so cli reminders take
        # the pass-through path (deliver to REPL stdout via the spine CliOutlet). The same
        # shim goes to the sentinel runner so anticipatory (sentinel:direct)
        # nudges resolve to the terminal instead of being dropped.
        from types import SimpleNamespace
        cli_shim = SimpleNamespace(enabled_channels=["cli"])
        if sentinel_runner is not None:
            sentinel_runner.set_channel_manager(cli_shim)
        # cron.on_job is wired inside run_interactive once the spine scheduler
        # exists — cron reminders submit CRON turns through it.

        # Show spinner when logs are off (no output to miss); skip when logs are on
        def _thinking_ctx():
            if logs:
                from contextlib import nullcontext

                return nullcontext()
            # Animated spinner is safe to use with prompt_toolkit input handling
            return console.status("[dim]Raven is thinking...[/dim]", spinner="dots")

        if message:
            # Single message mode — one USER turn through spine (submit -> lane ->
            # run_turn -> hub -> CliOutlet), with the legacy cli/direct defaults
            # (channel="cli", chat_id="direct", session_key=session_id). Progress
            # renders via the CliOutlet, gated by the same two config flags the bus
            # path honored (send_progress / send_tool_hints).
            from raven.cli._repl_spine import build_repl
            from raven.spine import ChatType, Origin, Source, TurnRequest

            async def run_once():
                # Bring the memory-backend plugin online before any turn
                # runs. ``backend`` is ``None`` when no plugin is wired.
                if backend is not None:
                    try:
                        await backend.start()
                    except Exception:
                        logger.exception(
                            "memory backend start failed; continuing with "
                            "legacy memory path",
                        )
                try:
                    # Build inside the running loop: Scheduler pins its home loop in
                    # __init__, so build_repl must not run in the sync prologue.
                    ch = agent_loop.channels_config
                    scheduler, hub, teardown = build_repl(
                        agent_loop,
                        "cli",
                        lambda t: _print_agent_response(t, render_markdown=markdown),
                        render_notice=lambda c: console.print(f"  [dim]↳ {c}[/dim]"),
                        send_progress=bool(ch.send_progress) if ch else False,
                        send_tool_hints=bool(ch.send_tool_hints) if ch else False,
                    )
                    # A one-shot spawn rarely finishes before the hard-exit below (same
                    # as the bus path), but wire submit for parity with REPL/TUI.
                    agent_loop.subagents.set_submit(scheduler.submit)
                    with _thinking_ctx():
                        handle = scheduler.submit(
                            TurnRequest(
                                origin=Origin.USER,
                                source=Source(
                                    channel="cli",
                                    chat_id="direct",
                                    sender_id="user",
                                    chat_type=ChatType.DM,
                                ),
                                text=message,
                                conversation=session_id,
                            )
                        )
                        await handle.result()
                    await hub.wait_idle("cli")  # render barrier: CliOutlet caught up
                    await teardown()
                    if wait_skill_extract or flush_skill_buffer:
                        # ``flush_skill_buffer`` sends session_end so any
                        # buffered turns drain through extraction (a single
                        # -m turn never trips a boundary on its own).
                        # ``wait_skill_extract`` blocks on the in-flight
                        # tasks; without it the flush schedules work that
                        # interpreter shutdown will cancel. The two flags
                        # are orthogonal — scripted multi-turn testing uses
                        # --wait-skill-extract alone so the buffer survives
                        # for the next CLI run.
                        await agent_loop.await_pending_extractions(
                            flush_session_id=session_id if flush_skill_buffer else None,
                            wait=wait_skill_extract,
                        )
                    await agent_loop.close_mcp()
                finally:
                    if backend is not None:
                        try:
                            await backend.stop()
                        except Exception:
                            logger.exception(
                                "memory backend stop failed; continuing shutdown",
                            )

            asyncio.run(run_once())
            # One-shot headless exit: torch's global teardown segfaults during
            # interpreter finalization (faulthandler shows a C-level fault with
            # no Python frame, torch._C in the loaded extensions). The response
            # is already printed and MCP/extractions drained inside run_once(),
            # so hard-exit to skip the buggy native destructors. Scripted /
            # subprocess callers otherwise see a spurious returncode 139.
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)
        else:
            # Interactive mode — user turns run through spine (submit -> lane ->
            # hub -> CliOutlet); cron/sentinel nudges go via the spine hub
            # (hub.post -> CliOutlet) too.
            from raven.cli._repl_spine import build_repl, run_repl_loop

            _init_prompt_session()
            console.print(
                f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n"
            )

            if ":" in session_id:
                cli_channel, cli_chat_id = session_id.split(":", 1)
            else:
                cli_channel, cli_chat_id = "cli", session_id

            def _handle_signal(signum, frame):
                sig_name = signal.Signals(signum).name
                _restore_terminal()
                console.print(f"\nReceived {sig_name}, goodbye!")
                sys.exit(0)

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
            # SIGHUP is not available on Windows
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, _handle_signal)
            # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
            # SIGPIPE is not available on Windows
            if hasattr(signal, "SIGPIPE"):
                signal.signal(signal.SIGPIPE, signal.SIG_IGN)

            async def run_interactive():
                # Backend lifecycle matches the single-message
                # mode; ``backend`` is ``None`` when no plugin is wired,
                # in which case start/stop are skipped.
                if backend is not None:
                    try:
                        await backend.start()
                    except Exception:
                        logger.exception(
                            "memory backend start failed; continuing with "
                            "legacy memory path",
                        )
                # agent_loop.run() is now a lifecycle keep-alive (executor /
                # debug server / MCP up, then idle); all turns go through the
                # spine. Gathered on teardown.
                runtime_task = asyncio.create_task(agent_loop.run())
                # Build the spine before starting cron: cron jobs submit CRON
                # turns through this scheduler, and on_job must be wired
                # before cron.start() so an immediately-firing job has its
                # callback. Scheduler pins its home loop here (run_interactive is
                # async) — it must not move to the sync prologue.
                def _render_nudge_marker() -> None:
                    console.print()
                    console.print("[bold magenta]🦞 [主动][/bold magenta]")

                scheduler, hub, teardown = build_repl(
                    agent_loop,
                    cli_channel,
                    lambda t: _print_agent_response(t, render_markdown=markdown),
                    render_marker=_render_nudge_marker,
                )
                # Subagent result re-injection submits a SUBAGENT-origin turn.
                agent_loop.subagents.set_submit(scheduler.submit)
                # Cron reminders run as CRON-origin turns through the spine
                # scheduler, delivered by the hub -> CliOutlet (replacing the
                # legacy bus path). readback_texts/system_events stay unset: the
                # REPL has no heartbeat, so the handler no-ops them.
                cron.on_job = make_on_cron_job(
                    agent_loop, hub,
                    submit=scheduler.submit,
                    channel_manager=cli_shim,
                    session_manager=session_manager,
                    default_channel="cli",
                )
                # Sentinel nudges now ride the spine hub -> CliOutlet (replacing
                # the legacy bus consume): late-bind the REPL hub's post.
                if sentinel_runner is not None and sentinel_runner.dispatcher is not None:
                    sentinel_runner.dispatcher.set_post(hub.post)
                # Start cron so scheduled reminders ("remind me in 1 minute")
                # actually fire — previously the REPL created a CronService but
                # never started its tick loop, so jobs just sat in jobs.json.
                await cron.start()
                # Start Sentinel if enabled so anticipatory nudges reach the REPL.
                # Nudges ride the spine hub -> CliOutlet (which renders the 🦞
                # proactive marker for _sentinel_origin); no bus consumer here.
                if sentinel_runner is not None:
                    await sentinel_runner.start()

                def _on_exit() -> None:
                    _restore_terminal()
                    console.print("\nGoodbye!")

                def _slash(command: str) -> bool:
                    from raven.cli._repl_slash import handle_repl_slash

                    return handle_repl_slash(command, console=console)

                try:
                    await run_repl_loop(
                        read_input=_read_interactive_input_async,
                        submit=scheduler.submit,
                        wait_idle=hub.wait_idle,
                        channel=cli_channel,
                        chat_id=cli_chat_id,
                        is_exit=_is_exit_command,
                        handle_slash=_slash,
                        thinking=_thinking_ctx,
                        on_exit=_on_exit,
                    )
                finally:
                    if sentinel_runner is not None:
                        await sentinel_runner.stop()
                    cron.stop()
                    agent_loop.stop()
                    await teardown()  # scheduler.shutdown + hub.aclose (honors the shutdown contract)
                    await asyncio.gather(runtime_task, return_exceptions=True)
                    if wait_skill_extract or flush_skill_buffer:
                        # ``exit`` is not a natural boundary; without a
                        # flush any buffered turns would sit
                        # indefinitely until the next session reuses
                        # the id. With ``flush_skill_buffer`` we send
                        # session_end here so they drain; with
                        # ``wait_skill_extract`` we block on the
                        # resulting (and any other in-flight) task.
                        await agent_loop.await_pending_extractions(
                            flush_session_id=(
                                f"{cli_channel}:{cli_chat_id}"
                                if flush_skill_buffer else None
                            ),
                            wait=wait_skill_extract,
                        )
                    await agent_loop.close_mcp()
                    # Stop the memory-backend plugin before exit.
                    # Closes the HTTP client pool (HTTP mode) or releases
                    # any in-process EverMem handles (embedded).
                    if backend is not None:
                        try:
                            await backend.stop()
                        except Exception:
                            logger.exception(
                                "memory backend stop failed; continuing shutdown",
                            )
                    warn_about_pending_cli_reminders(cron, config)

            asyncio.run(run_interactive())


__all__ = ["register"]
