"""Shell execution tool."""

import logging
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

_DEVICE_FILES = frozenset(
    {"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero", "/dev/urandom", "/dev/random"}
)


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
        # `is not None`, not `or`: an operator asking for no deny-list at all passes
        # an empty one, and `or` would hand back the defaults they just turned off.
        self.deny_patterns = (
            deny_patterns
            if deny_patterns is not None
            else [
                r"\brm\s+-[rf]{1,2}\b",  # rm -r, rm -rf, rm -fr
                r"\bdel\s+/[fq]\b",  # del /f, del /q
                r"\brmdir\s+/s\b",  # rmdir /s
                r"(?:^|[;&|]\s*)format\b",  # format (as standalone command only)
                r"\b(mkfs|diskpart)\b",  # disk operations
                r"\bdd\s+if=",  # dd
                r">\s*/dev/sd",  # write to disk
                r":\(\)\s*\{.*\};\s*:",  # fork bomb
            ]
        )
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
        return "Shorten the command, or split the work across several runs."

    @property
    def incomplete_hint(self) -> str:
        # Phrased as the consequent of a condition; see Tool.incomplete_hint.
        return "shorten the command, or split the work across several runs."

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
                return self._terminal_error(guard_error, command)
            decision = self._policy.evaluate(command)
            if decision is CommandDecision.HARD_DENY:
                # "denied by policy" and not "evaluation failed": HARD_DENY is a
                # decision, and the old wording described the one cause that is a
                # fault as though it were all three. A refusal nobody can read the
                # reason off is answered by abandoning the task -- one measured run
                # replied "no alternative method will be attempted" and ended with
                # the deck unbuilt.
                return self._terminal_error(
                    "Error: this command is denied by the exec policy (a deny pattern, "
                    "or a system-power command). Nothing about it can be retried; "
                    "reach the same end another way",
                    command,
                )
            if decision is CommandDecision.REQUIRE_APPROVAL:
                approval_error = await self._request_approval(command)
                if approval_error:
                    return approval_error
        elif self.restrict_to_workspace:
            # Sandboxed: skip the deny-list (microVM provides real isolation), but still
            # enforce workspace restriction so operator-set boundaries are respected.
            workspace_error = self._check_workspace_restriction(command, cwd)
            if workspace_error:
                return self._terminal_error(workspace_error, command)

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
            return self._terminal_error("Error: User denied this command earlier in the current turn", command)
        if turn.responder is None or not turn.conversation_id:
            # Nobody to ask is not the same as being told no. A denial is a decision
            # to respect rather than route around, which is what `abort_action`
            # enforces by ending the turn; an absent approval channel is a fact about
            # where this is running, and ending the turn on it costs everything the
            # model could still do without a shell. Two headless deck runs ended
            # here with eighteen of twenty pages unwritten -- one cropping a logo,
            # one reading its own materials -- and neither was refused by anyone.
            #
            # The command still does not run, and nor will any other that needs
            # approval, so there is no equivalent route to be routed to. Only the
            # turn survives.
            return ToolResult(
                model_text=(
                    "Error: this command needs approval and this turn has nobody to ask, so it was "
                    "not run. No shell command that needs approval will run here. Carry on with the "
                    "rest of the task by another route, and say what you could not do."
                ),
                retryable=False,
                abort_action=False,
            )
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
        return self._terminal_error("Error: User denied this command or the approval request expired", command)

    @classmethod
    def _terminal_error(cls, message: str, command: str = "") -> ToolResult:
        """Return a policy result that the registry and agent loop cannot retry.

        The command goes to the log. A refusal that does not say what it refused
        cannot be told apart from a fault, and this one ends the turn: two headless
        deck runs stopped here with eighteen of twenty pages unwritten and the only
        record was that the turn had ended. Truncated, because a refused command is
        untrusted text and the point is to recognise it, not to store it.
        """
        if command:
            logging.getLogger(__name__).warning("exec refused (%s) -- command: %s", message, command[:400])
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

        # The workspace bounds this, not the directory a call chose to run in.
        # A caller may work anywhere inside the workspace, and measuring against
        # its own choice breaks both ways: a build directory could not list the
        # figures beside it, and `working_dir="/"` would admit the filesystem.
        fence = Path(self.working_dir or cwd).expanduser().resolve()
        requested = Path(cwd).expanduser().resolve()
        if not self._within(requested, fence):
            return (
                f"Error: Command blocked by safety guard: working_dir {requested} is outside the "
                f"workspace {fence}. Pick a directory inside it, or leave working_dir unset."
            )
        for raw in self._extract_absolute_paths(cmd):
            try:
                expanded = os.path.expandvars(raw.strip())
                # The null-device family is how a shell mutes a stream, not an
                # escape from the workspace: `2>/dev/null` names a path only to
                # the guard's regex.
                if expanded in _DEVICE_FILES:
                    continue
                p = Path(expanded).expanduser().resolve()
            except Exception:
                continue
            if p.is_absolute() and not self._within(p, fence):
                # Which path, because the caller has to repair the command and one
                # refusal covers the whole of it. A live run wrote
                # `ls <workspace>/figures/ && ls /tmp/`, was told only that a path
                # was outside, could not tell which half offended, and ended the
                # run asking a user who was not there whether to continue.
                return (
                    f"Error: Command blocked by safety guard: {p} is outside the workspace "
                    f"{fence}. Every path this command names has to be inside it -- drop that part "
                    "or point it at the workspace, and run the rest."
                )

        return None

    @staticmethod
    def _within(path: Path, fence: Path) -> bool:
        return path == fence or fence in path.parents

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command)
        home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command)
        return win_paths + posix_paths + home_paths
