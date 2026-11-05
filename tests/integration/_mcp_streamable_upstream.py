"""A real MCP server over streamable HTTP, for the transport's e2e test.

Serves the SDK's own ``MCPServer`` on a loopback port the OS picks, then writes
that port to argv[1] so the test can dial the URL raven would be configured
with. Kept in its own process: the point is a real HTTP server, not the test's
event loop.
"""

from __future__ import annotations

import asyncio
import json
import sys

import uvicorn
from mcp.server.mcpserver import MCPServer

server = MCPServer("streamable-upstream")


@server.tool()
def ping() -> str:
    return "pong"


async def main() -> None:
    app = server.streamable_http_app(host="127.0.0.1")

    async def recorded(scope, receive, send):
        if len(sys.argv) > 2 and scope["type"] == "http":
            headers = dict(scope["headers"])
            with open(sys.argv[2], "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"method": scope["method"], "version": headers.get(b"mcp-protocol-version", b"").decode()}
                    )
                    + "\n"
                )
        await app(scope, receive, send)

    config = uvicorn.Config(recorded, host="127.0.0.1", port=0, log_level="error")
    http = uvicorn.Server(config)
    serving = asyncio.create_task(http.serve())
    while not http.started:
        await asyncio.sleep(0.02)
    port = http.servers[0].sockets[0].getsockname()[1]
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        fh.write(str(port))
    await serving


if __name__ == "__main__":
    asyncio.run(main())
