"""MCP client: connects to MCP servers and wraps their tools as native Raven tools."""

import asyncio
from collections.abc import Container
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from raven.agent.tools import media
from raven.agent.tools.registry import ToolRegistry
from raven.contracts.tool import Tool, ToolResult
from raven.mcp.naming import MCPToolRef, tool_name
from raven.sandbox import SandboxInitError

if TYPE_CHECKING:
    from raven.sandbox import SandboxExecutor


class MCPToolWrapper(Tool):
    """Wraps a single MCP server tool as an Raven Tool."""

    def __init__(
        self,
        session,
        server_name: str,
        tool_def,
        tool_timeout: int = 30,
        taken: Container[str] = frozenset(),
    ):
        self._session = session
        self._server_name = server_name
        self._original_name = tool_def.name
        self._name = tool_name(server_name, tool_def.name, taken=taken)
        self._description = tool_def.description or tool_def.name
        self._parameters = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._tool_timeout = tool_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def ref(self) -> MCPToolRef:
        """This tool's origin record, for the registry to file at registration.

        Deliberately not a lookup table anyone queries: the wrapper holds the
        pair because it needs it to place the call, and the one queryable copy
        lives in the registry. A public accessor that others could read back is
        how the second source grew last time.
        """
        return MCPToolRef(name=self._name, server=self._server_name, tool=self._original_name)

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self._original_name, arguments=kwargs),
                timeout=self._tool_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("MCP tool '{}' timed out after {}s", self._name, self._tool_timeout)
            return ToolResult(model_text=f"(MCP tool call timed out after {self._tool_timeout}s)", ok=False)
        except asyncio.CancelledError:
            # MCP SDK's anyio cancel scopes can leak CancelledError on timeout/failure.
            # Re-raise only if our task was externally cancelled (e.g. /stop).
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            logger.warning("MCP tool '{}' was cancelled by server/SDK", self._name)
            return ToolResult(model_text="(MCP tool call was cancelled)", ok=False)
        except Exception as exc:
            logger.exception(
                "MCP tool '{}' failed: {}: {}",
                self._name,
                type(exc).__name__,
                exc,
            )
            return ToolResult(model_text=f"(MCP tool call failed: {type(exc).__name__})", ok=False)

        # str(block) on a pydantic model yields its repr, so an ImageContent used
        # to put its entire base64 payload into the prompt as prose -- the model
        # saw gibberish instead of a picture and nothing errored. Convert per
        # content type instead, and never stringify a payload-bearing block.
        ok = not bool(getattr(result, "isError", False))
        text, blocks = media.blocks_from_mcp_content(result.content)
        if blocks:
            return ToolResult(model_text=text or "(no output)", blocks=blocks, ok=ok)
        if ok:
            return text or "(no output)"
        # The kind of failure this describes (isError) is not an exception: its
        # text looks like any other output, so the verdict must ride on the
        # result rather than be derived from a prefix.
        return ToolResult(model_text=text or "(no output)", ok=False)


@dataclass(frozen=True)
class Connected:
    """What one successful handshake leaves behind for the manager to commit.

    The session and the capabilities are returned rather than stored here
    because only the manager can decide whether this attempt still owns the
    record: two attempts on one server hand back two sessions, and the loser's
    transport is about to be closed. Committing them together with the tool
    names, under the same epoch check, is what keeps a caller from ever being
    handed a session whose stack is gone.
    """

    names: list[str]
    """Registered tool names, in the order they were registered."""
    session: Any
    """The live ``mcp.ClientSession``."""
    capabilities: Any
    """``ServerCapabilities`` from the handshake -- which of tools / resources /
    prompts this server actually offers. Read for gating; a server that offers
    only tools must not make the resource meta-tools appear."""


def resolve_transport(cfg) -> str | None:
    """Resolve a server config to its transport type; ``None`` when the
    config names neither a command nor a url."""
    if cfg.type:
        return cfg.type
    if cfg.command:
        return "stdio"
    if cfg.url:
        return "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
    return None


class MCPConfigError(ValueError):
    """A server config that cannot be connected (missing/unknown transport)."""


async def _report_redirect(response: httpx.Response) -> None:
    """Say plainly why a redirecting MCP endpoint does not connect.

    The HTTP clients built below do not follow redirects, and that is the
    security property: ``cfg.headers`` is where the plugin market renders the
    user's secret for a server, and httpx applies client-level headers to
    every hop of a redirect chain -- it scrubs ``Authorization`` when the
    origin changes but carries custom headers across, so a server answering
    302 could have read a secret meant only for itself. Refusing the hop
    removes the possibility rather than trusting the scrubbing.

    Following redirects by hand with a per-hop check (the shape in
    rpc/methods/skillhub.py) would also work, but an MCP endpoint is a
    configured address: the right answer to a redirect is to configure the
    address it points at, which this message asks for.
    """
    if 300 <= response.status_code < 400:
        logger.warning(
            "MCP endpoint {} answered {} redirecting to {!r}; not followed "
            "(configured headers must not reach another origin). Configure the "
            "final URL for this server instead.",
            response.request.url,
            response.status_code,
            response.headers.get("location", ""),
        )


@asynccontextmanager
async def open_mcp_transport(
    cfg,
    transport_type: str,
    executor: "SandboxExecutor | None" = None,
    http_auth: httpx.Auth | None = None,
):
    """One server's transport, without a session on top of it.

    Split out from :func:`_mcp_server_connection` because a proxied sub-agent
    has to run ``initialize`` itself: a session built here would make raven the
    peer the server negotiates capabilities with, and every server-initiated
    request would arrive at a client that never declared it could answer.
    """
    async with AsyncExitStack() as stack:
        from mcp import StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        if transport_type == "stdio":
            if executor is not None and executor.supports_process_spawning:
                read, write = await executor.start_process(cfg.command, cfg.args, env=cfg.env or None)
            else:
                params = StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env or None)
                read, write = await stack.enter_async_context(stdio_client(params))
        elif transport_type == "sse":

            def httpx_client_factory(
                headers: dict[str, str] | None = None,
                timeout: httpx.Timeout | None = None,
                auth: httpx.Auth | None = None,
            ) -> httpx.AsyncClient:
                merged_headers = {**(cfg.headers or {}), **(headers or {})}
                return httpx.AsyncClient(
                    headers=merged_headers or None,
                    follow_redirects=False,
                    event_hooks={"response": [_report_redirect]},
                    timeout=timeout,
                    auth=http_auth or auth,
                )

            read, write = await stack.enter_async_context(
                sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
            )
        elif transport_type == "streamableHttp":
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=cfg.headers or None,
                    follow_redirects=False,
                    event_hooks={"response": [_report_redirect]},
                    timeout=None,
                    auth=http_auth,
                )
            )
            read, write, _ = await stack.enter_async_context(streamable_http_client(cfg.url, http_client=http_client))
        else:
            raise KeyError(transport_type)
        yield read, write


@asynccontextmanager
async def _mcp_server_connection(
    cfg,
    transport_type: str,
    executor: "SandboxExecutor | None",
    http_auth: httpx.Auth | None = None,
):
    """One server's transport, session and handshake as a single lifecycle.

    The whole setup owns its own stack so an SDK task-group failure unwinds
    inside this generator and reaches the caller as an ordinary exception. Enter
    the transports into the caller's stack instead and the same failure surfaces
    only when that outer stack unwinds -- past every per-server ``except`` -- and
    cancels the turn that was connecting.
    """
    async with AsyncExitStack() as stack:
        from mcp import ClientSession

        read, write = await stack.enter_async_context(
            open_mcp_transport(cfg, transport_type, executor, http_auth=http_auth)
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        # The handshake result is the only place a server states which primitives
        # it offers, and it is stated once -- there is no way to ask again later.
        handshake = await session.initialize()
        tools = await session.list_tools()
        yield session, handshake, tools


async def connect_mcp_server(
    name: str,
    cfg,
    registry: ToolRegistry,
    stack: AsyncExitStack,
    executor: "SandboxExecutor | None" = None,
    http_auth: httpx.Auth | None = None,
) -> Connected:
    """Connect one MCP server, register its tools, and hand back what it offers.

    Failures propagate to the caller: :class:`MCPConfigError` for unusable
    configs, :class:`SandboxInitError` for the stdio-in-sandbox guard, and
    whatever the transport/handshake raises otherwise. The caller owns
    ``stack`` and must close it when this raises.

    ``http_auth`` is attached to the HTTP transports' client (SSE /
    streamableHttp) — this is where an OAuth provider plugs in; stdio
    servers authenticate through ``cfg.env`` instead.
    """
    transport_type = resolve_transport(cfg)
    if transport_type is None:
        raise MCPConfigError("no command or url configured")

    # Sandbox guard: fail hard so the agent never starts with a silently broken
    # MCP server.
    if (
        transport_type == "stdio"
        and executor is not None
        and executor.is_sandboxed
        and not executor.supports_process_spawning
    ):
        raise SandboxInitError(
            f"MCP server '{name}' uses stdio transport, but the active sandbox "
            f"({type(executor).__name__}) does not yet support process spawning. "
            "Either switch to an HTTP/SSE MCP server or set sandbox.backend='none'."
        )

    if transport_type not in {"stdio", "sse", "streamableHttp"}:
        raise MCPConfigError(f"unknown transport type '{transport_type}'")

    session, handshake, tools = await stack.enter_async_context(
        _mcp_server_connection(cfg, transport_type, executor, http_auth=http_auth)
    )
    registered: list[str] = []
    # The live registry, so a name registered a moment ago inside this same
    # loop counts as taken: two tools of one server can collide with each other
    # -- ``a.b`` and ``a/b`` both clean to ``a_b``.
    for tool_def in tools.tools:
        wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout, taken=registry)
        registry.register(wrapper, origin=wrapper.ref)
        registered.append(wrapper.name)
        logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)
    return Connected(names=registered, session=session, capabilities=handshake.capabilities)


async def connect_mcp_servers(
    mcp_servers: dict,
    registry: ToolRegistry,
    stack: AsyncExitStack,
    executor: "SandboxExecutor | None" = None,
) -> None:
    """Connect to configured MCP servers and register their tools.

    One-shot connect-all over a shared stack, and no longer the live path: the
    agent loop connects through
    :class:`~raven.mcp.manager.MCPConnectionManager`, which owns one
    stack per server so servers can be attached and detached while raven runs.

    This helper stays for callers that want one shot over one stack -- scripts
    and tests. Note it does **not** honour ``enabled``, so it is not a drop-in
    for the manager.
    """
    for name, cfg in mcp_servers.items():
        try:
            result = await connect_mcp_server(name, cfg, registry, stack, executor=executor)
            logger.info("MCP server '{}': connected, {} tools registered", name, len(result.names))
        except SandboxInitError:
            # Propagates so the caller surfaces it as a startup error rather
            # than running with a silently broken MCP server.
            raise
        except MCPConfigError as e:
            logger.warning("MCP server '{}': {}, skipping", name, e)
        except (Exception, BaseExceptionGroup) as e:
            # BaseExceptionGroup is raised by anyio task groups (e.g. streamableHttp cancel
            # scope failures) and is not a subclass of Exception in Python 3.11+.
            logger.error("MCP server '{}': failed to connect: {}", name, e)
