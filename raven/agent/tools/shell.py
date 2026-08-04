"""Shell execution tool."""

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import Any

from raven.agent.tools.background import BackgroundJobRegistry
from raven.agent.tools.base import Tool
from raven.sandbox import DirectExecutor, ExecSession, SandboxExecutor

# ``rm`` carrying a recursive flag, tolerating other flags on either side of it
# (``rm -f -r --``). Matched against the lower-cased command.
_RM_RECURSIVE = r"\brm\s+(?:-\S+\s+)*-\S*r\S*\s+(?:-\S+\s+)*"
_END = r"(?:\s|;|&|\||$)"
_SYSTEM_DIRS = "bin|boot|dev|etc|lib|lib64|proc|root|sbin|sys|usr|var"
# Anchor a name to command position (start of input / after ;|&&/newline /
# after sudo) so it only matches when *invoked*, never as a flag or argument.
# Without this, `qemu ... -no-shutdown` matched the bare \bshutdown\b pattern
# and `which mkfs.ext4` matched \bmkfs\b — both observed stranding agents in
# eval. Newline is a command separator in shell, so it anchors too (`^` alone
# only matches input start — a second line would slip past).
_CMD = r"(?:^|[;&|\n]\s*|\bsudo\s+)"


class ExecSessionRegistry:
    """Named, long-lived shells shared by exec / exec_write / exec_read.

    Owned by the agent loop rather than by a single tool because all three tools
    address the same shells by name; the loop closes them on shutdown so a
    session cannot outlive the agent.
    """

    MAX_SESSIONS = 8

    def __init__(self, executor: SandboxExecutor):
        self._executor = executor
        self._sessions: dict[str, ExecSession] = {}
        self._background_seq = 0

    @property
    def supported(self) -> bool:
        return self._executor.supports_sessions

    def names(self) -> list[str]:
        return sorted(self._sessions)

    def get(self, name: str) -> ExecSession | None:
        return self._sessions.get(name)

    def next_background_name(self) -> str:
        self._background_seq += 1
        return f"bg{self._background_seq}"

    async def open(self, name: str, cwd: str | None, env: dict[str, str] | None) -> ExecSession:
        existing = self._sessions.get(name)
        if existing is not None:
            if existing.running:
                return existing
            await self.close(name)
        if len(self._sessions) >= self.MAX_SESSIONS:
            raise RuntimeError(
                f"too many open sessions ({self.MAX_SESSIONS}); close one first: {', '.join(self.names())}"
            )
        session = await self._executor.open_session(cwd=cwd, env=env)
        self._sessions[name] = session
        return session

    async def close(self, name: str) -> bool:
        session = self._sessions.pop(name, None)
        if session is None:
            return False
        await session.close()
        return True

    async def close_all(self) -> None:
        for name in list(self._sessions):
            await self.close(name)


class ExecTool(Tool):
    """Tool to execute shell commands."""

    # Scoped at catastrophic, unrecoverable operations only. Ordinary destructive
    # work below the first directory level (``rm -f build/*.o``,
    # ``rm -rf /tmp/test-clone``, ``dd if=/dev/zero of=disk.img``) is routine, and
    # blocking it strands the agent with no alternative path.
    DEFAULT_DENY_PATTERNS = (
        rf"\brm\s+(?:-\S+\s+)*(?:/|/\*|~|~/\*|\$home){_END}",  # rm of / or the home root
        rf"{_RM_RECURSIVE}/[^/\s;&|]+/?{_END}",  # recursive rm of a whole first-level directory
        rf"{_RM_RECURSIVE}/(?:{_SYSTEM_DIRS})(?:/|{_END})",  # recursive rm inside a system tree
        r"\bdel\s+/[fq]\b",  # del /f, del /q
        r"\brmdir\s+/s\b",  # rmdir /s
        r"(?:^|[;&|\n]\s*)format\b",  # format (as standalone command only)
        rf"{_CMD}(?:mkfs(?:\.\w+)?|diskpart)\b",  # disk format commands, invoked only
        r"\bdd\s+[^|;&]*of=/dev/(sd|hd|vd|nvme|xvd)",  # raw write to a block device
        r">\s*/dev/sd",  # write to disk
        rf"{_CMD}(?:shutdown|reboot|poweroff)\b",  # system power, invoked only
        r":\(\)\s*\{.*\};\s*:",  # fork bomb
    )

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
        sessions: ExecSessionRegistry | None = None,
        jobs: BackgroundJobRegistry | None = None,
        max_timeout: int | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.max_timeout = max_timeout if max_timeout is not None else self._MAX_TIMEOUT
        # Backstop above the per-command cap; the executor's own timeout fires
        # first, this only catches a wedged executor.
        self.timeout_seconds = float(self.max_timeout + 60)
        # ``None`` selects the built-in defaults; an empty list disables the
        # deny-list entirely (tools.exec.deny_patterns: [] — for throwaway
        # containers such as benchmark sandboxes, where blocking ordinary
        # cleanup commands costs turns and buys no isolation).
        self.deny_patterns = list(self.DEFAULT_DENY_PATTERNS) if deny_patterns is None else list(deny_patterns)
        # Operator-configurable extras (tools.exec.extra_deny_patterns), appended
        # to the built-in defaults; empty by default so product behaviour is
        # unchanged. The proactivity-eval harness sets these to block host GUI
        # automation (osascript / `open -a|-b`) because it runs the agent
        # un-sandboxed on the operator's machine — not a product default.
        if extra_deny_patterns:
            self.deny_patterns = self.deny_patterns + list(extra_deny_patterns)
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self._executor: SandboxExecutor = executor if executor is not None else DirectExecutor()
        self.sessions = sessions if sessions is not None else ExecSessionRegistry(self._executor)
        self.jobs = jobs

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 30_000

    @property
    def description(self) -> str:
        return (
            "Execute a shell command (bash when available) and return its output. "
            "Use with caution.\n"
            "Each call is a fresh process by default, so cwd, exported variables "
            "and background jobs do NOT carry over between calls.\n"
            "Pass `session` to run inside a persistent shell instead: state "
            "carries over, and interactive programs (ssh, REPLs, debuggers) can "
            "then be driven with exec_write / exec_read. Sessions die when the "
            "agent exits — never host a server in one.\n"
            "Pass `background: true` for work that must outlive this call and the "
            "run itself (servers, long builds): it returns immediately, the process "
            "is detached with its output going to a log file, and it keeps running "
            "after you finish. Follow it with job_status, stop it with job_cancel.\n"
            "When killing processes, target an exact PID or process group; broad "
            "`pkill -f` patterns can match unrelated processes, including your own "
            "tooling."
        )

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
                        f"Timeout in seconds. Increase for long-running commands "
                        f"like compilation or installation (default 60, max "
                        f"{self.max_timeout}; larger values are clamped — use "
                        f"background:true for anything longer). With "
                        f"session/background this is how long to wait for "
                        f"output, not a limit on the command itself."
                    ),
                    "minimum": 1,
                },
                "session": {
                    "type": "string",
                    "description": (
                        "Name of a persistent shell to run the command in, created "
                        "on first use. Reuse the same name to keep cwd, environment "
                        "and running jobs across calls. The shell dies when the "
                        "agent exits, so it is for interactive state, not for "
                        "leaving things running."
                    ),
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        f"Return without waiting for the command to finish. The process "
                        f"is detached and survives both this call and the end of the run, "
                        f"so use it for servers the task needs left running and for work "
                        f"beyond the {self.max_timeout}s ceiling. A job name is reported "
                        f"back for job_status / job_wait / job_cancel (with `session` set, "
                        f"that name is used for the job)."
                    ),
                },
            },
            "required": ["command"],
        }

    # Grace period a background command gets before exec returns, so a command
    # that fails immediately reports its error in the same turn rather than
    # forcing a follow-up exec_read to discover it.
    _BACKGROUND_GRACE_S = 3.0

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        session: str | None = None,
        background: bool = False,
        **kwargs: Any,
    ) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()

        if not self._executor.is_sandboxed:
            # Non-sandboxed: full guard — deny-list patterns AND workspace restriction.
            guard_error = self._guard_command(command, cwd)
            if guard_error:
                return guard_error
        elif self.restrict_to_workspace:
            # Sandboxed: skip the deny-list (microVM provides real isolation), but still
            # enforce workspace restriction so operator-set boundaries are respected.
            workspace_error = self._check_workspace_restriction(command, cwd)
            if workspace_error:
                return workspace_error

        # Use `is None` check — `timeout or default` would treat timeout=0 as falsy.
        # Clamp rather than reject an over-limit timeout: the model's intent
        # ("this needs longer") is valid, only the number is out of range, and a
        # schema rejection was observed burning 5+ turns of identical retries.
        requested_timeout = self.timeout if timeout is None else timeout
        effective_timeout = min(requested_timeout, self.max_timeout)
        clamp_note = ""
        if requested_timeout > self.max_timeout:
            clamp_note = (
                f"[note: timeout {requested_timeout}s clamped to the {self.max_timeout}s "
                f"ceiling; for longer work run with background:true and wait with "
                f"job_wait / poll with job_status]\n"
            )

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

        # `background` always means "outlive this call, and the agent too" — a
        # detached job, never a shell-hosted process. When a session name is also
        # given it is used as the job's name: in eval, every model that combined
        # the two meant "named background process", and the old session-hosted
        # interpretation silently killed their servers at agent exit (four
        # verifier-time failures in one TB run).
        if background and self.jobs is not None and self.jobs.supported:
            return await self._start_background_job(command, cwd=cwd, env=env, name=session)

        if session or background:
            return clamp_note + await self._execute_in_session(
                command,
                session=session,
                background=background,
                cwd=cwd,
                env=env,
                timeout=effective_timeout,
            )

        try:
            result = await self._executor.exec(command, cwd=cwd, timeout=effective_timeout, env=env)
        except Exception as e:
            return f"Error executing command: {str(e)}"
        return clamp_note + result.as_text(self._MAX_OUTPUT, spill_prefix="exec")

    async def _start_background_job(
        self, command: str, cwd: str, env: dict[str, str] | None, name: str | None = None
    ) -> str:
        if self.jobs is None:
            return "Error: background jobs are not available with this sandbox backend."
        name = name or self.jobs.next_name()
        try:
            job = await self.jobs.start(name, command, cwd=cwd, env=env)
        except Exception as e:
            return f"Error starting background job: {str(e)}"
        # Give it the same short grace period a session-hosted background command got,
        # so a command that dies on startup reports the error in this turn instead of
        # looking healthy until the next poll.
        await asyncio.sleep(self._BACKGROUND_GRACE_S)
        head = self.jobs.read(name)
        return (
            f"[job: {name}, pid {job.pid}, log {job.log_path}]\n"
            f"{head}\n"
            f"[it keeps running after this call and after the run ends; "
            f"check it with job_status, stop it with job_cancel]"
        )

    async def _execute_in_session(
        self,
        command: str,
        session: str | None,
        background: bool,
        cwd: str,
        env: dict[str, str] | None,
        timeout: int,
    ) -> str:
        if not self.sessions.supported:
            return (
                "Error: persistent sessions are not available with this sandbox backend. "
                "Re-run without `session`/`background`; for long commands, redirect to a "
                "log file and poll it with read_file."
            )
        name = session or self.sessions.next_background_name()
        try:
            live = await self.sessions.open(name, cwd=cwd, env=env)
            await live.write(command + "\n")
            wait_for = self._BACKGROUND_GRACE_S if background and session is None else timeout
            result = await live.read(timeout=wait_for)
        except Exception as e:
            return f"Error executing command in session {name!r}: {str(e)}"
        return f"[session: {name}]\n" + result.as_text(self._MAX_OUTPUT, spill_prefix=f"session-{name}")

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            m = re.search(pattern, lower)
            if m:
                # Name the pattern and the exact match: a blocked agent that only
                # hears "dangerous pattern detected" cannot tell a real denial from
                # a false positive, and retries the same command blindly.
                return (
                    f"Error: Command blocked by safety guard: {m.group(0).strip()!r} "
                    f"matched deny pattern {pattern!r}. If this is a false positive "
                    "(a flag or filename, not the destructive command itself), "
                    "rephrase the command to avoid the match."
                )

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


class _SessionTool(Tool):
    """Shared plumbing for the tools that address an existing exec session."""

    _MAX_OUTPUT = 30_000
    # Above the 600s schema maximum of the `timeout` parameter; without this the
    # registry's 300s default ceiling killed reads the schema had just allowed.
    timeout_seconds = 660.0

    def __init__(self, sessions: ExecSessionRegistry, jobs: BackgroundJobRegistry | None = None):
        self.sessions = sessions
        self.jobs = jobs

    def _resolve(self, session: str) -> ExecSession | str:
        if not self.sessions.supported:
            return "Error: persistent sessions are not available with this sandbox backend."
        live = self.sessions.get(session)
        if live is None:
            known = ", ".join(self.sessions.names()) or "none"
            # Teach the creation path, not just the absence: in eval a model
            # repeated this exact miss 10 times because the error never said
            # how a session comes to exist.
            return (
                f"Error: no session named {session!r}. Open sessions: {known}. "
                f"A session is created by running exec(command=..., session={session!r}) "
                f"first; to read a regular file use read_file instead."
            )
        return live


class ExecWriteTool(_SessionTool):
    """Tool to send input to a running exec session."""

    @property
    def name(self) -> str:
        return "exec_write"

    @property
    def description(self) -> str:
        return (
            "Send input to a shell started by exec(session=...). Use it to answer "
            "prompts (ssh/sudo passwords, confirmations), drive a REPL or debugger, "
            "or send control characters such as \\u0003 (Ctrl-C) to interrupt. "
            "A newline is appended unless you set newline to false."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "Name of the session to write to",
                },
                "input": {
                    "type": "string",
                    "description": "Text to write to the session's stdin",
                },
                "newline": {
                    "type": "boolean",
                    "description": "Append a newline to the input (default true)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait for output after writing (default 30)",
                    "minimum": 1,
                    "maximum": 600,
                },
            },
            "required": ["session", "input"],
        }

    async def execute(
        self,
        session: str,
        input: str,
        newline: bool = True,
        timeout: int = 30,
        **kwargs: Any,
    ) -> str:
        live = self._resolve(session)
        if isinstance(live, str):
            return live
        try:
            await live.write(input + "\n" if newline else input)
            result = await live.read(timeout=timeout)
        except Exception as e:
            return f"Error writing to session {session!r}: {str(e)}"
        return result.as_text(self._MAX_OUTPUT, spill_prefix=f"session-{session}")


class ExecReadTool(_SessionTool):
    """Tool to read pending output from a running exec session or background job."""

    @property
    def name(self) -> str:
        return "exec_read"

    @property
    def description(self) -> str:
        return (
            "Read output produced since the last read by a shell started with "
            "exec(session=...), or by a background job started with "
            "exec(background=true). Poll this to follow a long build or a server's "
            "log. Set close to true to terminate a session once you are done with it "
            "(to stop a background job use job_cancel instead)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "Name of the session to read from",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait for new output (default 30)",
                    "minimum": 1,
                    "maximum": 600,
                },
                "close": {
                    "type": "boolean",
                    "description": "Terminate the session after this read (default false)",
                },
            },
            "required": ["session"],
        }

    async def execute(
        self,
        session: str,
        timeout: int = 30,
        close: bool = False,
        **kwargs: Any,
    ) -> str:
        # A background job is not a session, but the model addresses both by the name
        # exec handed back. Resolving jobs here keeps one polling verb instead of
        # making the caller remember which kind of thing it started.
        if self.jobs is not None and self.jobs.get(session) is not None:
            if close:
                return await self.jobs.cancel(session)
            return self.jobs.read(session)
        live = self._resolve(session)
        if isinstance(live, str):
            return live
        try:
            result = await live.read(timeout=timeout)
        except Exception as e:
            return f"Error reading session {session!r}: {str(e)}"
        text = result.as_text(self._MAX_OUTPUT, spill_prefix=f"session-{session}")
        if close:
            await self.sessions.close(session)
            text += f"\n[session {session} closed]"
        return text
