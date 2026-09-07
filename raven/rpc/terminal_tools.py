"""Bind host-agent terminal tools to the runtime's existing RPC handlers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import uuid4

from raven.agent import workdir
from raven.agent.tools.terminal import CreateTerminalTool, ResolveAgentTool, SendTerminalTool
from raven.contracts.terminal import TerminalError, worktree_id
from raven.rpc import connection


def provider_command(provider: str) -> tuple[str, list[str]]:
    from raven.config.loader import load_config

    rows = load_config().subagents.agents
    native = {"codex": ["codex"], "claude_code": ["claude"]}
    aliases = {"claude": "claude_code", "claude_code": "claude_code", "codex": "codex", "codex_cli": "codex"}
    provider_kind = aliases.get("_".join(provider.strip().lower().replace("-", " ").replace("_", " ").split()))
    matches = []
    for row in rows:
        values = row.model_dump() if hasattr(row, "model_dump") else row
        name, preset = values.get("name"), values.get("preset")
        brand = aliases.get(preset or name, preset or name)
        if provider == name or provider_kind is not None and provider_kind == brand:
            if brand in native:
                matches.append((name, native[brand]))
    if not matches:
        raise TerminalError(
            "no_matching_kind",
            "No matching agent kind. Open External Agents and add the Claude Code or Codex preset for this provider.",
        )
    if len(matches) > 1:
        raise TerminalError(
            "provider_not_unique",
            "Multiple matching agent kinds. Open External Agents and keep one matching preset, or use its exact name.",
        )
    return matches[0]


def task_worktree(task: str = "") -> str:
    path = task or str(workdir.current() or Path.cwd())
    if "::" in path:
        repo, _, directory = path.removeprefix("id:").partition("::")
        if not repo or not Path(directory).is_absolute() or not Path(directory).is_dir():
            raise TerminalError("invalid_worktree", "Task must name an existing worktree")
        return f"{repo}::{Path(directory).resolve()}"
    directory = Path(path)
    if not directory.is_absolute() or not directory.is_dir():
        raise TerminalError("invalid_worktree", "Task must be an absolute checkout path or worktree id")
    inherited = os.environ.get("RAVEN_WORKTREE_ID", "")
    if inherited.partition("::")[2] == str(directory.resolve()):
        return inherited
    try:
        return worktree_id(directory)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise TerminalError("invalid_worktree", "Cannot identify this task's Git worktree") from exc


def register_terminal_tools(tools, dispatcher):
    async def rpc(method, params):
        token = connection.bind_connection()
        try:
            connection.current_state()["terminal_owner"] = "raven"
            frame = await dispatcher.dispatch(
                {"jsonrpc": "2.0", "id": str(uuid4()), "method": method, "params": params}
            )
        finally:
            connection.unbind_connection(token)
        if "error" in frame:
            error = frame["error"]
            data = error.get("data") or {}
            code = data.get("code", error["message"])
            raise TerminalError(code, data.get("message") or data.get("detail") or error["message"], data)
        return frame["result"]

    scope = os.environ.get("RAVEN_ENVIRONMENT", "local") + "/development"
    installed = (
        CreateTerminalTool(rpc, provider_command, task_worktree),
        SendTerminalTool(rpc, scope=scope),
        ResolveAgentTool(rpc),
    )
    for tool in installed:
        tools.register(tool)

    def unregister():
        for tool in installed:
            if tools.get(tool.name) is tool:
                tools.unregister(tool.name)

    return unregister
