"""Orca-compatible terminal commands backed by the running Raven WebSocket gateway."""

from __future__ import annotations

import typer

from raven.cli import _terminal_rpc

terminal_app = typer.Typer(help="Create, inspect and communicate with hosted terminals.", no_args_is_help=True)


def worktree_selector(value):
    if value is None:
        return None
    return value.removeprefix("id:")


@terminal_app.command("list")
def terminal_list(
    worktree: str | None = typer.Option(None, "--worktree", help="Exact worktree id:<repo-id>::<path>"),
    limit: int = typer.Option(1000, "--limit", min=1, max=1000000),
    include_visual_layouts: bool = typer.Option(False, "--include-visual-layouts"),
    environment: str | None = typer.Option(None, "--environment"),
    json_output: bool = typer.Option(False, "--json"),
):
    """List hosted terminals, including live terminals whose tabs are hidden."""
    params = {"limit": limit, "include_visual_layouts": include_visual_layouts}
    if worktree is not None:
        params["worktree_id"] = worktree_selector(worktree)
    _terminal_rpc.run("terminal.list", params, environment=environment, json_output=json_output)


@terminal_app.command("show")
def terminal_show(
    terminal: str = typer.Option(..., "--terminal"),
    environment: str | None = typer.Option(None, "--environment"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Show one terminal by exact handle."""
    _terminal_rpc.run("terminal.show", {"handle": terminal}, environment=environment, json_output=json_output)


@terminal_app.command("send")
def terminal_send(
    terminal: str = typer.Option(..., "--terminal"),
    text: str = typer.Option(..., "--text"),
    enter: bool = typer.Option(False, "--enter", help="Submit the provider composer after pasting"),
    require_ack: bool = typer.Option(False, "--require-ack"),
    environment: str | None = typer.Option(None, "--environment"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Paste text into a terminal and optionally submit or wait for content ACK."""
    if len(text) > 1200:
        typer.echo("Warning: long terminal message; prefer a short summary and an absolute file path.", err=True)
    _terminal_rpc.run(
        "terminal.send",
        {"handle": terminal, "text": text, "enter": enter, "require_ack": require_ack},
        environment=environment,
        json_output=json_output,
        timeout_ms=310000 if require_ack else 60000,
    )


@terminal_app.command("wait")
def terminal_wait(
    terminal: str = typer.Option(..., "--terminal"),
    condition: str = typer.Option("tui-idle", "--for", help="tui-idle or exit"),
    timeout_ms: int = typer.Option(300000, "--timeout-ms", min=1, max=3600000),
    environment: str | None = typer.Option(None, "--environment"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Wait for the provider composer to accept a prompt, or for process exit."""
    _terminal_rpc.run(
        "terminal.wait",
        {"handle": terminal, "for": condition, "timeout_ms": timeout_ms},
        environment=environment,
        json_output=json_output,
        timeout_ms=timeout_ms + 10000,
    )


@terminal_app.command("create")
def terminal_create(
    worktree: str = typer.Option(..., "--worktree", help="Exact worktree id:<repo-id>::<path>"),
    command: str = typer.Option(..., "--command"),
    title: str = typer.Option("Terminal", "--title"),
    environment: str | None = typer.Option(None, "--environment"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Create a hosted terminal in the selected worktree."""
    _terminal_rpc.run(
        "terminal.create",
        {"worktree_id": worktree_selector(worktree), "command": command, "title": title},
        environment=environment,
        json_output=json_output,
    )


@terminal_app.command("close")
def terminal_close(
    terminal: str = typer.Option(..., "--terminal"),
    environment: str | None = typer.Option(None, "--environment"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Close one terminal owned by the caller."""
    _terminal_rpc.run("terminal.close", {"handle": terminal}, environment=environment, json_output=json_output)


@terminal_app.command("rename", hidden=True)
def terminal_rename(
    terminal: str = typer.Option(..., "--terminal"),
    title: str = typer.Option(..., "--title"),
    environment: str | None = typer.Option(None, "--environment"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Verify an existing canonical title projection without changing the name."""
    _terminal_rpc.run(
        "terminal.rename", {"handle": terminal, "title": title}, environment=environment, json_output=json_output
    )
