"""Host-local MCP endpoint connections, with authenticated loopback on Windows."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from urllib.parse import urlsplit

AUTH_TIMEOUT_S = 5.0
TOKEN_BYTES = 32
_ACK = b"ok\n"


def unix_sockets_available() -> bool:
    """Whether this host can listen on and connect to Unix sockets."""
    return callable(getattr(asyncio, "start_unix_server", None)) and callable(
        getattr(asyncio, "open_unix_connection", None)
    )


def endpoint_label(address: str) -> str:
    """An endpoint's diagnostic address without its loopback credential."""
    return "/".join(address.split("/", 3)[:3]) if address.startswith("tcp://") else address


async def authenticate(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, token: str) -> bool:
    """Admit a loopback peer before any upstream is opened or frame forwarded."""
    try:
        async with asyncio.timeout(AUTH_TIMEOUT_S):
            presented = await reader.readexactly(len(token) + 1)
            if not secrets.compare_digest(presented, token.encode("ascii") + b"\n"):
                return False
            writer.write(_ACK)
            await writer.drain()
    except (OSError, TimeoutError, asyncio.IncompleteReadError):
        return False
    return True


async def open_endpoint(address: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to a host endpoint, authenticating before exposing its MCP stream."""
    if not address.startswith("tcp://"):
        if not unix_sockets_available():
            raise OSError("Unix-socket MCP endpoints are unavailable on this host")
        return await asyncio.open_unix_connection(address)

    try:
        parts = urlsplit(address)
        token = parts.path.removeprefix("/")
        port = parts.port
        if (
            parts.hostname != "127.0.0.1"
            or parts.netloc != f"127.0.0.1:{port}"
            or port is None
            or port == 0
            or parts.query
            or parts.fragment
            or len(token) != TOKEN_BYTES * 2
            or any(ch not in "0123456789abcdef" for ch in token)
        ):
            raise ValueError
    except ValueError:
        raise OSError("invalid loopback MCP endpoint") from None

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        async with asyncio.timeout(AUTH_TIMEOUT_S):
            writer.write(token.encode("ascii") + b"\n")
            await writer.drain()
            if await reader.readexactly(len(_ACK)) != _ACK:
                raise OSError("MCP endpoint authentication failed")
    except BaseException as exc:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        if isinstance(exc, (TimeoutError, asyncio.IncompleteReadError)):
            raise OSError("MCP endpoint authentication failed") from None
        raise
    return reader, writer
