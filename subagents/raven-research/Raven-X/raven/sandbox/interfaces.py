"""Public interfaces for the sandbox package.

Import from here (or from raven.sandbox) — never from boxlite_executor.py
or direct_executor.py directly, so callers remain decoupled from concrete backends.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class SandboxInitError(RuntimeError):
    """Raised when the sandbox backend cannot be started or probed.

    Defined in interfaces.py (not in boxlite_executor.py) so it can be imported
    without requiring boxlite to be installed. mcp.py and loop.py import this
    type for error handling; they must not fail just because boxlite is absent.
    """


@dataclass
class ExecResult:
    """Result of a sandboxed command execution."""

    stdout: str
    stderr: str
    exit_code: int

    def as_text(self, max_chars: int = 10_000) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr.strip():
            parts.append(f"STDERR:\n{self.stderr}")
        parts.append(f"\nExit code: {self.exit_code}")
        result = "\n".join(parts)
        if len(result) > max_chars:
            half = max_chars // 2
            result = result[:half] + f"\n\n... ({len(result) - max_chars:,} chars truncated) ...\n\n" + result[-half:]
        return result


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

    @abstractmethod
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Execute a shell command, return stdout/stderr/exit_code."""

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
