"""Host-agent tools for creating and addressing human-visible peer terminals."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from raven.contracts.terminal import Envelope, TerminalError
from raven.contracts.tool import Tool

RpcCall = Callable[[str, dict], Awaitable[dict]]
_turn_session_key: ContextVar[str | None] = ContextVar("terminal_turn_session", default=None)


@contextmanager
def bind_terminal_session(session_key: str):
    token = _turn_session_key.set(session_key)
    try:
        yield
    finally:
        _turn_session_key.reset(token)


def _failure(exc: TerminalError) -> str:
    return json.dumps({"error": {"code": exc.code, "message": str(exc), "data": exc.data}})


class _TerminalTool(Tool):
    def __init__(self, rpc: RpcCall):
        self.rpc = rpc
        self._session_key = ContextVar("terminal_tool_session", default=None)

    def set_context(self, channel: str, chat_id: str, session_key: str | None = None):
        self._session_key.set(session_key or f"{channel}:{chat_id}")

    def session_params(self):
        session = self.session_key()
        return {"session_id": session} if session else {}

    def session_key(self):
        return _turn_session_key.get() or self._session_key.get()


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
            session_key = self.session_key()
            worktree = self.task_worktree(task, session_key)
            kind, command = self.provider_command(provider, unattended=unattended)
            existing = await self.rpc("agents.resolve", {"mention": name})
            if any(candidate["agent"].get("exitedAt") is None for candidate in existing.get("candidates", [])):
                raise TerminalError("agent_name_exists", "The canonical name or alias already exists")
            created = await self.rpc(
                "terminal.create", {"worktree_id": worktree, "command": command, "title": name, **self.session_params()}
            )
            terminal = created["terminal"]
            try:
                await self.rpc(
                    "agents.register",
                    {
                        "name": name,
                        "kind": kind,
                        "terminal": terminal["handle"],
                        "task_ref": worktree,
                        **({"session_key": session_key} if session_key else {}),
                    },
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
    timeout_seconds = 435
    parameters = {
        "type": "object",
        "properties": {
            "to": {"type": "string"},
            "text": {"type": "string"},
            "require_ack": {"type": "boolean"},
            "force": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Stale Raven pastes are reclaimed automatically; set this only after the human confirms "
                    "the terminal composer is empty to discard tracked human input"
                ),
            },
        },
        "required": ["to", "text"],
        "additionalProperties": False,
    }

    def __init__(self, rpc: RpcCall, *, sender: str = "raven", scope: str = "local/development"):
        super().__init__(rpc)
        self.sender = sender
        self.scope = scope

    async def execute(self, to: str, text: str, require_ack: bool = False, force: bool = False, **kwargs: Any) -> str:
        last_handle = None
        try:
            resolution = await self.rpc("agents.resolve", {"mention": to})
            candidates = resolution.get("candidates", [])
            if len(candidates) == 1:
                candidate = candidates[0]["agent"]
                last_handle = (candidate.get("binding") or {}).get("handle")
                if candidate.get("exitedAt") is not None:
                    raise TerminalError(
                        "agent_binding_stale", "The agent's terminal has exited", {"handle": last_handle}
                    )
            if not resolution.get("unique") or len(candidates) != 1:
                raise TerminalError(
                    "agent_not_unique", "Resolve a unique live canonical name before sending", resolution
                )
            agent = candidates[0]["agent"]
            binding = agent.get("binding")
            if agent.get("orphan") or not binding:
                raise TerminalError("agent_binding_stale", "Agent has no live binding", {"handle": last_handle})
            current = (await self.rpc("terminal.show", {"handle": binding["handle"]}))["terminal"]
            keys = ("handle", "incarnationId", "worktreeId", "tabId", "leafId")
            if any(not binding.get(k) or binding[k] != current.get(k) for k in keys) or not (
                current.get("connected") and current.get("writable") and current.get("orphaned") is False
            ):
                raise TerminalError(
                    "agent_binding_stale",
                    "The terminal incarnation no longer matches the identity",
                    {"handle": last_handle},
                )
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
            params = {
                "handle": binding["handle"],
                "text": envelope.to_text(),
                "enter": True,
                "require_ack": require_ack,
                **self.session_params(),
                **({"force": True} if force else {}),
            }
            try:
                result = await self.rpc("terminal.send", params)
            except TerminalError as blocked:
                if blocked.code == "composer_not_empty":
                    await self.rpc(
                        "terminal.wait", {"handle": binding["handle"], "for": "tui-idle", "timeout_ms": 120000}
                    )
                    try:
                        result = await self.rpc("terminal.send", params)
                    except TerminalError as retry_error:
                        if retry_error.code == "composer_not_empty":
                            raise TerminalError(
                                retry_error.code,
                                "The composer still holds unsubmitted human input after the peer went idle. "
                                "Ask the human to submit or clear it in the terminal tab, or retry with "
                                "force=true once they confirm the composer is empty.",
                                retry_error.data,
                            ) from retry_error
                        raise
                    return json.dumps(result["send"])
                if not (
                    blocked.code == "agent_prompt_blocked"
                    and isinstance(blocked.data, dict)
                    and blocked.data.get("reason") in {"startup_pending", "permission"}
                ):
                    raise
                if blocked.data.get("bytesWritten", 0) != 0:
                    raise TerminalError(
                        blocked.code,
                        "Text may already have been submitted. Ask the human to answer the dialog in the terminal tab "
                        "and verify delivery before retrying later.",
                        blocked.data,
                    ) from blocked
                waited = await self.rpc(
                    "terminal.wait", {"handle": binding["handle"], "for": "tui-idle", "timeout_ms": 120000}
                )
                if not waited["wait"].get("satisfied"):
                    raise TerminalError(
                        "agent_prompt_blocked",
                        "Ask the human to answer the startup or permission dialog in the terminal tab, then retry later.",
                        {**blocked.data, "wait": waited["wait"]},
                    ) from blocked
                try:
                    result = await self.rpc("terminal.send", params)
                except TerminalError as retry_error:
                    if retry_error.code == "agent_prompt_blocked":
                        raise TerminalError(
                            retry_error.code,
                            "Ask the human to answer the startup or permission dialog in the terminal tab, then retry later.",
                            retry_error.data,
                        ) from retry_error
                    raise
            return json.dumps(result["send"])
        except TerminalError as exc:
            if exc.code == "terminal_not_found" and last_handle:
                return _failure(
                    TerminalError(
                        "agent_binding_stale", "The agent's terminal no longer exists", {"handle": last_handle}
                    )
                )
            return _failure(exc)
