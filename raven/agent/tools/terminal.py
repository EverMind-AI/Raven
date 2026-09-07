"""Host-agent tools for creating and addressing human-visible peer terminals."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from raven.contracts.terminal import Envelope, TerminalError
from raven.contracts.tool import Tool

RpcCall = Callable[[str, dict], Awaitable[dict]]


def _failure(exc: TerminalError) -> str:
    return json.dumps({"error": {"code": exc.code, "message": str(exc), "data": exc.data}})


class _TerminalTool(Tool):
    def __init__(self, rpc: RpcCall):
        self.rpc = rpc
        self._session_key = ContextVar("terminal_tool_session", default=None)

    def set_context(self, channel: str, chat_id: str, session_key: str | None = None):
        self._session_key.set(session_key or f"{channel}:{chat_id}")

    def session_params(self):
        session = self._session_key.get()
        return {"session_id": session} if session else {}


class CreateTerminalTool(_TerminalTool):
    name = "create_terminal"
    description = "Create a visible Claude Code or Codex terminal and register its canonical agent name in this task."
    parameters = {
        "type": "object",
        "properties": {
            "provider": {"type": "string", "description": "Configured Claude Code or Codex kind"},
            "name": {"type": "string", "description": "Unique lowercase canonical name with hyphen-separated words"},
            "task": {
                "type": "string",
                "description": "Optional worktree id or checkout path; must match the calling session's cwd",
            },
            "unattended": {
                "type": "boolean",
                "description": "Explicitly bypass provider permission prompts; defaults to false for interactive terminals",
                "default": False,
            },
        },
        "required": ["provider", "name"],
        "additionalProperties": False,
    }

    def __init__(self, rpc: RpcCall, provider_command: Callable, task_worktree: Callable):
        super().__init__(rpc)
        self.provider_command = provider_command
        self.task_worktree = task_worktree

    async def execute(self, provider: str, name: str, task: str = "", unattended: bool = False, **kwargs: Any) -> str:
        try:
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
                raise TerminalError("invalid_agent_name", "Use a lowercase canonical agent name")
            worktree = self.task_worktree(task, self._session_key.get())
            kind, command = self.provider_command(provider, unattended=unattended)
            existing = await self.rpc("agents.resolve", {"mention": name})
            if existing.get("candidates"):
                raise TerminalError("agent_name_exists", "The canonical name or alias already exists")
            created = await self.rpc(
                "terminal.create", {"worktree_id": worktree, "command": command, "title": name, **self.session_params()}
            )
            terminal = created["terminal"]
            try:
                await self.rpc(
                    "agents.register",
                    {"name": name, "kind": kind, "terminal": terminal["handle"], "task_ref": worktree},
                )
            except TerminalError as exc:
                try:
                    await self.rpc("terminal.close", {"handle": terminal["handle"]})
                except TerminalError as cleanup:
                    raise TerminalError(
                        exc.code, str(exc), {"handle": terminal["handle"], "cleanup_error": cleanup.code}
                    ) from exc
                raise
            return json.dumps(
                {"handle": terminal["handle"], "instance": name, "incarnation_id": terminal["incarnationId"]}
            )
        except TerminalError as exc:
            return _failure(exc)


class ResolveAgentTool(_TerminalTool):
    name = "resolve_agent"
    description = "Resolve a canonical agent name or exact alias, returning candidates and whether the match is unique."
    parameters = {
        "type": "object",
        "properties": {"mention": {"type": "string"}},
        "required": ["mention"],
        "additionalProperties": False,
    }

    def __init__(self, rpc: RpcCall):
        super().__init__(rpc)

    async def execute(self, mention: str, **kwargs: Any) -> str:
        try:
            return json.dumps(await self.rpc("agents.resolve", {"mention": mention}))
        except TerminalError as exc:
            return _failure(exc)


class SendTerminalTool(_TerminalTool):
    name = "send_terminal"
    description = (
        "Send a plain-text task or summary to a uniquely named peer terminal, optionally waiting for content ACK."
    )
    timeout_seconds = 310
    parameters = {
        "type": "object",
        "properties": {"to": {"type": "string"}, "text": {"type": "string"}, "require_ack": {"type": "boolean"}},
        "required": ["to", "text"],
        "additionalProperties": False,
    }

    def __init__(self, rpc: RpcCall, *, sender: str = "raven", scope: str = "local/development"):
        super().__init__(rpc)
        self.sender = sender
        self.scope = scope

    async def execute(self, to: str, text: str, require_ack: bool = False, **kwargs: Any) -> str:
        try:
            resolution = await self.rpc("agents.resolve", {"mention": to})
            candidates = resolution.get("candidates", [])
            if not resolution.get("unique") or len(candidates) != 1:
                raise TerminalError(
                    "agent_not_unique", "Resolve a unique live canonical name before sending", resolution
                )
            agent = candidates[0]["agent"]
            binding = agent.get("binding")
            if agent.get("orphan") or not binding:
                raise TerminalError("agent_binding_stale", "Agent has no live binding")
            current = (await self.rpc("terminal.show", {"handle": binding["handle"]}))["terminal"]
            keys = ("handle", "incarnationId", "worktreeId", "tabId", "leafId")
            if any(not binding.get(k) or binding[k] != current.get(k) for k in keys) or not (
                current.get("connected") and current.get("writable") and current.get("orphaned") is False
            ):
                raise TerminalError("agent_binding_stale", "The terminal incarnation no longer matches the identity")
            try:
                envelope = Envelope(sender=self.sender, recipient=agent["agentName"], scope=self.scope, body=text)
            except ValueError as exc:
                raise TerminalError(
                    "invalid_message", "Message must have a nonempty body and valid peer names"
                ) from exc
            if require_ack:
                envelope.body += (
                    "\n\nAfter reading, acknowledge with: "
                    f"raven terminal send --to raven --text 'ack_for={envelope.nonce} received' --json"
                )
            result = await self.rpc(
                "terminal.send",
                {
                    "handle": binding["handle"],
                    "text": envelope.to_text(),
                    "enter": True,
                    "require_ack": require_ack,
                    **self.session_params(),
                },
            )
            return json.dumps(result["send"])
        except TerminalError as exc:
            return _failure(exc)
