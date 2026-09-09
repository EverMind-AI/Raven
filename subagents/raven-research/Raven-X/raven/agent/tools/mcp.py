"""MCP client: connects to MCP servers and wraps their tools as native Raven tools."""

import asyncio
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from raven.agent.tools.base import Tool
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

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

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

        parts = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                parts.append(str(block))
        return "\n".join(parts) or "(no output)"


async def connect_mcp_servers(
    mcp_servers: dict,
    registry: ToolRegistry,
    stack: AsyncExitStack,
    executor: "SandboxExecutor | None" = None,
) -> None:
    """Connect to configured MCP servers and register their tools."""
    for name, cfg in mcp_servers.items():
        # Resolve transport type BEFORE the try/except so the sandbox guard below
        # can raise without being swallowed by the per-server error handler.
        transport_type = cfg.type
        if not transport_type:
            if cfg.command:
                transport_type = "stdio"
            elif cfg.url:
                transport_type = "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
            else:
                logger.warning("MCP server '{}': no command or url configured, skipping", name)
                continue

        # Sandbox guard: fail hard so the agent never starts with a silently broken
        # MCP server. Runs outside try/except — SandboxInitError propagates to
        # _connect_mcp() which surfaces it as a startup error.
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

        try:
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
                        auth=auth,
                    )

                read, write = await stack.enter_async_context(
                    sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                )
            elif transport_type == "streamableHttp":
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                logger.warning("MCP server '{}': unknown transport type '{}'", name, transport_type)
                continue

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            tools = await session.list_tools()
            for tool_def in tools.tools:
                wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout)
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)

            logger.info("MCP server '{}': connected, {} tools registered", name, len(tools.tools))
        except (Exception, BaseExceptionGroup) as e:
            # BaseExceptionGroup is raised by anyio task groups (e.g. streamableHttp cancel
            # scope failures) and is not a subclass of Exception in Python 3.11+.
            logger.error("MCP server '{}': failed to connect: {}", name, e)
