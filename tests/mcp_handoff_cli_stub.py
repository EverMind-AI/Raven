"""Vendored-Raven-shaped CLI consumer for MCP handoff integration tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
from contextlib import AsyncExitStack
from pathlib import Path

from loguru import logger

from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import MCPServerConfig
from raven.mcp.client import connect_mcp_server

logger.disable("raven")


async def _run(mcp_file: Path, prompt_file: Path) -> str:
    fragment = json.loads(mcp_file.read_text(encoding="utf-8"))
    tools = fragment["tools"]
    configured = {name: MCPServerConfig.model_validate(payload) for name, payload in tools["mcpServers"].items()}
    receipt = {
        "mcp_file": str(mcp_file),
        "mode": stat.S_IMODE(mcp_file.stat().st_mode),
        "servers": sorted(configured),
        "disabledTools": tools.get("disabledTools", []),
    }
    Path(os.environ["MCP_HANDOFF_RECEIPT"]).write_text(json.dumps(receipt), encoding="utf-8")

    marker = prompt_file.read_text(encoding="utf-8").strip()
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        for name, config in configured.items():
            await connect_mcp_server(name, config, registry, stack)
        tool_names = registry.names_from("probe")
        if len(tool_names) != 1:
            raise RuntimeError(f"expected one probe tool, got {tool_names!r}")
        return str(await registry.execute(tool_names[0], {"marker": marker}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-file", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    args = parser.parse_args()
    print(asyncio.run(_run(args.mcp_file, args.prompt_file)), flush=True)


if __name__ == "__main__":
    main()
