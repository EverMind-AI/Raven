"""`raven a2a`: the inbound A2A face, and the switch that opens it.

``serve`` is the headless hosting. The gateway-mounted one is
``gate.mount_gateway_face``, called from the app builder; both refuse in a
sub-agent process through the same check, so neither can be the one that forgot.

The two are not gated alike, which is easy to misread as an oversight: the config
flag decides only the gateway-mounted face, because running ``serve`` is itself
the opt-in for that one. ``enable`` and ``disable`` write that flag, and they are
the only thing that does -- an install finishes with the face closed, so opening
a second network surface is always something an operator typed.
"""

from __future__ import annotations

import typer

from raven.a2a.gate import refuse_if_subagent

a2a_app = typer.Typer(name="a2a", help="Serve and switch the A2A protocol face.")


@a2a_app.callback()
def _a2a_group() -> None:
    # A Typer app with exactly one command and no callback collapses into that
    # command directly, dropping its name (Typer's `get_command`): `serve` would
    # stop being a subcommand name and become an unexpected positional argument
    # to itself. This no-op callback keeps `a2a` a real group so `serve` stays
    # addressable by name, both as `raven a2a serve` and under a direct
    # `CliRunner.invoke(a2a_app, ["serve"])`.
    pass


@a2a_app.command("serve")
def serve(
    port: int = typer.Option(8710, help="Port to bind."),
    host: str = typer.Option("127.0.0.1", help="Address to bind."),
) -> None:
    """Run the A2A server standalone."""
    reason = refuse_if_subagent()
    if reason is not None:
        typer.echo(f"Refusing to start: {reason}", err=True)
        raise typer.Exit(code=1)

    import asyncio

    from loguru import logger

    from raven.a2a.runtime import make_run_turn, serve_standalone
    from raven.agent.loop.bundles import TurnPolicy
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config
    from raven.core.engine_stack import build_local_sessions
    from raven.core.provider_stack import build_model_routing
    from raven.core.runtime import build_runtime
    from raven.providers.factory import make_provider

    cfg = load_config()
    ec_config = load_raven_config()
    provider = make_provider(cfg)
    router, provider = build_model_routing(cfg, provider)
    session_manager, workdir_resolver = build_local_sessions(cfg, workspace=None)
    runtime = build_runtime(
        cfg,
        ec_config,
        provider=provider,
        session_manager=session_manager,
        router=router,
        workdir_resolver=workdir_resolver,
        policy=TurnPolicy(interactive=False),
    )
    run_turn = make_run_turn(runtime.loop)

    async def _serve() -> None:
        # `raven a2a serve` is a resident host serving many turns over its whole
        # lifetime, not a one-shot `raven agent -m` invocation: build_runtime only
        # builds contributed plugin services inert, so a resident host is the one
        # that must start them, and dispose() is what retires the full generation
        # (services, MCP, backend) together on the way out.
        if runtime.backend is not None:
            try:
                await runtime.backend.start()
            except Exception:
                logger.exception("memory backend start failed; continuing with legacy memory path")
        await runtime.loop.start_plugin_services()
        try:
            await serve_standalone(cfg.a2a, host=host, port=port, run_turn=run_turn)
        finally:
            await runtime.dispose()

    typer.echo(f"Serving A2A on http://{host}:{port}")
    asyncio.run(_serve())


@a2a_app.command("enable")
def enable() -> None:
    """Open the gateway-mounted A2A face, minting a token if there is none."""
    from raven.config.update import set_a2a_server_enabled

    minted = set_a2a_server_enabled(True)
    if minted is not None:
        typer.echo("Minted an inbound token into a2a.server.token.")
    typer.echo("A2A inbound face enabled.")
    # The face is mounted while the app is built, so a process already serving
    # was built against the old flag and will not pick this up on its own.
    typer.echo("Restart `raven gateway` (or `raven web`) for it to take effect.")


@a2a_app.command("disable")
def disable() -> None:
    """Close the gateway-mounted A2A face, keeping the token for later."""
    from raven.config.update import set_a2a_server_enabled

    set_a2a_server_enabled(False)
    typer.echo("A2A inbound face disabled.")
    typer.echo("Restart `raven gateway` (or `raven web`) for it to take effect.")
