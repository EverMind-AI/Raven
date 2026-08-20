"""``reload.mcp`` RPC handler -- no-op MCP reloader.

Reaching it takes a deliberate ``/reload-mcp``, so answering it is a promise to
the user rather than a way to keep an automatic caller quiet: reloading MCP from
the TUI is not implemented, and this returns the shape that says nothing
happened rather than an error the slash command would have to explain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher


async def reload_mcp(params: dict) -> dict:
    """``reload.mcp`` — return the canonical no-op response shape.

    Ignores ``params`` entirely; future v0.2 evolution may interpret a
    ``force: bool`` flag, but the v0.1 contract is fixed.
    """
    return {"ok": True, "reloaded": 0, "tools_changed": False}


def register_reload_methods(dispatcher: "Dispatcher") -> None:
    """Register ``reload.mcp`` on a dispatcher instance."""
    dispatcher.register("reload.mcp", reload_mcp)


__all__ = ["reload_mcp", "register_reload_methods"]
