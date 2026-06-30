"""Shell execution tool."""

import os
import re
import shlex
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
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
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        executor: SandboxExecutor | None = None,
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
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",  # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self._executor: SandboxExecutor = executor if executor is not None else DirectExecutor()

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

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
