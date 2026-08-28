"""MCP server *definitions* scoped to one graph run.

The third scope in a system that already had two. The host's own
``tools.mcpServers`` is process-level, and a sub-agent that brings servers of its
own gets a per-session overlay (``raven.agent.tools.registry``). A playbook that
ships an ``mcpServers`` section fits neither: those definitions belong to one
dispatch of one playbook, and must be invisible to the main agent and to every
other run in flight.

**Only the definition lookup is scoped here.** Nothing is connected, nothing is
registered, and the host's ``MCPConnectionManager`` is never touched -- so the
main agent's tool list cannot move because a playbook ran. It does not need to
be touched: ``raven.mcp.endpoint`` already dials a fresh upstream per (node,
server) at dispatch time, which is why two concurrent runs naming one server do
not share a service. What a run in a conversation was missing was never a
service, only the config seam answering to the name -- ``resolve_grant`` asked
``source.server(name)``, the host's mapping had never heard of it, and the server
came back ``not_configured``.

**Nothing here merges the two mappings.** :func:`run_mcp_servers` is read by the
host's MCP source (``AgentLoop``'s ``LiveMcpSource``) as a *second* mapping
beside the process-level one, and the source decides precedence -- playbook
first -- for the definition, the connection state and the tool wrappers
together. Merging here and handing one mapping over would put the precedence
rule in a place that cannot say whose definition won, and a shadowed name would
then take the host server's live state and wrappers while carrying the run's
config: an in-process node reaching a different service than the one its config
names.

A :class:`~contextvars.ContextVar` rather than an argument threaded down the
call chain, for two reasons. The reader is that source, which sits below a
backend this package must not reach into, so there is no parameter to thread.
And per-task values are exactly the isolation two concurrent runs need:
``asyncio.create_task`` copies the context, so a run's definitions follow its own
task -- including the background task a dispatch leaves behind -- and reach no
other run's.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from raven.config.schema import MCPServerConfig

_EMPTY: Mapping[str, MCPServerConfig] = {}

_RUN_SERVERS: ContextVar[Mapping[str, MCPServerConfig]] = ContextVar("dag_run_mcp_servers", default=_EMPTY)


@contextmanager
def run_mcp_scope(servers: Mapping[str, MCPServerConfig] | None) -> Iterator[None]:
    """Make ``servers`` resolvable by name for the duration of one run.

    Entries that are not already validated :class:`MCPServerConfig` objects are
    dropped rather than parsed. This is an in-process hand-off between the
    playbook engine and the graph tool, and the graph tool is also a model-facing
    tool whose argument schema does not close over undeclared keys -- a model that
    guessed this parameter's name would otherwise be defining an MCP server, and
    a stdio definition is a command line. Wire data cannot arrive as a
    ``MCPServerConfig`` instance, so requiring one is the whole gate.
    """
    scoped = {name: cfg for name, cfg in (servers or {}).items() if isinstance(cfg, MCPServerConfig)}
    token = _RUN_SERVERS.set(scoped or _EMPTY)
    try:
        yield
    finally:
        _RUN_SERVERS.reset(token)


def run_mcp_servers() -> Mapping[str, MCPServerConfig]:
    """This run's own definitions, empty outside a run."""
    return _RUN_SERVERS.get()


__all__ = ["run_mcp_scope", "run_mcp_servers"]
