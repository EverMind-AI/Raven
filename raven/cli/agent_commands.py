"""Top-level ``agent`` command (one-shot ``-m`` mode).

``raven agent -m "..."`` runs a single USER turn through the spine
(submit -> lane -> run_turn -> hub -> CliOutlet) and exits. The
interactive REPL was removed — ``raven tui`` is the interactive
front-end; invoking ``raven agent`` without ``-m`` prints a pointer
and exits non-zero.

``commands.py`` registers the command via :func:`register`.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from raven import __logo__
from raven.cli._helpers import (
    load_runtime_config,
    make_provider,
    parse_fake_now,
    print_deprecated_memory_window_notice,
)
from raven.cli._plugin_stack import (
    build_plugin_registry,
    build_plugin_tools,
    maybe_build_memory_backend,
)
from raven.utils.helpers import sync_workspace_templates

console = Console()


# One-shot ``-m`` exit code: set by the error renderer, checked after the
# turn. Module-level because the render callback runs inside the delivery
# hub's worker task, where raising typer.Exit would be swallowed.
_ONE_SHOT_EXIT = {"code": 0}


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    if _print_llm_error(content):
        return
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} Raven[/cyan]")
    console.print(body)
    console.print()


# Category-apt hints for non-auth provider errors. Credential guidance would
# mislead here: a rate limit or a network drop is not fixed by re-checking the
# key. Categories absent from this map (invalid_request, unknown, ...) render
# the error line alone.
_NON_AUTH_HINTS = {
    "rate_limit": "Hint: the provider is rate limiting; retry in a moment.",
    "network": "Hint: network problem; check connectivity and retry.",
    "context_overflow": "Hint: the input exceeds the model's context window; shorten it.",
    "server": "Hint: provider-side error; retry later or switch models.",
    "model_unavailable": "Hint: model not served; pick another with raven provider use <name>/<model>.",
    "billing": "Hint: billing or quota issue; check your provider account.",
}


def _print_llm_error(content: str) -> bool:
    """Render a provider error as a diagnosis + fix hint instead of a fake
    agent reply. Returns True when handled; marks the one-shot path to exit
    non-zero."""
    from rich.markup import escape

    from raven.providers.base import parse_llm_error

    parsed = parse_llm_error(content)
    if parsed is None:
        return False
    category, provider, detail = parsed
    console.print()
    if category == "auth":
        # No status code here: the auth bucket also fires on 403, on
        # PermissionDeniedError and on substring matches, so naming one would
        # be a guess. The detail carries the provider's own reason instead.
        where = f" ({escape(provider)})" if provider else ""
        console.print(f"[red]Error: provider rejected the credentials{where}: {escape(detail[:200])}[/red]")
        target = provider or "<name>"
        console.print(f"Fix: raven provider test {escape(target)}  or  raven onboard")
    else:
        console.print(f"[red]Error: LLM call failed ({escape(category)}): {escape(detail[:200])}[/red]")
        hint = _NON_AUTH_HINTS.get(category)
        if hint:
            console.print(hint)
    console.print()
    _ONE_SHOT_EXIT["code"] = 1
    return True


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
        markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
        logs: bool = typer.Option(False, "--logs/--no-logs", help="Show Raven runtime logs during chat"),
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
        """Run a one-shot agent turn (requires -m); interactive chat lives in `raven tui`."""
        if sum((session_id is not None, continue_, resume is not None)) > 1:
            raise typer.BadParameter("--session, --continue and --resume are mutually exclusive")

        if message is None:
            console.print(
                "[yellow]The interactive REPL was removed. Use [bold]raven tui[/bold] "
                'for interactive chat, or [bold]raven agent -m "..."[/bold] for a '
                "one-shot turn.[/yellow]"
            )
            raise typer.Exit(code=2)

        from loguru import logger

        from raven.agent.loop import AgentLoop
        from raven.agent.loop.recovery import limits_from_defaults
        from raven.cli._proactive_stack import (
            attach_sentinel_decision_consumer,
            attach_sentinel_spawn,
            build_sentinel_stack,
        )
        from raven.config.raven import load_raven_config
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

        # Build Sentinel stack if enabled — same wiring gateway uses, so the two
        # processes share state via ~/.raven/sentinel/state.json. Discover
        # triggers are dispatcher-side: only the gateway has real channel
        # adapters, so this process must NOT drain them or feishu/slack triggers
        # get consumed without delivery.
        sentinel_runner, sentinel_response_modifier, sentinel_on_user_inbound = build_sentinel_stack(
            config,
            sentinel_cfg,
            session_manager,
            provider,
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
            config.workspace_path,
            ec_config,
            registry=plugin_registry,
        )
        plugin_tools = build_plugin_tools(
            config.workspace_path,
            ec_config,
            registry=plugin_registry,
        )

        # No cron_service here: with the REPL gone this process is never a
        # cron runner, so registering CronTool would create jobs nothing
        # fires. Scripted reminder creation is `raven cron add` with an
        # explicit --channel.
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
            deep_research_config=config.tools.deep_research,
            exec_config=config.tools.exec,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            session_manager=session_manager,
            mcp_servers=config.tools.mcp_servers,
            disabled_tools=config.tools.disabled_tools,
            tool_search_config=config.tools.tool_search,
            sandbox_config=config.tools.sandbox,
            channels_config=config.channels,
            skill_forge_config=skill_forge_cfg,
            context_config=ec_config.context,
            runtime_config=ec_config.runtime,
            # ``-m "..."`` is a one-shot — no next turn for recovery
            # injection, so the ``"interactive"`` policy skips the
            # checkpoint here.
            interactive=False,
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
        # One-shot mode has no real ChannelManager — provide a minimal shim
        # that reports "cli" as the sole enabled channel so sentinel
        # (sentinel:direct) resolution targets the terminal instead of being
        # dropped.
        from types import SimpleNamespace

        cli_shim = SimpleNamespace(enabled_channels=["cli"])
        if sentinel_runner is not None:
            sentinel_runner.set_channel_manager(cli_shim)

        # Show spinner when logs are off (no output to miss); skip when logs are on
        def _thinking_ctx():
            if logs:
                from contextlib import nullcontext

                return nullcontext()
            return console.status("[dim]Raven is thinking...[/dim]", spinner="dots")

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
                        "memory backend start failed; continuing with legacy memory path",
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
                # as the bus path), but wire submit for parity with the TUI.
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

        _ONE_SHOT_EXIT["code"] = 0
        asyncio.run(run_once())
        # Native runtimes loaded by the agent loop (lancedb's Rust/tokio
        # thread, torch) segfault during interpreter finalization. The exit
        # chokepoint in raven.cli.commands.run hard-exits past finalization
        # when that hazard is live, so this path just returns normally.
        if _ONE_SHOT_EXIT["code"]:
            raise typer.Exit(_ONE_SHOT_EXIT["code"])


__all__ = ["register"]
