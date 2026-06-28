"""Tests for the gateway's loopback /health endpoint handler.

The handler runs inside the gateway's own asyncio loop; here it is driven
directly via ``asyncio.start_server`` on an ephemeral port so the test needs
no full gateway stack. Async tests run under pytest-asyncio's auto mode.
"""

from __future__ import annotations

import asyncio

from raven.cli.gateway_commands import _health_handler


async def test_health_handler_returns_ok_json() -> None:
    server = await asyncio.start_server(_health_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        data = await reader.read(1024)
        writer.close()

    assert b"200 OK" in data
    assert b'{"status":"ok"}' in data
