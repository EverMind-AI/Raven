"""Shell execution tool.

Authorization lives at the registry's door (``raven.permissions``): whether a
command may run, must ask, or is refused is decided before this tool is
dispatched. What stays here is the tool's own integrity boundary -- the
operator's allowlist and the workspace fence -- and the execution itself.
"""

import os
import re
import shlex
from pathlib import Path
from typing import Any

from raven.agent import workdir
from raven.agent.tools.shell_policy import executable_text
from raven.contracts.tool import STOP_RETRY_INSTRUCTION, Continuation, Tool, ToolOutput, ToolResult
from raven.sandbox import DirectExecutor, SandboxExecutor


class ExecTool(Tool):
    """Tool to execute shell commands."""

    # Backstop above the 600s internal exec cap (``_MAX_TIMEOUT``); the
    # executor's own timeout fires first, this only catches a wedged executor.
    timeout_seconds = 660.0

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        executor: SandboxExecutor | None = None,
        extra_allowed_dirs: tuple[Path, ...] = (),
        *,
        follow_binding: bool = True,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
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

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        # Two descriptions, because the second sentence is a lie for an owner
        # who registered no machines. Read per call rather than fixed at
        # construction -- the registry can be written mid-session, and the loop
        # rebuilds the tool schema every turn.
        base = "Execute a shell command and return its output. Use with caution."
        from raven.agent.tools.machine_exec import machines_registered

        if not machines_registered():
            return base
        return (
            f"{base} "
            "Runs on THIS computer unless you name a machine: pass 'machine' with a "
            "connection id from the owner's registry to run it there instead. A machine "
            "of the owner's is where their code and their cases live, and work that "
            "outlives the call belongs to a job runner, so this refuses to detach there."
        )

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
        schema: dict[str, Any] = {
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
                        "Timeout in seconds for a command on THIS computer. Increase for long-running "
                        "commands like compilation or installation (default 60, max 600). Not read with "
                        "'machine': a look on a registered machine is capped at 60s whatever this says, and "
                        "anything longer there is a job for the on-call agent's ops_submit."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Run detached on THIS computer instead of holding the turn: for long "
                        "mechanical local work (a download, an rsync, packaging). Returns a task id "
                        "and a log path at once; 'timeout' is not read on this lane, the task runs "
                        "until it finishes or the session ends. Read the log with a later exec. Not "
                        "combinable with 'machine': work on a registered machine is ops_submit's job."
                    ),
                },
            },
            "required": ["command"],
        }
        from raven.agent.tools.machine_exec import machines_registered

        if machines_registered():
            schema["properties"]["machine"] = {
                "type": "string",
                "description": "Where to run it: a connection id from the owner's machine "
                "registry, exactly as listed (an unknown id answers with the list). "
                "Leave out to run on THIS computer.",
            }
        return schema

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        machine: str = "",
        run_in_background: bool = False,
        **kwargs: Any,
    ) -> str | ToolResult:
        # Someone else's machine is a different place with different rules: the
        # guards below are about this computer (a deny-list for accidents, an
        # operator's workspace boundary), while there the rule is that work
        # outliving the call belongs on a ledger. Branching before them keeps
        # each set where it means something.
        #
        # Imported here and nowhere else in the execute path: a caller who
        # names no machine must not be able to be broken by the ops layer -- a
        # malformed connection registry has to leave the plain shell working.
        if machine and run_in_background:
            # Background work ON a registered machine is the definition of an
            # ops_submit job; allowing it here would re-legalise the accounting
            # bypass this boundary exists for (a main agent once drove 35 jobs
            # over raw ssh nohup, past the occupancy gate, dedup and the GPU
            # ledger). The two parameters never combine.
            return (
                "Error: 'machine' and 'run_in_background' do not combine. Background work on a "
                "registered machine is a job: submit it with ops_submit (budget, dedup, ledger). "
                "run_in_background is for long mechanical work on THIS computer. Nothing was run."
            )
        if machine:
            from raven.agent.tools.machine_exec import run_on_machine

            return await run_on_machine(command, connection=machine, cwd=working_dir)

        bound = str(workdir.current() or "") if self.follow_binding else ""
        cwd = working_dir or bound or self.working_dir or os.getcwd()

        if not self._executor.is_sandboxed:
            guard_error = self._guard_command(command, cwd)
            if guard_error:
                return self._boundary_error(guard_error)
        elif self.restrict_to_workspace:
            # Sandboxed: the microVM provides real isolation, but the workspace
            # fence is an operator-set boundary and still holds.
            workspace_error = self._check_workspace_restriction(command, cwd)
            if workspace_error:
                return self._boundary_error(workspace_error)

        if run_in_background:
            # After the guards on purpose: the background lane changes where the
            # output goes and who holds the turn, never what a command is
            # allowed to do. The permission gate has already ruled on this call
            # at the registry door, whichever lane it takes.
            if self._executor.is_sandboxed:
                # The detached child is a host process; starting one from a
                # sandboxed session would run the command OUTSIDE the sandbox.
                return (
                    "Error: run_in_background is not available in a sandboxed session -- the "
                    "detached process would run outside the sandbox. Run it synchronously, or "
                    "split the work. Nothing was run."
                )
            from raven.agent.tools import background_exec
            from raven.sandbox.direct_executor import _baseline_env

            # The same environment hygiene as the synchronous path: the child
            # gets the executor's baseline allowlist, never the full host
            # environment, so a detached download cannot read credentials the
            # capped path already withholds.
            bg_env = _baseline_env()
            if self.path_append:
                bg_env["PATH"] = bg_env.get("PATH", "") + os.pathsep + self.path_append
            try:
                task = background_exec.start(command, cwd=cwd, env=bg_env)
            except OSError as exc:
                return f"Error starting background command: {exc}"
            return background_exec.start_note(task)

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

    @classmethod
    def _boundary_error(cls, message: str) -> ToolResult:
        """A fence refusal: this call and its siblings do not run, the turn does."""
        return ToolResult(
            model_text=message + STOP_RETRY_INSTRUCTION,
            retryable=False,
            blocks_call=True,
            continuation=Continuation.CONTINUE,
            ok=False,
        )

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """The tool's own boundary: the operator's allowlist and the workspace fence.

        Reads the executable view (comments and quoted text stripped) so a
        command cannot be talked onto the allowlist by its own comment, and a
        path named in a comment is not scanned as a path the command touches.
        The deny list is not here: refusing dangerous commands is the
        permission gate's job, decided before dispatch.
        """
        cmd = executable_text(command).strip()
        lower = cmd.lower()

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
        """Check only the workspace boundary constraints (no allow-list).

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
