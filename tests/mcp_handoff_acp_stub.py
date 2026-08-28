"""ACP adapter stub that consumes handed-off MCP servers end to end."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import MCPServerConfig
from raven.mcp.client import connect_mcp_server

logger.disable("raven")

_SERVERS: list[dict[str, Any]] = []
_SESSION = "mcp-handoff-session"


def _send(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _ok(request_id: Any, result: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _record(method: str, servers: list[dict[str, Any]]) -> None:
    path = Path(os.environ["ACP_HANDOFF_RECEIPT"])
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"method": method, "mcpServers": servers}) + "\n")


def _config(server: dict[str, Any]) -> MCPServerConfig:
    transport = server.get("type")
    if transport is None and server.get("command"):
        env = {entry["name"]: entry["value"] for entry in server.get("env", [])}
        return MCPServerConfig(command=server["command"], args=server.get("args", []), env=env)
    headers = {entry["name"]: entry["value"] for entry in server.get("headers", [])}
    return MCPServerConfig(
        transport="streamableHttp" if transport == "http" else "sse",
        url=server["url"],
        headers=headers,
    )


async def _call_mcp(marker: str) -> str:
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        for server in _SERVERS:
            await connect_mcp_server(server["name"], _config(server), registry, stack)
        names = registry.names_from("probe")
        if len(names) != 1:
            raise RuntimeError(f"expected one probe tool, got {names!r}")
        return str(await registry.execute(names[0], {"marker": marker}))


def main() -> None:
    global _SERVERS
    for line in sys.stdin:
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        method = frame.get("method")
        request_id = frame.get("id")
        params = frame.get("params") or {}
        if method == "initialize":
            _ok(
                request_id,
                {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "mcp-handoff-acp-stub", "version": "1"},
                    "agentCapabilities": {
                        "loadSession": True,
                        "sessionCapabilities": {"resume": {}},
                        "mcpCapabilities": {"http": True, "sse": True},
                    },
                },
            )
        elif method in ("session/new", "session/load"):
            _SERVERS = list(params.get("mcpServers") or [])
            _record(method, _SERVERS)
            _ok(request_id, {"sessionId": _SESSION} if method == "session/new" else {})
        elif method == "session/prompt":
            prompt = params.get("prompt") or []
            marker = "".join(str(item.get("text") or "") for item in prompt if isinstance(item, dict))
            result = asyncio.run(_call_mcp(marker))
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": params.get("sessionId") or _SESSION,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": result},
                        },
                    },
                }
            )
            _ok(request_id, {"stopReason": "end_turn"})
        elif request_id is not None:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unknown method {method}"},
                }
            )


if __name__ == "__main__":
    main()
