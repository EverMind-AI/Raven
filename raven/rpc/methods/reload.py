"""``reload.mcp`` RPC handler -- reconcile the live MCP server set with config.

Two callers, with different needs, and the contract has to serve both:

* ``/reload-mcp`` from the TUI, typed by a person. Reloading moves the tool
  list, and the tool list is the first thing in the prompt-cache prefix, so
  every change costs a full rebuild of the cached prompt. That is worth a
  confirmation, which is why the command asks for one unless the caller sends
  ``confirm``.
* a config-file watch, which fires on a timer. This one must be cheap when
  nothing changed, so the handler asks the manager first and returns without
  touching a transport when the answer is "nothing to do".

Response shape, matching what the TUI reads (``status`` decides which line it
prints; ``reloaded``/``tools_changed`` are for callers that want the detail):

    {"ok": true, "status": "reloaded" | "noop" | "confirm_required",
     "message": str, "reloaded": int, "tools_changed": bool}
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

_CONFIRM_MESSAGE = (
    "/reload-mcp reconnects the MCP servers whose config changed. The tool list "
    "is the first thing in the cached prompt prefix, so a change to it rebuilds "
    "the whole cache for this conversation. Run '/reload-mcp now' to proceed."
)


def _noop(message: str) -> dict:
    return {"ok": True, "status": "noop", "message": message, "reloaded": 0, "tools_changed": False}


async def reload_mcp(params: dict, *, agent_loop_factory: Any = None) -> dict:
    """``reload.mcp`` -- re-read the MCP server config and reconcile.

    ``confirm`` skips the confirmation gate for this call. There is no "and
    stop asking": persisting that needs a settings surface this config has no
    section for, and a flag that claims to be remembered and is not is worse
    than not offering it -- which is what shipped, the TUI telling the user
    "future /reload-mcp will run without confirmation" over a server-side log
    line saying the opposite.
    """
    loop = None
    if agent_loop_factory is not None:
        try:
            loop = agent_loop_factory()
        except Exception:  # noqa: BLE001 -- no live loop is a valid answer, not a fault
            loop = None
    if loop is None or not hasattr(loop, "apply_mcp_config"):
        return _noop("no live agent loop to reload; the next one reads the config as written")

    from raven.config.loader import load_config

    try:
        servers = load_config().tools.mcp_servers
    except (ValueError, OSError) as e:
        return _noop(f"config could not be read: {e}")

    # The cheap gate, ahead of the confirmation: asking a person to approve a
    # reload that would do nothing is worse than not offering it.
    if not loop.mcp_config_changed(servers):
        return _noop("MCP servers already match the config; nothing to reload")

    if not params.get("confirm"):
        return {
            "ok": True,
            "status": "confirm_required",
            "message": _CONFIRM_MESSAGE,
            "reloaded": 0,
            "tools_changed": False,
        }

    # Guarded because the contract above is MUST NOT throw and this call does:
    # ``apply_config`` re-raises a ``SandboxInitError`` from any attempt, and any
    # other exception from one, once every pending server has had its turn. A
    # reconcile that failed is an answer -- ``noop`` with the reason, which is
    # the branch ops.ts already prints ``message`` from -- not a JSON-RPC error
    # for the caller to explain.
    try:
        report = await loop.apply_mcp_config(servers)
    except Exception as e:  # noqa: BLE001 -- see above; a transport fault is not a protocol fault
        logger.warning("reload.mcp: the reconcile failed: {}", e)
        return _noop(f"reload failed: {type(e).__name__}: {e}")

    # "reconciled", not "reconnected". The count is records touched -- a detach
    # counts, and so does an attempt that failed or parked at the browser -- so
    # naming it a reconnect reported a total outage as a successful reload.
    tools = "the tool list changed" if report.tools_changed else "the tool list is unchanged"
    return {
        "ok": True,
        "status": "reloaded",
        "message": f"reconciled {report.reloaded} MCP server(s); {tools}",
        **report.as_dict(),
    }


def register_reload_methods(dispatcher: "Dispatcher", *, agent_loop_factory: Any = None) -> None:
    """Register ``reload.mcp`` on a dispatcher instance."""

    async def _handler(params: dict) -> dict:
        return await reload_mcp(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("reload.mcp", _handler)


__all__ = ["reload_mcp", "register_reload_methods"]
