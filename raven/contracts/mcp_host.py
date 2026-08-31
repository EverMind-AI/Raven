"""The MCP control face of an assembled loop.

Versioned with the factory loop: the members below are the loop's own doors,
and they move when the loop's MCP glue moves. The face exists for the two
plugin-market machines that operate the MCP organ from outside it -- the RPC
panel's ``plug.*`` methods and the agent's own ``plugin`` tool. Both are
handed the loop; this paper is the whole of what they may reach of it: the
config reconcile (the one entry point for changing the server set inside a
running generation), the connection manager (the MCP organ itself, whose
per-server verbs -- status / connect / disconnect / tool_map -- are its own
public vocabulary), and the executor provider a sandboxed stdio connect runs
under. The entrance-guard roster pins who spells these reaches; this paper is
what those spellings are measured against.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class McpHost(Protocol):
    """What the plugin-market machinery may hold of the loop."""

    @property
    def mcp_manager(self) -> Any:
        """The MCP connection organ: per-server lifecycle and the tool map."""
        ...

    async def apply_mcp_config(self, cfg_servers: dict, *, attempts: dict | None = None) -> Any:
        """Reconcile live MCP connections with the already-written config."""
        ...

    async def mcp_executor_provider(self) -> Any:
        """The sandbox executor an MCP stdio connect runs under, started."""
        ...


__tier__ = "factory_loop"
__all__ = ["McpHost"]
