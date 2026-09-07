"""Shell execution tool."""

import os
import re
import shlex
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent import workdir
from raven.agent.tools.shell_policy import CommandDecision, ShellCommandPolicy, executable_text
from raven.contracts.asking import ApprovalResponder
from raven.contracts.tool import Continuation, Tool, ToolOutput, ToolResult
from raven.sandbox import DirectExecutor, SandboxExecutor


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


# Why a command was refused, in the words the reader needs. The old message
# said "policy evaluation failed" for all four causes, which is what the session
# this work came from asked about four times -- and what the model, given
# nothing to go on, then guessed wrong about twice.
#
# Each line names the rule and where it lives, because these refusals are
# acted on: a deny pattern is the operator's list to edit, a parse error is the
# command's own to fix. Absent from this map, the generic text stands: a
# message that names the wrong rule is worse than one that names none.
_DENY_REASONS: dict[str, str] = {
    "deny_pattern": "matches a denied pattern (tools.exec.extraDenyPatterns, plus the built-in list)",
    "recursive_delete": "deletes a directory tree recursively",
    "system_power": "powers the machine off or reboots it",
    "parse_error": "could not be parsed as a shell command; an unbalanced quote is the usual cause",
}

# What the reader is being asked about, per family. A constant string was here
# before -- "Delete files using a shell command" -- which was accurate only
# while deletion was the one family registered, and became wrong the moment a
# surface registered more. An unlisted family falls back to deliberately vague
# text rather than a guess, for the reason the map above gives.
_APPROVAL_DESCRIPTIONS: dict[str, str] = {
    "delete_command": "Delete files using a shell command",
    "publish_command": "Publish or push work to a remote",
    "install_command": "Install software, which runs code from the network",
    "remote_exec_command": "Run a command on, or copy files to, another machine",
    "credential_command": "Read or change stored credentials",
    "destructive_vcs_command": "Discard uncommitted work in this repository",
    "fetch_side_effect": "Download to a file, upload data, or run what it downloads",
}


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
        allow_destructive_commands: bool = False,
        extra_allowed_dirs: tuple[Path, ...] = (),
        *,
        extra_deny_source: Callable[[], list[str] | None] | None = None,
        allow_destructive_source: Callable[[], bool | None] | None = None,
        follow_binding: bool = True,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        # `rm` is absent here on purpose: the policy classifies it from tokens
        # (`_matches_recursive_delete` hard-denies a recursive one, everything
        # else goes to approval). A regexp here cannot tell `rm -rf /` from
        # `rm -f a.py b.json`, and denying both means the agent cannot clean up
        # after itself -- with no prompt offered, because hard deny outranks
        # approval.
        self.deny_patterns = deny_patterns or [
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
        # ``extra_deny_source`` is the live form of the same list: read before
        # each classification (see ``_refresh_deny_patterns``), so a pattern
        # added to the file blocks the very next call -- tightening must not
        # wait for the next turn, let alone the next process.
        self._base_deny_patterns = list(self.deny_patterns)
        self._extra_deny_current = list(extra_deny_patterns or [])
        self._extra_deny_source = extra_deny_source
        self._allow_destructive_current = bool(allow_destructive_commands)
        self._allow_destructive_source = allow_destructive_source
        if extra_deny_patterns:
            self.deny_patterns = self.deny_patterns + list(extra_deny_patterns)
        self._policy = ShellCommandPolicy(
            deny_patterns=self.deny_patterns,
            allow_destructive_commands=allow_destructive_commands,
        )
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.extra_allowed_dirs = extra_allowed_dirs
        # A sub-agent run is a background asyncio task that can outlive the turn
        # that spawned it, since SubagentManager.spawn captures the workspace at
        # spawn time; its ExecTool must resolve cwd from the directory captured
        # for that run, never from the ambient binding, or it disagrees with its
        # own fs tools about which directory it is in (the same follow_binding
        # convention as the filesystem tools). The main loop's ExecTool keeps
        # following the live binding as normal.
        self.follow_binding = follow_binding
        self.path_append = path_append
        self._executor: SandboxExecutor = executor if executor is not None else DirectExecutor()
        self._approval_turn: ContextVar[_ApprovalTurn] = ContextVar(
            "exec_tool_approval_turn",
            default=_ApprovalTurn(),
        )

    def _refresh_allow_destructive(self) -> None:
        """Apply the latest deletion-safety preference before classifying a command."""
        if self._allow_destructive_source is None:
            return
        try:
            allowed = self._allow_destructive_source()
        except Exception:
            return
        if allowed is None:
            return
        allowed = bool(allowed)
        if allowed == self._allow_destructive_current:
            return
        self._policy.set_allow_destructive_commands(allowed)
        self._allow_destructive_current = allowed

    def _refresh_deny_patterns(self) -> None:
        """Track the operator's extra deny list as it stands on disk.

        Runs at the top of every :meth:`execute`, not once per turn: tightening
        a permission must bind the tool call that is about to run, and the
        classification below is the only gate it crosses. Loosening works the
        same way -- the list is the operator's own choice in both directions.

        A reader answering ``None`` means "no live answer" (no config section,
        or no source at all) and keeps the constructor's extras. A pattern that
        does not compile rejects the whole edit and keeps the current policy:
        half-armed is the one state this must never leave behind.
        """
        if self._extra_deny_source is None:
            return
        try:
            extras = self._extra_deny_source()
        except Exception:
            return
        if extras is None:
            return
        extras = [str(p) for p in extras]
        if extras == self._extra_deny_current:
            return
        patterns = self._base_deny_patterns + extras
        try:
            self._policy.set_deny_patterns(patterns)
        except re.error as exc:
            logger.warning("tools.exec extra deny patterns rejected ({}); keeping the current set", exc)
            return
        self._extra_deny_current = extras
        self.deny_patterns = patterns

    def register_approval_matcher(self, name: str, matcher: Callable[[str], bool]) -> None:
        """Add a command family this tool must ask about before running.

        The policy is per-tool rather than process-wide, so a surface that needs
        to ask about more than deletion has to reach it through the tool it will
        actually run on. Exposed here because the alternative is a caller
        touching ``_policy`` -- and the set of families a surface asks about is a
        property of that surface, not of the policy's internals.

        See ``shell_policy.EXTERNAL_EFFECT_MATCHERS`` for the group ``raven acp``
        registers and for why the terminal does not.
        """
        self._policy.register_approval_matcher(name, matcher)

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
    # Said as a refusal of the command, not of the task: an unattended agent told to
    # "stop this operation immediately" read it as the whole job and ended a deck
    # build with nothing published, on a curl loop a pattern happened to match.
    _STOP_INSTRUCTION = (
        " This command will not run here. Do not retry it with another command, "
        "tool, script, interpreter, or equivalent method; carry on with the rest of "
        "the task without it."
    )
    # A parse failure is the command's own to fix, not a protected action: the
    # stop instruction above told the model to abandon the install it was doing.
    _PARSE_INSTRUCTION = (
        " This command will not run here as written. Close the quote, or write the "
        "script to a file with the file tools and run that file, then retry."
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
        self._refresh_allow_destructive()
        self._refresh_deny_patterns()
        bound = str(workdir.current() or "") if self.follow_binding else ""
        cwd = working_dir or bound or self.working_dir or os.getcwd()

        sandboxed = self._executor.is_sandboxed
        if not sandboxed:
            # Non-sandboxed: full guard — deny-list patterns AND workspace restriction.
            guard_error = self._guard_command(command, cwd)
            if guard_error:
                return self._terminal_error(guard_error)
        elif self.restrict_to_workspace:
            # Sandboxed: skip the deny-list (microVM provides real isolation), but still
            # enforce workspace restriction so operator-set boundaries are respected.
            workspace_error = self._check_workspace_restriction(command, cwd)
            if workspace_error:
                return self._terminal_error(workspace_error)
        # Classification runs either way, and the sandbox flag reaches it rather
        # than skipping it. A microVM contains what a command does to files; it
        # does not contain a push, an install, or a connection to another machine.
        # Short-circuiting the whole check on the flag made the SAFER
        # configuration prompt less than the plain one, for exactly the
        # operations the sandbox has no say over.
        outcome = self._policy.classify(command, sandboxed=sandboxed)
        if outcome.decision is CommandDecision.HARD_DENY:
            why = _DENY_REASONS.get(outcome.reason_code, "")
            message = (
                f"Error: Command blocked by safety guard: it {why}" if why else "Error: Command blocked by safety guard"
            )
            if outcome.reason_code == "parse_error":
                # Not a protected action, so not a reason to end the turn: the
                # command is the model's own to fix, and ending the turn here is
                # what turned one unclosed quote into an hour of design work
                # abandoned one step before delivery.
                return ToolResult(model_text=message + self._PARSE_INSTRUCTION, ok=False)
            return self._terminal_error(message)
        if outcome.decision is CommandDecision.REQUIRE_APPROVAL:
            approval_error = await self._request_approval(command, sandboxed=sandboxed)
            if approval_error:
                return approval_error

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
        text = result.as_text(self._MAX_OUTPUT)
        # The exit code is the verdict a config change, a security call or a
        # syntax error share, and the text a failing command produced is not
        # token-safe to classify from -- so the caller gets it structurally.
        return ToolOutput(text, ok=result.exit_code == 0)

    async def _request_approval(self, command: str, *, sandboxed: bool = False) -> ToolResult | None:
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
            description=_APPROVAL_DESCRIPTIONS.get(
                self._policy.approval_reason(command, sandboxed=sandboxed) or "",
                "Run a command that needs your approval",
            ),
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
            blocks_call=True,
            # Every refusal still ends the turn. Which of them should not is
            # the next change, and it needs somewhere to say so first.
            continuation=Continuation.ABORT_TURN,
            ok=False,
        )

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands.

        Reads the same lexical view the policy does, but only ever with regex --
        no shlex pass, so no fail-closed branch to reach. With the deny list
        moved to its one owner, what this gate still decides by itself is the
        allowlist and the workspace boundary -- and a comment could reach both:
        `allow_patterns` was matched against text the shell discards, so a
        command could be talked onto the allowlist, and a path named in a
        comment was scanned as a path the command touches.
        """
        cmd = executable_text(command).strip()
        lower = cmd.lower()

        # The deny list is not read here. `ShellCommandPolicy` was constructed
        # with this same list and classifies against it a few lines later, so
        # running it twice bought nothing and cost a second message for one
        # cause -- "dangerous pattern detected" here, and whatever the policy
        # said there. One owner, one sentence.
        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        workspace_error = self._check_workspace_restriction(command, cwd)
        if workspace_error:
            return workspace_error

        return None

    # The null-device family is how a shell mutes or feeds a stream, not an
    # escape from the workspace: `2>/dev/null` names a path only to the
    # guard's regex, and blocking it turns every quiet read-only probe into a
    # terminal safety refusal. Matched on the written form, before resolve(),
    # which on macOS follows /dev/stdout into /dev/fd and out of this set.
    _DEVICE_FILES = frozenset(
        {
            "/dev/null",
            "/dev/stdin",
            "/dev/stdout",
            "/dev/stderr",
            "/dev/tty",
            "/dev/zero",
            "/dev/urandom",
            "/dev/random",
        }
    )

    def _check_workspace_restriction(self, command: str, cwd: str) -> str | None:
        """Check only the workspace boundary constraints (no deny/allow-list).

        Reads the executable view rather than trusting the caller to strip: the
        traversal and absolute-path scans have no reason to see text the shell
        discards, and there are two call sites -- the guard and the sandboxed
        path -- so doing it here is what keeps them from diverging. Reading the
        raw text refused ``ls -la  # see ../notes for why`` as path traversal.
        """
        if not self.restrict_to_workspace:
            return None
        command = executable_text(command)

        cmd = command.strip()
        if "..\\" in cmd or "../" in cmd:
            return "Error: Command blocked by safety guard (path traversal detected)"

        cwd_path = Path(cwd).resolve()
        roots = [cwd_path, *(Path(d).resolve() for d in self.extra_allowed_dirs)]
        for raw in self._extract_absolute_paths(cmd):
            try:
                expanded = os.path.expandvars(raw.strip())
                if expanded in self._DEVICE_FILES:
                    continue
                p = Path(expanded).expanduser().resolve()
            except Exception:
                continue
            if not p.is_absolute():
                continue
            if any(root == p or root in p.parents for root in roots):
                continue
            return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        # The boundary class must cover every character a path can be glued
        # to, not just whitespace: --file=/etc/passwd, </etc/passwd,
        # cmd;/bin/x, $(/usr/bin/id) and `/bin/x` all name a path with no
        # space before it, and a boundary the class misses is a fence bypass.
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)
        posix_paths = re.findall(r"(?:^|[\s|>'\"=<;(`])(/[^\s\"'>;|<]+)", command)
        home_paths = re.findall(r"(?:^|[\s|>'\"=<;(`])(~[^\s\"'>;|<]*)", command)
        return win_paths + posix_paths + home_paths
