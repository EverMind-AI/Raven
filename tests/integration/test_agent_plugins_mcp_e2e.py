"""A package's mcp.json reaches the agent's tool registry.

The claim under test is the whole point of the MCP phase: a portable Agent
Plugin package can ship a local MCP server, Raven launches it with the
package root as its working directory and with the two plugin variables in
its environment, and the server's tools become callable.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from raven.agent_plugins.mcp import PACKAGE_SERVER_SEPARATOR, package_mcp_layer

MANIFEST_SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
PACKAGE_NAME = "demo-mcp"
SERVER_NAME = "demo"

FIXTURE_SERVER = '''
import os
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def where_am_i() -> str:
    """Report the working directory, the interpreter and the plugin variables."""
    return (
        f"cwd={os.getcwd()}"
        f"|executable={sys.executable}"
        f"|root={os.environ.get('PLUGIN_ROOT')}"
        f"|data={os.environ.get('PLUGIN_DATA')}"
    )


if __name__ == "__main__":
    mcp.run()
'''


def _write_package(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps({"$schema": MANIFEST_SCHEMA_URL, "name": PACKAGE_NAME, "version": "1.0.0"}),
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_URL,
                "mcpServers": {
                    SERVER_NAME: {
                        "type": "stdio",
                        "command": "python",
                        "args": ["${PLUGIN_ROOT}/server.py"],
                        "cwd": "${PLUGIN_ROOT}",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "server.py").write_text(FIXTURE_SERVER, encoding="utf-8")
    return root


@pytest.fixture
def venv_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the bare ``python`` the format asks for resolve to this interpreter.

    The command has to be one executable token, so the test cannot spell an
    absolute interpreter path in the package; it puts the running
    interpreter's directory first on ``PATH`` instead.
    """
    scripts = Path(sys.executable).parent
    monkeypatch.setenv("PATH", str(scripts) + os.pathsep + os.environ.get("PATH", ""))


def test_a_packages_mcp_server_registers_its_tools(tmp_path: Path, venv_on_path: None) -> None:
    package_root = _write_package(tmp_path / PACKAGE_NAME)
    data_root = tmp_path / "plugin-data"
    package_data = data_root / PACKAGE_NAME

    servers, notes = package_mcp_layer([package_root], allowed=[PACKAGE_NAME], plugin_data_root=data_root)

    assert notes == []
    assert list(servers) == [f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}{SERVER_NAME}"]
    assert package_data.is_dir()

    from raven.agent.tools.registry import ToolRegistry
    from raven.mcp.manager import MCPConnectionManager

    async def run() -> tuple[list[str], str]:
        registry = ToolRegistry()
        manager = MCPConnectionManager(registry)
        try:
            await manager.apply_config(servers)
            states = {record["name"]: record["state"] for record in manager.status()}
            assert states == {f"{PACKAGE_NAME}{PACKAGE_SERVER_SEPARATOR}{SERVER_NAME}": "connected"}
            names = [name for name in registry.names() if name.startswith("mcp_")]
            assert len(names) == 1, names
            result = await registry.execute(names[0], {})
            return names, str(result)
        finally:
            await manager.aclose()

    names, result = asyncio.run(run())

    assert names[0].startswith("mcp_")
    fields = dict(part.split("=", 1) for part in result.split("|"))
    # The child reports the path it was launched by; this interpreter may be
    # reached through a link, so both sides are resolved before comparing.
    assert Path(fields["cwd"]).resolve() == package_root.resolve()
    assert Path(fields["root"]).resolve() == package_root.resolve()
    assert Path(fields["data"]).resolve() == package_data.resolve()
    assert Path(fields["executable"]).resolve() == Path(sys.executable).resolve()


def test_a_package_outside_the_opt_in_starts_nothing(tmp_path: Path, venv_on_path: None) -> None:
    package_root = _write_package(tmp_path / PACKAGE_NAME)
    data_root = tmp_path / "plugin-data"

    servers, notes = package_mcp_layer([package_root], allowed=[], plugin_data_root=data_root)

    assert servers == {}
    assert notes == []
    assert not (data_root / PACKAGE_NAME).exists()
