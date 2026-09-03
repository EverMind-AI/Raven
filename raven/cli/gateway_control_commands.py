"""`raven gateway reload|status|stop`: drive the running gateway's control plane.

These are the CLI clients of :mod:`raven.rpc.control`, through
:mod:`raven.gateway.live_probe`, which finds the daemon by the lock payload.
Transport only: each verb asks one question and renders the answer; every
"no gateway answered" is reported as that, never as a channel or runtime
fact.
"""

from __future__ import annotations

import asyncio
import time

import typer
from rich.console import Console

console = Console()


def register(gateway_app: typer.Typer) -> None:
    """Attach the control-plane verbs to the ``raven gateway`` group."""

    @gateway_app.command("status")
    def status() -> None:
        """Show the running gateway's generation, swap state and page."""
        from raven.gateway import live_probe

        info = asyncio.run(live_probe.status())
        if info is None:
            console.print("[yellow]No running gateway answered.[/yellow]")
            raise typer.Exit(1)
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.get("started_at", 0)))
        console.print(f"[green]✓[/green] gateway pid {info.get('pid')} (since {started})")
        console.print(f"  generation {info.get('generation')}  swap in flight: {info.get('swap_in_flight')}")
        console.print(f"  config: {info.get('config_path')}")
        page = info.get("page") or {}
        console.print(f"  page: {'mounted at ' + str(page.get('url')) if page.get('mounted') else 'not mounted'}")

    @gateway_app.command("reload")
    def reload(
        force: bool = typer.Option(False, "--force", help="Swap even while sub-agents or questions are in flight."),
    ) -> None:
        """Rebuild the runtime from config and swap it in, without a restart."""
        from raven.gateway import live_probe

        reply = asyncio.run(live_probe.reload(force=force))
        if reply is None:
            console.print("[yellow]No running gateway answered; nothing to reload.[/yellow]")
            raise typer.Exit(1)
        if not reply.get("ok"):
            reason = reply.get("reason", "refused")
            detail = ""
            if reason == "busy":
                detail = f" ({reply.get('subagents', 0)} sub-agents, {reply.get('questions', 0)} pending questions; --force overrides)"
            elif reason == "build_failed":
                detail = f": {reply.get('error', '')}"
            console.print(f"[red]✗[/red] reload refused: {reason}{detail}")
            raise typer.Exit(1)
        console.print(
            f"[green]✓[/green] generation {reply.get('generation')} built; the running generation stops within "
            f"~1s, in-flight turns get {reply.get('grace_s')}s before they are cancelled."
        )

    @gateway_app.command("stop")
    def stop() -> None:
        """Stop the running gateway gracefully through its control plane."""
        from raven.gateway import live_probe

        try:
            from raven.cli.serve_commands import _read_web_state

            supervised = _read_web_state() is not None
        except Exception:
            supervised = False
        if supervised:
            console.print(
                "[yellow]A `raven web` supervisor is running and would restart the gateway; "
                "use `raven web --stop` to stop both.[/yellow]"
            )
            raise typer.Exit(1)
        if not asyncio.run(live_probe.shutdown()):
            console.print("[yellow]No running gateway answered.[/yellow]")
            raise typer.Exit(1)
        console.print("[green]✓[/green] gateway stopping")


__all__ = ["register"]
