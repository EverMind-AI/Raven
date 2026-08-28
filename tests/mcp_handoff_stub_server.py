"""Deterministic stdio MCP server for sub-agent handoff integration tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

server = FastMCP("raven-mcp-handoff-stub", log_level="ERROR")


@server.tool()
def handoff_probe(marker: str) -> str:
    """Record and echo a marker received through a real MCP tool call."""
    ledger = Path(os.environ["MCP_HANDOFF_LEDGER"])
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"tool": "handoff_probe", "marker": marker}) + "\n")
    return f"MCP_HANDOFF_OK:{marker}"


if __name__ == "__main__":
    server.run(transport="stdio")
