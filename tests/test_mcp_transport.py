"""Authentication, isolation and teardown of host-local MCP endpoints."""

import asyncio
from urllib.parse import urlsplit

import pytest

from raven.config.schema import MCPServerConfig
from raven.mcp import endpoint, transport
from raven.mcp.endpoint import McpEndpoints
from raven.mcp.transport import open_endpoint


@pytest.fixture
def loopback(monkeypatch):
    monkeypatch.setattr(endpoint, "unix_sockets_available", lambda: False)


@pytest.mark.parametrize(
    "presented",
    [b"x" * 64 + b"\n", b'{"jsonrpc":"2.0"}\n', b"", b"x" * 65536],
    ids=["wrong-token", "mcp-before-auth", "silent", "oversized"],
)
async def test_untrusted_connections_never_open_an_upstream(loopback, monkeypatch, presented):
    monkeypatch.setattr(transport, "AUTH_TIMEOUT_S", 0.05)
    calls = []

    async def executor():
        calls.append(True)

    endpoints = McpEndpoints(executor_provider=executor)
    address = await endpoints.open("node", "private", MCPServerConfig(command="must-not-run"))
    parts = urlsplit(str(address))
    reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
    try:
        writer.write(presented)
        await writer.drain()
        try:
            assert await asyncio.wait_for(reader.read(), 2) == b""
        except ConnectionResetError:
            pass
        assert calls == []
    finally:
        writer.close()
        await endpoints.aclose()


async def test_each_endpoint_has_its_own_token_and_reaps_unauthenticated_connections(loopback):
    endpoints = McpEndpoints()
    cfg = MCPServerConfig(command="must-not-run")
    first = str(await endpoints.open("one", "private", cfg))
    second = str(await endpoints.open("two", "private", cfg))
    one, two = urlsplit(first), urlsplit(second)
    assert one.hostname == two.hostname == "127.0.0.1"
    assert one.path != two.path
    reader, writer = await asyncio.open_connection(one.hostname, one.port)
    try:
        with pytest.raises(OSError, match="authentication failed"):
            await open_endpoint(first.rsplit("/", 1)[0] + two.path)
        await asyncio.wait_for(endpoints.close("one"), 2)
        assert await asyncio.wait_for(reader.read(), 2) == b""
        with pytest.raises(OSError):
            await open_endpoint(first)
        assert endpoints._by_node["two"][0].listener.is_serving()
    finally:
        writer.close()
        await endpoints.aclose()


@pytest.mark.parametrize(
    "address",
    [
        "tcp://example.com:1234/" + "a" * 64,
        "tcp://127.0.0.2:1234/" + "a" * 64,
        "tcp://127.0.0.1:0/" + "a" * 64,
        "tcp://127.0.0.1:bad/" + "a" * 64,
        "tcp://127.0.0.1:1234/short",
        "tcp://127.0.0.1:1234/" + "a" * 64 + "?token=other",
    ],
)
async def test_malformed_endpoints_are_rejected_before_connecting(address, monkeypatch):
    async def connected(*args, **kwargs):
        pytest.fail("an invalid address reached the network")

    monkeypatch.setattr(asyncio, "open_connection", connected)
    with pytest.raises(OSError, match="invalid loopback MCP endpoint"):
        await open_endpoint(address)


@pytest.mark.parametrize("ending", ["cancel", "timeout", "bad-ack"])
async def test_an_interrupted_handshake_closes_the_clients_connection(monkeypatch, ending):
    received = asyncio.Event()
    closed = asyncio.get_running_loop().create_future()

    async def silent(reader, writer):
        try:
            await reader.readexactly(transport.TOKEN_BYTES * 2 + 1)
            received.set()
            if ending == "bad-ack":
                writer.write(b"no\n")
                await writer.drain()
            closed.set_result(await reader.read())
        finally:
            writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    address = f"tcp://127.0.0.1:{server.sockets[0].getsockname()[1]}/" + "a" * 64
    if ending == "timeout":
        monkeypatch.setattr(transport, "AUTH_TIMEOUT_S", 0.05)
    opening = asyncio.create_task(open_endpoint(address))
    try:
        await asyncio.wait_for(received.wait(), 2)
        if ending == "cancel":
            opening.cancel()
        with pytest.raises(asyncio.CancelledError if ending == "cancel" else OSError):
            await asyncio.wait_for(opening, 2)
        assert await asyncio.wait_for(closed, 2) == b""
    finally:
        opening.cancel()
        await asyncio.gather(opening, return_exceptions=True)
        server.close()
        await server.wait_closed()
