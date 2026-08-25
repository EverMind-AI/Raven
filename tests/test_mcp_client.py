"""MCP tool wrapper result verdicts (ok flag on failures)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from raven.agent.tools.base import ToolResult
from raven.mcp.client import MCPToolWrapper


class _Session:
    def __init__(self, outcome: object | Exception) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict) -> object:
        self.calls.append((name, arguments))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _wrapper(session: _Session) -> MCPToolWrapper:
    tool_def = SimpleNamespace(
        name="read",
        description="read something",
        inputSchema={"type": "object", "properties": {}},
    )
    return MCPToolWrapper(session, "server-x", tool_def, tool_timeout=30, taken=frozenset())


def _result(*, is_error: bool, text: str) -> SimpleNamespace:
    from mcp.types import TextContent

    return SimpleNamespace(isError=is_error, content=[TextContent(type="text", text=text)])


async def test_an_is_error_result_carries_ok_false() -> None:
    session = _Session(_result(is_error=True, text="permission denied"))

    result = await _wrapper(session).execute(arguments={})

    assert isinstance(result, ToolResult)
    assert result.model_text == "permission denied"
    assert result.ok is False, "isError is the producer's verdict, not a text prefix"
    assert session.calls == [("read", {"arguments": {}})]


async def test_a_clean_result_stays_a_bare_string() -> None:
    session = _Session(_result(is_error=False, text="all good"))

    result = await _wrapper(session).execute(arguments={})

    assert result == "all good"
    assert isinstance(result, str)


async def test_a_timeout_reports_failure() -> None:
    session = _Session(asyncio.TimeoutError())

    result = await _wrapper(session).execute(arguments={})

    assert isinstance(result, ToolResult) and result.ok is False


async def test_a_generic_failure_reports_failure() -> None:
    session = _Session(RuntimeError("boom"))

    result = await _wrapper(session).execute(arguments={})

    assert isinstance(result, ToolResult) and result.ok is False


class TestTransportFailureIsolation:
    """A failed transport must not unwind the turn that was connecting.

    The SDK opens streamableHttp inside an anyio task group. Entered into the
    caller's ``AsyncExitStack``, a transport that dies takes the caller down
    when that outer stack unwinds -- past every per-server ``except`` -- so one
    MCP server with an expired credential cost the user the whole answer.
    ``_mcp_server_connection`` owns the transport, session and handshake as one
    lifecycle, which is what turns that into this server's connection error.
    """

    async def test_a_failed_transport_does_not_cancel_the_following_server(self, monkeypatch) -> None:
        from contextlib import AsyncExitStack, asynccontextmanager

        import anyio
        import mcp
        import mcp.client.streamable_http

        from raven.agent.tools.registry import ToolRegistry
        from raven.mcp.client import connect_mcp_servers

        bad, good = "https://bad.example/mcp", "https://good.example/mcp"
        attempted: list[str] = []

        @asynccontextmanager
        async def fake_streamable_http_client(url, http_client):
            attempted.append(url)
            if url == bad:
                async with anyio.create_task_group() as group:

                    async def fail_transport():
                        await anyio.sleep(0)
                        raise RuntimeError("transport failed")

                    group.start_soon(fail_transport)
                    yield url, object(), None
            else:
                yield url, object(), None

        class FakeSession:
            def __init__(self, read, write) -> None:
                self.read = read

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def initialize(self):
                if self.read == bad:
                    await asyncio.Event().wait()
                return SimpleNamespace(capabilities=SimpleNamespace(tools=True))

            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name="ping", description="", inputSchema={})])

        monkeypatch.setattr(mcp, "ClientSession", FakeSession)
        monkeypatch.setattr(mcp.client.streamable_http, "streamable_http_client", fake_streamable_http_client)

        def config(url):
            return SimpleNamespace(
                type="streamableHttp", url=url, headers=None, env=None, command=None, args=None, tool_timeout=30
            )

        registry = ToolRegistry()
        async with AsyncExitStack() as stack:
            await connect_mcp_servers({"bad": config(bad), "good": config(good)}, registry, stack)

        assert attempted == [bad, good], "the good server was never reached"
        assert registry.names(), "the good server registered nothing, so containment proves too little"
        assert asyncio.current_task().cancelling() == 0, "the caller was cancelled by the failed server"
