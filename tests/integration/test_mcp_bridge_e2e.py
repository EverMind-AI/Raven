"""A sub-agent reaching a host-held server through the bridge.

Runs a real MCP client over a real ``raven mcp bridge`` process against a real
FastMCP server, so the properties under test are the ones a dispatch depends on:
the sub-agent handshakes with the true server identity, server-initiated
requests reach it, progress notifications arrive, a large result survives the
hop, and an upstream that dies mid-request closes the sub-agent's connection
rather than leaving it on its own timeout.

Marked ``integration`` because it spawns real processes; deselected by default,
run with ``-m integration``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CreateMessageResult, TextContent

from raven.config.schema import MCPServerConfig
from raven.mcp.endpoint import McpEndpoints

pytestmark = pytest.mark.integration

_UPSTREAM = Path(__file__).parent / "_mcp_bridge_upstream.py"

# What "immediately" means for the EOF an unreachable upstream owes the
# sub-agent. Measured on this tree: 0.03s when the endpoint closes downstream as
# soon as the upstream stream ends, against 4.0s when that close waits for
# transport teardown (the SDK's 2s wait on the process plus a 2s SIGTERM->SIGKILL
# escalation). 1.5s sits ~50x above the first and ~2.7x below the second, so it
# separates them without being a stopwatch on a loaded machine.
_EOF_DEADLINE_S = 1.5


async def _sampling_cb(context, params):
    return CreateMessageResult(
        role="assistant", content=TextContent(type="text", text="pong-from-subagent"), model="fake"
    )


def _bridge_params(socket_path: Path) -> StdioServerParameters:
    """How a sub-agent is told to reach the endpoint.

    ``PATH`` is emptied rather than inherited to keep the check honest: the
    bridge must work with nothing but the socket path, so anything it picked up
    from the host's environment would hide a dependency.
    """
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "raven", "mcp", "bridge", str(socket_path)],
        env={"PATH": ""},
    )


def _reap(pid_file: Path) -> None:
    """SIGKILL the upstream ``go_silent`` left behind.

    Nothing else reaches it: it ignores SIGTERM, closing its stdin does not
    faze it, and the client SDK's SIGTERM->SIGKILL escalation runs inside a
    cancelled scope on this path, so its awaits re-raise instead of running. The
    server's own timer would get there eventually; this keeps the process from
    outliving the test that made it.
    """
    try:
        pid = int(pid_file.read_text())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def test_a_bridged_subagent_sees_the_real_server_and_its_reverse_requests_arrive():
    cfg = MCPServerConfig(command=sys.executable, args=[str(_UPSTREAM)])
    endpoints = McpEndpoints()
    try:
        path = await endpoints.open("e2e", "upstream", cfg)
        async with stdio_client(_bridge_params(path)) as (read, write):
            async with ClientSession(read, write, sampling_callback=_sampling_cb) as session:
                init = await asyncio.wait_for(session.initialize(), timeout=60)
                # The identity the sub-agent handshakes with is the real
                # server's, not raven's: nothing on this path terminates the
                # session.
                assert init.serverInfo.name == "bridge-e2e-upstream"

                reverse = await asyncio.wait_for(session.call_tool("ask_client", {}), timeout=60)
                assert "pong-from-subagent" in reverse.content[0].text

                progress: list[tuple[float, float | None, str | None]] = []

                async def on_progress(value, total, message):
                    progress.append((value, total, message))

                stepped = await asyncio.wait_for(
                    session.call_tool("stepped", {"steps": 3}, progress_callback=on_progress), timeout=60
                )
                assert len(progress) == 3
                assert stepped.structuredContent is not None

                big = await asyncio.wait_for(session.call_tool("big", {"kb": 1024}), timeout=120)
                assert len(big.content[0].text) == 1024 * 1024
    finally:
        await endpoints.aclose()


async def test_an_upstream_that_dies_mid_request_closes_the_subagents_connection():
    cfg = MCPServerConfig(command=sys.executable, args=[str(_UPSTREAM)])
    endpoints = McpEndpoints()
    try:
        path = await endpoints.open("e2e-die", "upstream", cfg)
        async with stdio_client(_bridge_params(path)) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=60)
                # Direct to a stdio server this surfaces immediately; the bridge
                # must not turn it into the client's own timeout.
                with pytest.raises(Exception) as caught:
                    await asyncio.wait_for(session.call_tool("die", {}), timeout=20)
                assert not isinstance(caught.value, asyncio.TimeoutError)
    finally:
        await endpoints.aclose()


async def test_an_upstream_that_goes_unreachable_closes_the_subagent_promptly(tmp_path):
    """The endpoint owes downstream an EOF before it tears the upstream down.

    This is the case that pins it. The upstream stops answering but stays alive
    and refuses SIGTERM, so transport teardown takes seconds -- and a
    ``writer.close()`` that happens after teardown instead of when the upstream
    stream ends leaves the sub-agent waiting that long for a reply that is
    already known not to be coming. Deadline rather than exception type, because
    both placements eventually raise the same ``McpError``; only the timing
    tells them apart.
    """
    pid_file = tmp_path / "upstream.pid"
    cfg = MCPServerConfig(command=sys.executable, args=[str(_UPSTREAM), str(pid_file)])
    endpoints = McpEndpoints()
    try:
        path = await endpoints.open("e2e-silent", "upstream", cfg)
        async with stdio_client(_bridge_params(path)) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=60)
                with pytest.raises(Exception) as caught:
                    await asyncio.wait_for(session.call_tool("go_silent", {}), timeout=_EOF_DEADLINE_S)
                # A TimeoutError here is the failure this test exists to catch:
                # it means the deadline expired with the connection still open.
                assert not isinstance(caught.value, asyncio.TimeoutError)
    finally:
        await endpoints.aclose()
        _reap(pid_file)
