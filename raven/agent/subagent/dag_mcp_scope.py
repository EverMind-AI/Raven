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

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from raven.config.schema import MCPServerConfig

_EMPTY: Mapping[str, MCPServerConfig] = {}


# A callable, not a mapping: the definitions are re-rendered on every read, so a
# credential stored after the run started reaches a node that is re-dispatched
# (``continue``) without anyone re-entering the scope. The background task a
# dispatch leaves behind copied this context, so it holds the callable and calls
# it fresh each time.
_RUN_SERVERS: ContextVar[Callable[[], Mapping[str, MCPServerConfig]]] = ContextVar(
    "dag_run_mcp_servers", default=lambda: _EMPTY
)
# The playbook whose credentials the run's definitions may use, or None for a
# run that carries none. Read where an OAuth server is dialled or its tokens
# are looked for, so a carried server never touches the host's token file.
_RUN_SCOPE: ContextVar[str | None] = ContextVar("dag_run_mcp_scope", default=None)
# The run's carried servers whose credential reference did not resolve. Read
# where a grant is decided, so such a server is reported as not delivered with
# the sentence that says where the credential is set, rather than handed over
# to 401 on its first call. A callable for the same reason the definitions are:
# a credential stored mid-run must change the answer for a continued node.
_RUN_GAPS: ContextVar[Callable[[], frozenset[str]]] = ContextVar("dag_run_mcp_gaps", default=frozenset)


@contextmanager
def run_mcp_scope(
    servers: Mapping[str, MCPServerConfig] | Callable[[], Mapping[str, MCPServerConfig]] | None,
    *,
    scope: str | None = None,
    credential_gaps: "Callable[[], frozenset[str]] | None" = None,
) -> Iterator[None]:
    """Make ``servers`` resolvable by name for the duration of one run.

    Entries that are not already validated :class:`MCPServerConfig` objects are
    dropped rather than parsed. This is an in-process hand-off between the
    playbook engine and the graph tool, and the graph tool is also a model-facing
    tool whose argument schema does not close over undeclared keys -- a model that
    guessed this parameter's name would otherwise be defining an MCP server, and
    a stdio definition is a command line. Wire data cannot arrive as a
    ``MCPServerConfig`` instance, so requiring one is the whole gate.
    """
    render: Callable[[], Mapping[str, MCPServerConfig]] = servers if callable(servers) else (lambda: servers or {})

    def guarded() -> Mapping[str, MCPServerConfig]:
        # The gate holds on every read, not only at entry: a callable is where a
        # later reader could otherwise slip an unvalidated definition in.
        return {name: cfg for name, cfg in (render() or {}).items() if isinstance(cfg, MCPServerConfig)}

    token = _RUN_SERVERS.set(guarded)
    scope_token = _RUN_SCOPE.set(scope)
    gaps_token = _RUN_GAPS.set(credential_gaps or frozenset)
    try:
        yield
    finally:
        _RUN_GAPS.reset(gaps_token)
        _RUN_SCOPE.reset(scope_token)
        _RUN_SERVERS.reset(token)


def run_mcp_servers() -> Mapping[str, MCPServerConfig]:
    """This run's own definitions as they read right now, empty outside a run."""
    return _RUN_SERVERS.get()()


def run_mcp_credential_scope() -> str | None:
    """The credential scope of the run in progress, None outside one or for a run with none."""
    return _RUN_SCOPE.get()


def run_mcp_credential_gaps() -> frozenset[str]:
    """This run's carried servers whose credential is not set on this machine."""
    return _RUN_GAPS.get()()


__all__ = ["run_mcp_credential_gaps", "run_mcp_credential_scope", "run_mcp_scope", "run_mcp_servers"]
