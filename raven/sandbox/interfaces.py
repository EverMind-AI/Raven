"""Public interfaces for the sandbox package.

Import from here (or from raven.sandbox) — never from openshell_executor.py
or direct_executor.py directly, so callers remain decoupled from concrete backends.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from raven.sandbox.denials import NetworkDenial

if TYPE_CHECKING:
    from raven.sandbox.network_policy import NetworkGrant


class SandboxInitError(RuntimeError):
    """Raised when the sandbox backend cannot be started or probed.

    Defined here rather than in ``openshell_executor`` so it can be imported
    without the OpenShell SDK installed: the agent loop and the MCP client import this
    type for error handling and must not fail because a backend is absent.
    """


@dataclass
class ExecResult:
    """Result of a sandboxed command execution."""

    stdout: str
    stderr: str
    exit_code: int
    # Connections the sandbox's network policy refused while the command ran;
    # empty for an executor that enforces no network policy.
    denials: tuple[NetworkDenial, ...] = ()

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

    Implementations: OpenShellExecutor (OpenShell sandbox), DirectExecutor (host fallback).
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

        connect_mcp_server() checks this flag for the stdio MCP branch instead of
        using isinstance() -- keeps caller code decoupled from concrete executor types.
        DirectExecutor and the base class default to False.
        """
        return False

    @property
    def supports_network_grants(self) -> bool:
        """True if a running executor can open a blocked host (``grant_network``)."""
        return False

    async def grant_network(self, grants: Sequence[NetworkGrant]) -> bool:
        """Open ``grants`` in this executor only; False where there is no network policy to change."""
        return False

    async def sync_network(self) -> bool:
        """Bring a running executor's network policy up to date; False where there is none."""
        return False

    async def denials_since(self, since: float, *, wait: bool = False) -> list[NetworkDenial]:
        """Connections the sandbox refused since ``since`` (epoch seconds), whoever made them.

        For a process that outlives any one command (a stdio MCP server), whose
        refusals no ``exec`` result carries. ``wait`` gives a refusal a moment
        to arrive. None for an executor that enforces no network policy.
        """
        return []

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
        cwd: str | None = None,
    ) -> tuple[Any, Any]:
        """Start a long-running child process; return (read_stream, write_stream).

        Streams are anyio MemoryObjectReceiveStream / MemoryObjectSendStream,
        compatible with the MCP SDK's ClientSession.

        ``command``, ``args``, ``env`` values and ``cwd`` are host paths. The
        executor owns their mapping to wherever the process actually runs,
        because only it knows which host directories it mounted; a caller
        hands them over exactly as the server's configuration spells them.
        ``cwd`` is where the process runs. An MCP server contributed by an
        Agent Plugin package always names one -- the package root when the
        package's ``mcp.json`` omits it -- so an executor that cannot honour
        it must refuse rather than run the server somewhere else.

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
