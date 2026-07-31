"""Public interfaces for the sandbox package.

Import from here (or from raven.sandbox) — never from boxlite_executor.py
or direct_executor.py directly, so callers remain decoupled from concrete backends.
"""

from __future__ import annotations

import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SandboxInitError(RuntimeError):
    """Raised when the sandbox backend cannot be started or probed.

    Defined in interfaces.py (not in boxlite_executor.py) so it can be imported
    without requiring boxlite to be installed. mcp.py and loop.py import this
    type for error handling; they must not fail just because boxlite is absent.
    """


def spill_output(text: str, prefix: str) -> str | None:
    """Persist oversized tool output so truncation never loses evidence.

    "Re-run and redirect to a file" is not a real recovery path — the command
    may take minutes or not be idempotent. Saving what we already captured lets
    the model grep/read the full output directly. Returns the path, or None
    when the home is unwritable (truncation then falls back to re-run advice).
    """
    root = Path(os.path.expanduser("~/.raven/tool-output"))
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{prefix}-{uuid.uuid4().hex[:8]}.log"
        path.write_text(text, encoding="utf-8")
        return str(path)
    except OSError:
        return None


@dataclass
class ExecResult:
    """Result of a sandboxed command execution."""

    stdout: str
    stderr: str
    exit_code: int

    def as_text(self, max_chars: int = 10_000, spill_prefix: str | None = None) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr.strip():
            parts.append(f"STDERR:\n{self.stderr}")
        parts.append(f"\nExit code: {self.exit_code}")
        result = "\n".join(parts)
        if len(result) > max_chars:
            half = max_chars // 2
            total_lines = result.count("\n") + 1
            # Report the totals, not just the amount dropped: without them the
            # reader cannot tell whether ten lines went missing or ten thousand.
            # Naming the recovery path matters as much - otherwise the only way
            # to see the rest is to re-run the command blind.
            spilled = spill_output(result, spill_prefix) if spill_prefix else None
            if spilled:
                recovery = (
                    f"... [full output saved to {spilled}; search it with grep or "
                    "page through it with read_file offset/limit] ...\n\n"
                )
            else:
                recovery = (
                    "... [re-run redirecting to a file (`cmd > /tmp/out.log 2>&1`) and "
                    "page through it with read_file to see all of it] ...\n\n"
                )
            marker = (
                f"\n\n... ({len(result) - max_chars:,} chars truncated; full output "
                f"{len(result):,} chars / {total_lines:,} lines) ...\n" + recovery
            )
            result = result[:half] + marker + result[-half:]
        return result


@dataclass
class SessionOutput:
    """Incremental output pulled from a live ExecSession."""

    output: str
    running: bool
    exit_code: int | None = None

    def as_text(self, max_chars: int = 30_000, spill_prefix: str | None = None) -> str:
        body = self.output
        if len(body) > max_chars:
            half = max_chars // 2
            spilled = spill_output(body, spill_prefix) if spill_prefix else None
            saved = f" full output saved to {spilled}, grep/read_file it;" if spilled else ""
            body = (
                body[:half]
                + f"\n\n... ({len(self.output) - max_chars:,} chars, middle omitted;{saved}) ...\n\n"
                + body[-half:]
            )
        if self.running:
            return f"{body}\n[session still running - call exec_read again for more output]"
        return f"{body}\n[session ended, exit code: {self.exit_code}]"


class ExecSession(ABC):
    """A long-lived shell the agent can write to and read from incrementally.

    Exists so a single logical shell can span multiple tool calls: cwd, exported
    variables and background jobs survive between them, and interactive programs
    (ssh password prompts, REPLs, debuggers) can be driven turn by turn. A
    plain ``exec`` call cannot do either - it is one process per call.
    """

    @property
    @abstractmethod
    def running(self) -> bool:
        """True while the underlying shell process is alive."""

    @property
    @abstractmethod
    def exit_code(self) -> int | None:
        """Exit status once the shell has ended, else None."""

    @abstractmethod
    async def write(self, data: str) -> None:
        """Write raw bytes to the shell's stdin (caller supplies any newline)."""

    @abstractmethod
    async def read(self, timeout: float) -> SessionOutput:
        """Drain output produced since the previous read, waiting up to timeout."""

    @abstractmethod
    async def close(self) -> None:
        """Terminate the shell and release its file descriptors."""


class SandboxExecutor(ABC):
    """
    Abstraction for isolated command execution.

    Implementations: BoxliteExecutor (boxlite microVM), DirectExecutor (host fallback).
    ExecTool holds this interface and is unaware of the concrete backend.
    """

    @property
    def is_sandboxed(self) -> bool:
        """True if commands run inside an isolated environment (not the host process).

        ExecTool reads this flag to decide whether to apply the regex deny-list guard.
        DirectExecutor overrides this to False; all other implementations default to True.

        The base class intentionally defaults to True rather than False. A custom executor
        that forgets to override this property will skip the deny-list, which is the
        safe-failure direction — real isolation comes from the sandbox. The only class
        that must opt out is DirectExecutor (host execution), which explicitly returns False.
        """
        return True

    @property
    def supports_process_spawning(self) -> bool:
        """True if start_process() is implemented for long-running child processes.

        connect_mcp_servers() checks this flag for the stdio MCP branch instead of
        using isinstance() — keeps caller code decoupled from concrete executor types.
        DirectExecutor and the base class default to False.
        """
        return False

    @property
    def supports_background_jobs(self) -> bool:
        """True if start_background() is implemented.

        Distinct from supports_sessions: a session is a PTY-backed interactive shell
        scoped to the agent, a background job is a detached process expected to
        outlive it. A backend can reasonably offer one and not the other, and
        ExecTool falls back to a session when jobs are unavailable.
        """
        return False

    @property
    def supports_sessions(self) -> bool:
        """True if open_session() is implemented.

        ExecTool reads this to decide whether to offer session-backed execution;
        the base class defaults to False so a backend without session support
        degrades to one-shot exec rather than erroring.
        """
        return False

    @abstractmethod
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Execute a shell command, return stdout/stderr/exit_code."""

    async def open_session(
        self,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecSession:
        """Start a long-lived interactive shell.

        Only implemented by executors that override supports_sessions to True.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support interactive sessions. Use one-shot exec instead."
        )

    async def start_background(
        self,
        command: str,
        *,
        cwd: str,
        env: dict[str, str] | None,
        log_path: str,
        status_path: str,
    ) -> int:
        """Start a detached process and return its pid.

        Contract every implementation must honour, because the caller relies on all
        three to survive its own exit:

        * no controlling terminal (stdin from /dev/null, output redirected to
          ``log_path``) — otherwise closing the terminal SIGHUPs the process;
        * its own process session/group, so signalling the job never reaches the
          agent and vice versa;
        * the shell's exit status written to ``status_path`` when it ends, so the
          outcome is readable after the agent that started it is gone.

        Only implemented by executors that override supports_background_jobs.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support background jobs. Use exec(session=...) instead."
        )

    async def signal_background(self, pid: int, sig: int) -> None:
        """Send a signal to a background job's whole process group.

        The group, not the pid: a job is usually a shell that spawned the real
        workload, and signalling only the shell leaves the workload running.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support background jobs.")

    async def start_process(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> tuple[Any, Any]:
        """Start a long-running child process; return (read_stream, write_stream).

        Streams are anyio MemoryObjectReceiveStream / MemoryObjectSendStream,
        compatible with the MCP SDK's ClientSession.
        Only implemented by executors that override supports_process_spawning to True.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support process spawning. "
            "Stdio MCP servers cannot be sandboxed with this executor."
        )

    async def start(self) -> None:
        """Lifecycle: called once before first exec. Default: no-op."""

    async def stop(self) -> None:
        """Lifecycle: called on graceful shutdown. Default: no-op."""

    async def __aenter__(self) -> SandboxExecutor:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.stop()
