"""MCP client: connects to MCP servers and wraps their tools as native Raven tools."""

import asyncio
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from raven.agent.tools import media
from raven.agent.tools.base import Tool, ToolResult
from raven.agent.tools.registry import ToolRegistry
from raven.sandbox import SandboxInitError

if TYPE_CHECKING:
    from raven.sandbox import SandboxExecutor


class MCPToolWrapper(Tool):
    """Wraps a single MCP server tool as an Raven Tool."""

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30):
        self._session = session
        self._original_name = tool_def.name
        self._name = f"mcp_{server_name}_{tool_def.name}"
        self._description = tool_def.description or tool_def.name
        self._parameters = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._tool_timeout = tool_timeout

    @property
    def name(self) -> str:
        return self._name

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
            return f"(MCP tool call timed out after {self._tool_timeout}s)"
        except asyncio.CancelledError:
            # MCP SDK's anyio cancel scopes can leak CancelledError on timeout/failure.
            # Re-raise only if our task was externally cancelled (e.g. /stop).
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            logger.warning("MCP tool '{}' was cancelled by server/SDK", self._name)
            return "(MCP tool call was cancelled)"
        except Exception as exc:
            logger.exception(
                "MCP tool '{}' failed: {}: {}",
                self._name,
                type(exc).__name__,
                exc,
            )
            return f"(MCP tool call failed: {type(exc).__name__})"

        # str(block) on a pydantic model yields its repr, so an ImageContent used
        # to put its entire base64 payload into the prompt as prose -- the model
        # saw gibberish instead of a picture and nothing errored. Convert per
        # content type instead, and never stringify a payload-bearing block.
        text, blocks = media.blocks_from_mcp_content(result.content)
        if blocks:
            return ToolResult(model_text=text or "(no output)", blocks=blocks)
        return text or "(no output)"


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


async def connect_mcp_server(
    name: str,
    cfg,
    registry: ToolRegistry,
    stack: AsyncExitStack,
    executor: "SandboxExecutor | None" = None,
    http_auth: httpx.Auth | None = None,
) -> list[str]:
    """Connect one MCP server, register its tools, return their names.

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

    from mcp import ClientSession, StdioServerParameters
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
                follow_redirects=True,
                timeout=timeout,
                auth=http_auth or auth,
            )

        read, write = await stack.enter_async_context(sse_client(cfg.url, httpx_client_factory=httpx_client_factory))
    elif transport_type == "streamableHttp":
        http_client = await stack.enter_async_context(
            httpx.AsyncClient(
                headers=cfg.headers or None,
                follow_redirects=True,
                timeout=None,
                auth=http_auth,
            )
        )
        read, write, _ = await stack.enter_async_context(streamable_http_client(cfg.url, http_client=http_client))
    else:
        raise MCPConfigError(f"unknown transport type '{transport_type}'")

    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()

    tools = await session.list_tools()
    registered: list[str] = []
    for tool_def in tools.tools:
        wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout)
        registry.register(wrapper)
        registered.append(wrapper.name)
        logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)
    return registered


async def connect_mcp_servers(
    mcp_servers: dict,
    registry: ToolRegistry,
    stack: AsyncExitStack,
    executor: "SandboxExecutor | None" = None,
) -> None:
    """Connect to configured MCP servers and register their tools.

    One-shot connect-all over a shared stack, and no longer the live path: the
    agent loop connects through
    :class:`~raven.agent.tools.mcp_manager.MCPConnectionManager`, which owns one
    stack per server so servers can be attached and detached while raven runs.

    This helper stays for callers that want one shot over one stack -- scripts
    and tests. Note it does **not** honour ``enabled``, so it is not a drop-in
    for the manager.
    """
    for name, cfg in mcp_servers.items():
        try:
            registered = await connect_mcp_server(name, cfg, registry, stack, executor=executor)
            logger.info("MCP server '{}': connected, {} tools registered", name, len(registered))
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
