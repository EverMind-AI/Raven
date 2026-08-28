"""open_mcp_transport hands back streams without performing a handshake."""

import sys

import pytest

from raven.config.schema import MCPServerConfig
from raven.mcp.client import open_mcp_transport


@pytest.mark.asyncio
async def test_stdio_transport_yields_streams_without_initialize():
    # A server that would fail an initialize but is fine to merely spawn: the
    # point is that this layer does not talk to it.
    cfg = MCPServerConfig(command=sys.executable, args=["-c", "import sys; sys.stdin.read()"])
    async with open_mcp_transport(cfg, "stdio") as (read, write):
        assert read is not None
        assert write is not None


@pytest.mark.asyncio
async def test_unknown_transport_is_the_callers_problem():
    cfg = MCPServerConfig(command="/bin/true")
    with pytest.raises(KeyError):
        async with open_mcp_transport(cfg, "carrier-pigeon"):
            pass
