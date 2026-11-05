"""A real MCP client against a real streamable HTTP server.

``streamableHttp`` is the transport ``resolve_transport`` picks for every URL
that does not end in ``/sse``, and mcp 2.x's ``streamable_http_client`` yields
two streams. A wiring that unpacks three -- the 1.x contract, where the third
value was the session id -- fails before a session exists, so the server
registers no tools. The unit fakes state the SDK's shape but nothing else dials
a real server; this is the case that keeps that shape honest.

Marked ``integration`` because it starts a real server process; deselected by
default, run with ``-m integration``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import MCPServerConfig
from raven.mcp.client import connect_mcp_server
from raven.mcp.endpoint import McpEndpoints

pytestmark = pytest.mark.integration

_UPSTREAM = Path(__file__).parent / "_mcp_streamable_upstream.py"
_START_TIMEOUT_S = 30.0


def _start_upstream(tmp_path: Path, headers_file: Path | None = None) -> tuple[subprocess.Popen[bytes], int]:
    """The running server and the loopback port it bound.

    The port comes from the child rather than from a socket the parent binds
    first: a port reserved that way is free for anyone to take in the window
    between close and bind, and the failure then reads as a broken transport.
    """
    port_file = tmp_path / "port"
    args = [sys.executable, str(_UPSTREAM), str(port_file)]
    if headers_file is not None:
        args.append(str(headers_file))
    process = subprocess.Popen(args)
    deadline = time.monotonic() + _START_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the streamable HTTP upstream exited with {process.returncode}")
        try:
            return process, int(port_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.05)
    process.terminate()
    raise RuntimeError("the streamable HTTP upstream never wrote its port")


def _stop_upstream(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


async def test_a_streamable_http_server_registers_and_runs_its_tools(tmp_path):
    process, port = _start_upstream(tmp_path)
    cfg = MCPServerConfig(url=f"http://127.0.0.1:{port}/mcp")
    registry = ToolRegistry()
    stack = AsyncExitStack()
    try:
        connected = await asyncio.wait_for(connect_mcp_server("streamable", cfg, registry, stack), timeout=60)
        assert connected.names == ["mcp_streamable_ping"]
        assert await asyncio.wait_for(registry.execute("mcp_streamable_ping", {}), timeout=60) == "pong"
    finally:
        await stack.aclose()
        _stop_upstream(process)


async def test_a_bridged_streamable_server_receives_the_negotiated_protocol_version(tmp_path):
    headers_file = tmp_path / "headers.jsonl"
    process, port = _start_upstream(tmp_path, headers_file)
    cfg = MCPServerConfig(url=f"http://127.0.0.1:{port}/mcp")
    endpoints = McpEndpoints()
    try:
        address = await endpoints.open("protocol", "streamable", cfg)
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "raven", "mcp", "bridge", str(address)], env={"PATH": ""}
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await asyncio.wait_for(session.initialize(), timeout=60)
                result = await asyncio.wait_for(session.call_tool("ping", {}), timeout=60)
                assert not result.is_error
                assert result.content[0].text == "pong"
                requests = [json.loads(line) for line in headers_file.read_text(encoding="utf-8").splitlines()]
                posts = [request for request in requests if request["method"] == "POST"]
                assert len(posts) >= 3
                assert posts[0]["version"] == ""
                assert all(request["version"] == init.protocol_version for request in posts[1:]), posts
    finally:
        await endpoints.aclose()
        _stop_upstream(process)
