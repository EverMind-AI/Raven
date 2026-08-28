"""``raven mcp bridge``: the frame pump's process shell.

A sub-agent spawns this and speaks newline-delimited JSON-RPC to its stdin and
stdout, so the only bytes allowed on stdout are frames. ``claim_stdout`` is what
enforces that: it moves fd 1 to stderr and hands back a duplicate, so a library
that prints on import cannot corrupt the stream.
"""

from __future__ import annotations

import asyncio

import typer

from raven.acp.stdio import claim_stdout
from raven.mcp.bridge import run_bridge

mcp_app = typer.Typer(name="mcp", help="MCP plumbing for sub-agents.")


@mcp_app.command("bridge")
def bridge(socket_path: str = typer.Argument(..., help="Unix socket the host is listening on")) -> None:
    """Relay MCP frames between this process's stdio and a host-held server."""
    with claim_stdout() as frames:
        code = asyncio.run(run_bridge(socket_path, frames))
    raise typer.Exit(code)
