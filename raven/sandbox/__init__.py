"""
Sandbox package — self-contained isolated command execution for Python agents.

Public API (import everything from here, not from sub-modules):
    SandboxInitError   — raised when a sandbox backend fails to start
    ExecResult         — result of a single exec() call
    SandboxExecutor    — ABC for executor implementations
    SandboxConfig      — Pydantic config model
    DirectExecutor     — host-process fallback (no isolation)
    build_executor()   — factory: SandboxConfig → SandboxExecutor
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger

from raven.sandbox.config import SandboxConfig
from raven.sandbox.direct_executor import DirectExecutor
from raven.sandbox.interfaces import ExecResult, SandboxExecutor, SandboxInitError

# Warn once per process: many executors (AgentLoop + each subagent) are built
# over a process lifetime, but the "no sandbox" caveat only needs saying once.
_warned_no_sandbox = False

__all__ = [
    "ExecResult",
    "SandboxExecutor",
    "SandboxInitError",
    "SandboxConfig",
    "DirectExecutor",
    "build_executor",
]


def build_executor(
    sandbox_cfg: SandboxConfig | None,
    workspace: Path,
    owned_ids: set[str] | None = None,
) -> SandboxExecutor:
    """Synchronously construct the executor for the given config.

    Object creation is always sync and cheap. Probe / VM initialisation runs
    later inside executor.start() / __aenter__(). Raises SandboxInitError
    (propagated from start()) if the backend cannot be started.

    owned_ids: optional shared set that BoxliteExecutor populates with its VM
    ID on start and removes on stop. Used by SandboxDebugServer to distinguish
    VMs owned by this process from those of other processes.
    """
    backend = sandbox_cfg.backend if sandbox_cfg else "none"

    if backend == "none":
        global _warned_no_sandbox
        if not _warned_no_sandbox:
            _warned_no_sandbox = True
            logger.warning(
                "Sandbox backend is 'none' — agent commands run directly on the "
                "host with no isolation. Prompt-injected commands execute with "
                "full host privileges. Set tools.sandbox.backend to 'auto' or "
                "'boxlite' to contain them."
            )
        return DirectExecutor()

    if backend in ("auto", "boxlite"):
        try:
            import boxlite as _  # noqa: F401 — probe availability before constructing executor
            from raven.sandbox.boxlite_executor import BoxliteExecutor
        except ImportError as exc:
            raise SandboxInitError(
                f"No sandbox backend available: {exc}\n"
                "  • Install: pip install raven[sandbox]"
            ) from exc
        return BoxliteExecutor(
            image=sandbox_cfg.image,
            workspace=workspace,
            cpus=sandbox_cfg.cpus,
            memory_mib=sandbox_cfg.memory_mib,
            disk_size_gb=sandbox_cfg.disk_size_gb,
            allow_net=sandbox_cfg.allow_net,
            extra_volumes=sandbox_cfg.extra_volumes,
            default_timeout=sandbox_cfg.default_timeout,
            verify_timeout=sandbox_cfg.verify_timeout,
            create_timeout=sandbox_cfg.create_timeout,
            owned_ids=owned_ids,
        )

    raise SandboxInitError(
        f"Unknown sandbox backend: {backend!r}. "
        f"Valid values: 'none', 'auto', 'boxlite'."
    )
