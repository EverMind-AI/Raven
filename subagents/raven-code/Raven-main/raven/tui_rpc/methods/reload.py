"""``reload.mcp`` RPC handler — no-op MCP reloader for v0.1.

Why this exists
---------------

hermes's fork-imported ``useConfigSync.ts:202`` polls ``reload.mcp`` every 5
seconds, hard-coded. The TUI side does NOT modify that polling loop, so
the server side must respond cleanly to every probe. MCP admin in the TUI is
out of scope for v0.1 — we therefore make ``reload.mcp`` a deterministic no-op
that returns ``{"ok": true, "reloaded": 0, "tools_changed": false}`` and
**NEVER raises**. Raising would spam the hermes log every 5s and degrade the UX.

If the idle cost of the 5-second polling no-op shows measurable log noise /
CPU jitter, we may add a server-side throttle that coalesces back-to-back
polls — but the hermes call frequency itself cannot be touched (it's pulled
from the fork).
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
