"""Shell execution tool."""

import os
import re
import shlex
from contextvars import ContextVar
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from raven.agent.tools.base import Tool, ToolResult
from raven.agent.tools.shell_policy import CommandDecision, ShellCommandPolicy
from raven.sandbox import DirectExecutor, SandboxExecutor


class ApprovalResponder(Protocol):
    """Turn-scoped capability that can approve one exact shell command."""

    async def await_approval(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        tool_call_id: str,
        command: str,
        description: str,
    ) -> bool: ...


@dataclass(frozen=True)
class _ApprovalTurn:
    """Approval state isolated by async context for one agent turn.

    ``denied_digests`` suppresses duplicate prompts only within this turn; a
    later user turn receives a fresh decision boundary.
    """

    responder: ApprovalResponder | None = None
    conversation_id: str = ""
    turn_id: str = ""
    tool_call_id: str = ""
    denied_digests: frozenset[str] = frozenset()


class ExecTool(Tool):
    """Tool to execute shell commands."""

    # Backstop above the 600s internal exec cap (``_MAX_TIMEOUT``); the
    # executor's own timeout fires first, this only catches a wedged executor.
    timeout_seconds = 660.0

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        executor: SandboxExecutor | None = None,
        extra_deny_patterns: list[str] | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",  # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",  # del /f, del /q
            r"\brmdir\s+/s\b",  # rmdir /s
            r"(?:^|[;&|]\s*)format\b",  # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",  # disk operations
            r"\bdd\s+if=",  # dd
            r">\s*/dev/sd",  # write to disk
            r":\(\)\s*\{.*\};\s*:",  # fork bomb
        ]
        # Operator-configurable extras (tools.exec.extra_deny_patterns), appended
        # to the built-in defaults; empty by default so product behaviour is
        # unchanged. The proactivity-eval harness sets these to block host GUI
        # automation (osascript / `open -a|-b`) because it runs the agent
        # un-sandboxed on the operator's machine — not a product default.
        if extra_deny_patterns:
            self.deny_patterns = self.deny_patterns + list(extra_deny_patterns)
        self._policy = ShellCommandPolicy(deny_patterns=self.deny_patterns)
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self._executor: SandboxExecutor = executor if executor is not None else DirectExecutor()
        self._approval_turn: ContextVar[_ApprovalTurn] = ContextVar(
            "exec_tool_approval_turn",
            default=_ApprovalTurn(),
        )

    def start_approval_turn(
        self,
        responder: ApprovalResponder | None,
        *,
        conversation_id: str,
        turn_id: str,
    ) -> None:
        """Bind or revoke interactive approval capability for the current turn."""

        self._approval_turn.set(
            _ApprovalTurn(
                responder=responder,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
        )

    def set_tool_call_id(self, tool_call_id: str) -> None:
        """Attach the provider call ID so approval is auditable end to end."""

        self._approval_turn.set(replace(self._approval_turn.get(), tool_call_id=tool_call_id))

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000
    # This text is model-facing, not the user-facing turn summary. It closes
    # the common loophole where a denied ``rm`` is translated into Python,
    # Perl, or another shell form that performs the same protected action.
    # Runtime enforcement in AgentLoop is still authoritative; the instruction
    # keeps traces and any non-AgentLoop registry consumers equally explicit.
    _STOP_INSTRUCTION = (
        " Stop this operation immediately. Do not retry it with another command, "
        "tool, script, interpreter, or equivalent method."
    )

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def truncation_hint(self) -> str:
        # "Send it in smaller pieces" is meaningless for a command: half a
        # command is not a command. What splits here is the work, not the
        # argument.
        return "A command cannot be sent in pieces -- shorten it, or split the work across several runs."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Timeout in seconds. Increase for long-running commands "
                        "like compilation or installation (default 60, max 600)."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        cwd = working_dir or self.working_dir or os.getcwd()

        if not self._executor.is_sandboxed:
            # Non-sandboxed: full guard — deny-list patterns AND workspace restriction.
            guard_error = self._guard_command(command, cwd)
            if guard_error:
                return self._terminal_error(guard_error)
            decision = self._policy.evaluate(command)
            if decision is CommandDecision.HARD_DENY:
                return self._terminal_error("Error: Command blocked by safety guard (policy evaluation failed)")
            if decision is CommandDecision.REQUIRE_APPROVAL:
                approval_error = await self._request_approval(command)
                if approval_error:
                    return approval_error
        elif self.restrict_to_workspace:
            # Sandboxed: skip the deny-list (microVM provides real isolation), but still
            # enforce workspace restriction so operator-set boundaries are respected.
            workspace_error = self._check_workspace_restriction(command, cwd)
            if workspace_error:
                return self._terminal_error(workspace_error)

        # Use `is None` check — `timeout or default` would treat timeout=0 as falsy.
        effective_timeout = min(self.timeout if timeout is None else timeout, self._MAX_TIMEOUT)

        env: dict[str, str] | None = None
        if self.path_append:
            if self._executor.is_sandboxed:
                # Inject path inside the VM via command wrapper; never pass os.environ
                # to a sandboxed executor — it would leak host credentials into the VM.
                command = f'export PATH="$PATH:{shlex.quote(self.path_append)}" && {command}'
            else:
                # Pass ONLY the PATH override. Copying os.environ here would hand
                # the full host environment to DirectExecutor and defeat its
                # baseline-allowlist hygiene; the executor supplies the rest.
                base_path = os.environ.get("PATH", "")
                env = {"PATH": base_path + os.pathsep + self.path_append}

        try:
            result = await self._executor.exec(command, cwd=cwd, timeout=effective_timeout, env=env)
        except Exception as e:
            return f"Error executing command: {str(e)}"
        return result.as_text(self._MAX_OUTPUT)

    async def _request_approval(self, command: str) -> ToolResult | None:
        """Request one-shot authority for an exact command, failing closed.

        The responder belongs to the current turn and is installed only for an
        Origin with a trusted approval transport. A missing responder therefore
        means "cannot approve", not "approval unnecessary". A digest rejected
        earlier in the same turn is remembered to avoid prompting repeatedly.
        """
        turn = self._approval_turn.get()
        digest = sha256(command.encode()).hexdigest()
        if digest in turn.denied_digests:
            return self._terminal_error("Error: User denied this command earlier in the current turn")
        if turn.responder is None or not turn.conversation_id:
            return self._terminal_error("Error: Command requires user approval, but this turn is not interactive")
        approved = await turn.responder.await_approval(
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            tool_call_id=turn.tool_call_id,
            command=command,
            description="Delete files using a shell command",
        )
        if approved:
            return None
        self._approval_turn.set(
            replace(
                turn,
                denied_digests=turn.denied_digests | {digest},
            )
        )
        return self._terminal_error("Error: User denied this command or the approval request expired")

    @classmethod
    def _terminal_error(cls, message: str) -> ToolResult:
        """Return a policy result that the registry and agent loop cannot retry."""
        return ToolResult(
            model_text=message + cls._STOP_INSTRUCTION,
            retryable=False,
            abort_action=True,
        )

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        workspace_error = self._check_workspace_restriction(command, cwd)
        if workspace_error:
            return workspace_error

        return None

    def _check_workspace_restriction(self, command: str, cwd: str) -> str | None:
        """Check only the workspace boundary constraints (no deny/allow-list)."""
        if not self.restrict_to_workspace:
            return None

        cmd = command.strip()
        if "..\\" in cmd or "../" in cmd:
            return "Error: Command blocked by safety guard (path traversal detected)"

        cwd_path = Path(cwd).resolve()
        for raw in self._extract_absolute_paths(cmd):
            try:
                expanded = os.path.expandvars(raw.strip())
                p = Path(expanded).expanduser().resolve()
            except Exception:
                continue
            if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command)
        home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command)
        return win_paths + posix_paths + home_paths
