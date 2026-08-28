"""Resolve one dispatch's MCP server grant and project it to a backend."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import MCPServerConfig
from raven.mcp.client import MCPToolWrapper, resolve_transport
from raven.mcp.endpoint import bridge_command
from raven.mcp.naming import MCPToolRef

GrantKind = Literal["raven-loop", "raven-cli", "acp"]
Transport = Literal["stdio", "http", "sse"]
MissingReason = Literal[
    "not_configured",
    "disabled",
    "invalid_transport",
    "not_connected",
    "auth_required",
    "no_tools",
    "command_not_found",
]
WithheldReason = Literal[
    "oauth_interaction",
    "secret_export_denied",
]


@dataclass(frozen=True)
class McpServerView:
    """One configured MCP server and its host-side connection state."""

    name: str
    config: MCPServerConfig = field(repr=False)
    state: str | None


class McpSource(Protocol):
    """Read-only view of the host's live MCP configuration and tools."""

    def server(self, name: str) -> McpServerView | None: ...

    def tools(self, name: str) -> tuple["GrantedTool", ...]: ...

    def disabled_tools(self) -> frozenset[str]: ...

    def executor_provider(self) -> Any | None:
        """The sandbox executor a bridged upstream must be spawned inside.

        Carried here rather than threaded separately because this object already
        is the host's MCP view, and the setter that hands it to a backend already
        exists. ``None`` means no confinement is configured; a backend that
        cannot reach one still refuses a stdio server the sandbox would have had
        to contain, rather than spawning it on the host.
        """
        return None


class LiveMcpSource:
    """Combine public manager and registry seams into a live MCP source.

    ``run_servers`` is the second definition mapping this source may answer from:
    the servers one graph run brought with it, which shadow the host's by name
    (see ``raven.agent.subagent.dag_mcp_scope``). Precedence is realized here
    rather than in a merged mapping handed to ``config_getter``, because the three
    seams below have to agree on *which* definition they are describing. A merged
    mapping loses that: the config would be the run's while the state and the
    wrappers still described the host server that happens to share the name, and
    an in-process node would be granted live wrappers onto a different service
    than the one its config names.
    """

    def __init__(
        self,
        config_getter: Callable[[], dict[str, MCPServerConfig]],
        manager_getter: Callable[[], Any | None],
        registry: ToolRegistry,
        disabled_tools_getter: Callable[[], frozenset[str]],
        executor_provider: Any | None = None,
        run_servers: Callable[[], Mapping[str, MCPServerConfig]] | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._manager_getter = manager_getter
        self._registry = registry
        self._disabled_tools_getter = disabled_tools_getter
        self._executor_provider = executor_provider
        self._run_servers = run_servers

    def _selected(self, name: str) -> tuple[MCPServerConfig | None, bool]:
        """The definition answering to ``name``, and whether host live state describes it.

        Run-scoped definitions are never dialled by this process: the host's
        manager was configured from ``tools.mcpServers`` and nothing adds to it
        for a run, which is deliberate -- a bridged sub-agent's upstream is dialled
        per (dispatch, server) by ``raven.mcp.endpoint``. So a shadowing definition
        has no connection state and no tool wrappers of its own, and the host's
        cannot be borrowed for it.

        Equality is the one case where they can: a run that re-declares a server
        the host already has, field for field, has named the same service, so
        there is no provenance to confuse. That is what keeps a portable playbook
        -- one that ships a definition so it runs anywhere -- from costing an
        in-process node the wrappers it would have had on a host that also has it.
        """
        host = self._config_getter().get(name)
        run = self._run_servers().get(name) if self._run_servers is not None else None
        if run is None:
            return host, True
        return run, run == host

    def server(self, name: str) -> McpServerView | None:
        config, host_state_applies = self._selected(name)
        if config is None:
            return None
        if not host_state_applies:
            return McpServerView(name=name, config=config, state=None)
        manager = self._manager_getter()
        states = {row["name"]: row.get("state") for row in manager.status()} if manager is not None else {}
        return McpServerView(name=name, config=config, state=states.get(name))

    def tools(self, name: str) -> tuple["GrantedTool", ...]:
        if not self._selected(name)[1]:
            return ()
        granted: list[GrantedTool] = []
        for registered in self._registry.names_from(name):
            wrapper = self._registry.get(registered)
            ref = self._registry.origin_of(registered)
            if isinstance(wrapper, MCPToolWrapper) and ref is not None:
                granted.append(GrantedTool(wrapper=wrapper, ref=ref))
        return tuple(granted)

    def disabled_tools(self) -> frozenset[str]:
        return self._disabled_tools_getter()

    def executor_provider(self) -> Any | None:
        return self._executor_provider


@dataclass(frozen=True)
class McpGrantTarget:
    """All receiver-specific policy needed to decide a grant."""

    kind: GrantKind
    require_connected: bool
    allow_secrets: bool = False
    stdio_path: str | None = None
    disabled_tools: Literal["live", "copy", "bridge", "ignore"] = "ignore"
    """Who applies the host's tool off-switch for this receiver.

    ``live`` -- nobody has to: the receiver shares the host's registry, which
    already withholds a disabled tool per request. ``copy`` -- the set is
    written into the child's own config, which is the only handle a separate
    raven process offers. ``bridge`` -- the relay enforces it on the wire, the
    one place a transparently bridged child can be held to it. ``ignore`` --
    nothing enforces it, which is only ever right for a receiver that gets no
    MCP at all."""
    exports_definition: bool = False
    """Whether this receiver reads the server definition itself.

    True only for the vendored CLI child, whose config file carries ``env`` and
    ``headers`` verbatim. The two credential gates below hang off this rather
    than off ``kind``, because what they are about is a definition crossing a
    process boundary -- and on a bridged path nothing crosses: the sub-agent is
    handed one stdio stanza naming a socket, with an empty ``env``."""


@dataclass(frozen=True)
class GrantedTool:
    """One live host MCP wrapper and its recorded origin."""

    wrapper: MCPToolWrapper = field(repr=False)
    ref: MCPToolRef


@dataclass(frozen=True)
class GrantedServer:
    """One server that passed every policy check for this receiver."""

    name: str
    transport: Transport
    config: MCPServerConfig = field(repr=False)
    tools: tuple[GrantedTool, ...] = ()
    command: str | None = None
    socket_path: str | None = None
    """The endpoint a bridged sub-agent connects to, filled in by
    :meth:`McpGrant.with_endpoints` once the host has opened it."""


@dataclass(frozen=True)
class MissingServer:
    """A declared server that cannot be delivered in the current state."""

    name: str
    reason: MissingReason


@dataclass(frozen=True)
class WithheldServer:
    """A valid server definition held back by receiver policy."""

    name: str
    reason: WithheldReason
    transport: Transport | None = None


@dataclass(frozen=True)
class McpGrant:
    """The final server grant for one sub-agent dispatch."""

    granted: tuple[GrantedServer, ...] = ()
    missing: tuple[MissingServer, ...] = ()
    withheld: tuple[WithheldServer, ...] = ()
    disabled_tools: tuple[str, ...] = ()
    """The host's off-switch, for the receiver whose policy says it has to carry
    it (:attr:`McpGrantTarget.disabled_tools`). One field rather than one per
    mode: it is one fact -- which tools the host withheld -- and the mode
    decides who applies it, not what it says."""

    def for_registry(self) -> tuple[tuple[MCPToolWrapper, MCPToolRef], ...]:
        """Live wrappers to register in an in-process Raven tool registry."""
        return tuple((tool.wrapper, tool.ref) for server in self.granted for tool in server.tools)

    def for_child_config(self, child_disabled_tools: Sequence[str] = ()) -> dict[str, Any]:
        """A Raven ``tools`` stanza for a vendored CLI launcher."""
        servers: dict[str, Any] = {}
        for server in self.granted:
            payload = server.config.model_dump(by_alias=True, exclude={"enabled", "auth", "oauth"})
            servers[server.name] = payload
        disabled = sorted(set(self.disabled_tools) | set(child_disabled_tools))
        return {"mcpServers": servers, "disabledTools": disabled}

    def with_endpoints(self, paths: Mapping[str, str]) -> "McpGrant":
        """A copy whose granted servers carry the endpoints the host opened."""
        return replace(
            self,
            granted=tuple(
                replace(server, socket_path=paths[server.name]) if server.name in paths else server
                for server in self.granted
            ),
        )

    def for_acp(self, bridge_argv: Sequence[str]) -> list[dict[str, Any]]:
        """ACP SDK wire objects for ``session/new`` and ``session/load``.

        Every server projects to the same shape whatever its upstream transport
        is: one stdio stanza pointing at a host endpoint. ``env`` is empty by
        construction -- the definition and its secrets stay on the host, and the
        socket's 0600 mode is the whole of the boundary.
        """
        out: list[dict[str, Any]] = []
        for server in self.granted:
            if server.socket_path is None:
                raise ValueError(f"MCP server {server.name!r} has no endpoint; call with_endpoints first")
            out.append(
                {
                    "name": server.name,
                    "command": bridge_argv[0],
                    "args": [*bridge_argv[1:], server.socket_path],
                    "env": [],
                }
            )
        return out

    def note_text(self) -> str:
        """Human-readable, secret-free explanation of every degraded server."""
        notes = [_missing_note(item) for item in self.missing]
        notes.extend(_withheld_note(item) for item in self.withheld)
        return "; ".join(notes)


class McpDispatchError(RuntimeError):
    """A failed dispatch plus the secret-free MCP degradation that accompanied it."""


@contextmanager
def annotate_mcp_failure(grant: McpGrant) -> Iterator[None]:
    """Keep degradation details on a failed dispatch without mutating its exception."""
    try:
        yield
    except Exception as exc:
        if note := grant.note_text():
            from raven.agent.subagent.backends.base import SubagentActionAbortedError

            if isinstance(exc, SubagentActionAbortedError):
                raise
            raise McpDispatchError(f"{exc}\n\n[raven] {note}.") from exc
        raise


def raven_loop_target() -> McpGrantTarget:
    """Policy for sharing the host's already-connected wrappers."""
    return McpGrantTarget(kind="raven-loop", require_connected=True, disabled_tools="live")


def raven_cli_target(*, allow_secrets: bool) -> McpGrantTarget:
    """Policy for a vendored Raven CLI child config."""
    return McpGrantTarget(
        kind="raven-cli",
        require_connected=False,
        allow_secrets=allow_secrets,
        disabled_tools="copy",
        exports_definition=True,
    )


def acp_target(*, allow_secrets: bool, stdio_path: str | None) -> McpGrantTarget:
    """Policy for an ACP sub-agent, which is handed bridge endpoints only.

    No transport gate: every upstream reaches the adapter as one stdio stanza,
    so what the adapter advertised about http or sse no longer decides anything.

    ``disabled_tools="bridge"``, not ``"ignore"``: the child speaks MCP straight
    to the real server, so nothing between them withholds a tool on its own --
    it would list and call one the host had switched off. The set therefore
    travels with the grant and the endpoint enforces it on the wire, which is
    what keeps the registry's promise that a withheld tool is unreachable
    everywhere.
    """
    return McpGrantTarget(
        kind="acp",
        require_connected=False,
        allow_secrets=allow_secrets,
        stdio_path=stdio_path,
        disabled_tools="bridge",
    )


def resolve_grant(names: Sequence[str] | None, source: McpSource | None, target: McpGrantTarget) -> McpGrant:
    """Resolve server names once, applying all receiver policy before projection."""
    requested = tuple(dict.fromkeys(names or ()))
    if not requested:
        return McpGrant(disabled_tools=_disabled_tools(source, target))
    if source is None:
        return McpGrant(missing=tuple(MissingServer(name, "not_connected") for name in requested))

    granted: list[GrantedServer] = []
    missing: list[MissingServer] = []
    withheld: list[WithheldServer] = []
    for name in requested:
        view = source.server(name)
        if view is None:
            missing.append(MissingServer(name, "not_configured"))
            continue
        cfg = view.config
        if not cfg.enabled:
            missing.append(MissingServer(name, "disabled"))
            continue
        transport = _transport(cfg)
        if transport is None:
            missing.append(MissingServer(name, "invalid_transport"))
            continue
        if target.require_connected:
            if view.state == "auth_required":
                missing.append(MissingServer(name, "auth_required"))
                continue
            if view.state != "connected":
                missing.append(MissingServer(name, "not_connected"))
                continue
            tools = source.tools(name)
            if not tools:
                missing.append(MissingServer(name, "no_tools"))
                continue
            granted.append(GrantedServer(name, transport, cfg, tools=tools))
            continue
        if target.exports_definition:
            if cfg.auth == "oauth":
                withheld.append(WithheldServer(name, "oauth_interaction", transport))
                continue
            if (cfg.auth == "apikey" or cfg.env or cfg.headers) and not target.allow_secrets:
                withheld.append(WithheldServer(name, "secret_export_denied", transport))
                continue
        elif cfg.auth == "oauth" and view.state != "connected":
            # A bridged receiver never sees the credential, so the ``auth`` label
            # decides nothing on its own -- but an endpoint whose upstream is
            # still unauthorized hands the sub-agent a server that 401s on its
            # first call, which reads as a broken tool rather than a missing one.
            missing.append(MissingServer(name, "auth_required"))
            continue
        command = None
        if target.kind == "acp":
            # What has to be findable is raven, not the server: the adapter is
            # handed a bridge command, and the server's own command is spawned
            # on this side.
            argv = bridge_command(path=target.stdio_path)
            if argv is None:
                missing.append(MissingServer(name, "command_not_found"))
                continue
            command = argv[0]
        granted.append(GrantedServer(name, transport, cfg, command=command))

    return McpGrant(tuple(granted), tuple(missing), tuple(withheld), _disabled_tools(source, target))


def _disabled_tools(source: McpSource | None, target: McpGrantTarget) -> tuple[str, ...]:
    """The host off-switch this grant has to carry, or empty when it need not.

    Read once here rather than at each projection so the snapshot a dispatch
    acts on is the one taken when it was resolved.
    """
    if source is None or target.disabled_tools not in {"copy", "bridge"}:
        return ()
    return tuple(sorted(source.disabled_tools()))


def _transport(cfg: MCPServerConfig) -> Transport | None:
    transport = resolve_transport(cfg)
    if transport == "stdio" and cfg.command:
        return "stdio"
    if transport == "sse" and cfg.url:
        return "sse"
    if transport == "streamableHttp" and cfg.url:
        return "http"
    return None


def _missing_note(item: MissingServer) -> str:
    reasons = {
        "not_configured": "is not configured",
        "disabled": "is disabled",
        "invalid_transport": "has no valid transport",
        "not_connected": "is not connected on the host",
        "auth_required": "is waiting for host authorization",
        "no_tools": "has no tools to share with an in-process agent",
        "command_not_found": "needs raven on the ACP adapter PATH to reach the host bridge",
    }
    return f"MCP server {item.name!r} was not delivered because it {reasons[item.reason]}"


def _withheld_note(item: WithheldServer) -> str:
    reasons = {
        "oauth_interaction": "OAuth needs user interaction that a background sub-agent cannot perform",
        "secret_export_denied": "exporting its environment values or headers requires allowMcpSecrets=true on this sub-agent",
    }
    return f"MCP server {item.name!r} was withheld because {reasons[item.reason]}"


__all__ = [
    "LiveMcpSource",
    "McpDispatchError",
    "McpGrant",
    "McpGrantTarget",
    "McpServerView",
    "McpSource",
    "annotate_mcp_failure",
    "acp_target",
    "raven_cli_target",
    "raven_loop_target",
    "resolve_grant",
]
