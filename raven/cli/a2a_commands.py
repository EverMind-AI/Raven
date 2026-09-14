"""`raven a2a serve`: the A2A face without a gateway.

The headless hosting. The gateway-mounted one is ``gate.mount_if_allowed``,
called from the app builder; both refuse in a sub-agent process through the same
check, so neither can be the one that forgot.
"""

from __future__ import annotations

import typer

from raven.a2a.gate import refuse_if_subagent

a2a_app = typer.Typer(name="a2a", help="Serve the A2A protocol face.", subcommand_metavar="")


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
    typer.echo(f"Serving A2A on http://{host}:{port}")
