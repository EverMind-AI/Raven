"""``agent_plugins.*`` handlers -- Agent Plugin packages on the settings page.

The page lists the packages this host can see and allows or withholds each
package's MCP servers. Importing and removing a package's skills stays with
``raven agent-plugins``: both name a path on the host's own disk, and the
browser showing the page is not necessarily on that machine.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods.console import _mcp_live_states, _safe_loop

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


def _live_servers(loop: Any) -> tuple[frozenset[str], dict[str, dict]]:
    """The package servers the running loop was built with, and their states.

    The manager starts on the first turn, so a loop may run package servers
    the manager has no record of yet.
    """
    if loop is None:
        return frozenset(), {}
    live = frozenset(getattr(loop, "package_mcp_server_names", None) or ())
    try:
        states = _mcp_live_states(loop)
    except Exception:
        logger.exception("agent_plugins.list: the MCP manager status could not be read")
        states = {}
    return live, states


def _server_row(name: str, live: frozenset[str], states: dict[str, dict]) -> dict:
    if name not in live:
        return {"name": name, "state": "off", "error": None}
    snap = states.get(name)
    if snap is None:
        return {"name": name, "state": "disconnected", "error": None}
    return {"name": name, "state": str(snap.get("state") or "disconnected"), "error": snap.get("error")}


def _collect(live: frozenset[str], states: dict[str, dict]) -> dict:
    from raven.agent_plugins import PackageLedger, discover_packages, discover_skills
    from raven.agent_plugins.mcp import host_mcp_server_names, package_mcp_report
    from raven.config.loader import load_config
    from raven.config.paths import get_agent_plugins_ledger_path, package_data_root_path
    from raven.config.raven import load_raven_config

    block = load_raven_config().agent_plugins
    config = load_config()
    packages, problems = discover_packages(block.packages, disabled=block.disabled)
    workspace = Path(config.workspace_path)
    ledger = PackageLedger.load(get_agent_plugins_ledger_path())
    allowed = set(block.mcp_packages)
    host_servers = host_mcp_server_names(config.tools.mcp_servers)
    data_root = package_data_root_path()

    rows: list[dict] = []
    for package in packages:
        skills, skill_problems = discover_skills(package)
        report = package_mcp_report(package, plugin_data=data_root / package.name, reserved_names=host_servers)
        mcp_problems = [report.disabled] if report.disabled else []
        mcp_problems += [f"{item.server}: {item.reason}" for item in report.problems]
        rows.append(
            {
                "name": package.name,
                "version": package.version,
                "description": package.manifest.description or "",
                "source": str(package.root),
                "notes": list(package.notes),
                "skills": [skill.name for skill in skills],
                "skills_imported": ledger.install_state(package.name, workspace=workspace) == "verified",
                "problems": [f"{item.name}: {item.reason}" for item in skill_problems],
                "mcp": {
                    "servers": [_server_row(name, live, states) for name in report.servers],
                    "allowed": package.name in allowed,
                    "live": any(name in live for name in report.servers),
                    "problems": mcp_problems,
                },
            }
        )
    return {
        "packages": rows,
        "problems": [{"path": str(item.path), "reason": item.reason} for item in problems],
    }


async def agent_plugins_list(params: dict, *, agent_loop_factory: Any = None) -> dict:
    live, states = _live_servers(_safe_loop(agent_loop_factory))
    return await asyncio.to_thread(_collect, live, states)


def _visible(name: str) -> bool:
    from raven.agent_plugins import discover_packages
    from raven.config.raven import load_raven_config

    block = load_raven_config().agent_plugins
    packages, _ = discover_packages(block.packages, disabled=block.disabled)
    return any(package.name == name for package in packages)


async def agent_plugins_set_mcp(params: dict, *, agent_loop_factory: Any = None) -> dict:
    from raven.config.update import set_agent_plugin_mcp

    name = params.get("name")
    enabled = params.get("enabled")
    if not isinstance(name, str) or not name:
        raise ConfigValidationError("name must be a non-empty string", data={"field": "name"})
    if not isinstance(enabled, bool):
        raise ConfigValidationError("enabled must be a boolean", data={"field": "enabled"})
    # Only allowing is checked: a package that has moved away must still be
    # switchable off, or its name would sit in the list with no way to clear it.
    if enabled and not await asyncio.to_thread(_visible, name):
        raise ConfigValidationError(f"no Agent Plugin package named '{name}'", data={"field": "name", "name": name})
    try:
        changed = await asyncio.to_thread(set_agent_plugin_mcp, name, enabled)
    except (OSError, ValueError) as exc:
        raise ConfigValidationError(f"agentPlugins.mcpPackages cannot be written: {exc}") from exc
    return {"name": name, "enabled": enabled, "changed": changed}


def register_agent_plugins_methods(dispatcher: "Dispatcher", *, agent_loop_factory: Any = None) -> None:
    """Register the Agent Plugin package handlers on a dispatcher instance."""

    def bind(fn):
        async def _h(params: dict) -> dict:
            return await fn(params, agent_loop_factory=agent_loop_factory)

        return _h

    dispatcher.register("agent_plugins.list", bind(agent_plugins_list))
    dispatcher.register("agent_plugins.set_mcp", bind(agent_plugins_set_mcp))


__all__ = [
    "agent_plugins_list",
    "agent_plugins_set_mcp",
    "register_agent_plugins_methods",
]
