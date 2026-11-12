"""
Sandbox package — self-contained isolated command execution for Python agents.

Public API (import everything from here, not from sub-modules):
    SandboxInitError   — raised when a sandbox backend fails to start
    ExecResult         — result of a single exec() call
    SandboxExecutor    — ABC for executor implementations
    SandboxConfig      — Pydantic config model
    DirectExecutor     — host-process fallback (no isolation)
    build_executor()   — factory: SandboxConfig → SandboxExecutor
    sandbox_install_hint() — how to get the OpenShell SDK into this environment
"""

from __future__ import annotations

import importlib.metadata
import re
import shlex
import sys
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
    "sandbox_install_hint",
]


def sandbox_install_hint() -> str:
    """The advice that tells the user how to get the OpenShell SDK into this raven's environment.

    raven is not distributed under this name on package indexes, so naming the
    ``raven[sandbox]`` extra in a command would fetch an unrelated project.
    The hint therefore installs the pinned SDK requirement itself, into the
    interpreter raven is running under (a uv tool venv or a project venv).

    The SDK is not published for Windows, so there the answer is that the
    backend is unavailable rather than a command that cannot work.
    """
    if sys.platform == "win32":
        return "The OpenShell SDK is not published for Windows, so the OpenShell sandbox backend needs Linux or macOS."

    spec = "openshell"
    try:
        for requirement in importlib.metadata.requires("raven") or []:
            candidate = requirement.split(";", 1)[0].strip()
            if re.match(r"openshell\b", candidate):
                spec = _install_spec(requirement)
                break
    except importlib.metadata.PackageNotFoundError:
        pass
    return (
        "Install the OpenShell SDK into this raven's environment: "
        f"uv pip install --python {_quote(sys.executable)} {_quote(spec)}"
    )


def _quote(value: str) -> str:
    """Quote a path or a requirement for the shell that will read it."""
    if sys.platform == "win32":
        return f'"{value}"'
    return shlex.quote(value)


def _install_spec(requirement: str) -> str:
    """The requirement as a command that installs it names it.

    The marker of a requirement that lives in an extra carries two
    conditions: the platform the dependency is for, and the extra that pulls
    it in. The extra is a packaging context the command does not have -- it
    names the package itself -- so only the platform condition stays, and its
    quotes are doubled so the shell quoting around it stays readable.
    """
    name, _, marker = requirement.partition(";")
    if not marker:
        return name.strip()
    platform = re.sub(r"\s+and\s+extra\s*==\s*['\"][^'\"]*['\"]", "", marker)
    single, double = "'", '"'
    return f"{name.strip()}; {platform.strip().replace(single, double)}"


def build_executor(
    sandbox_cfg: SandboxConfig | None,
    workspace: Path,
    extra_volumes: tuple[tuple[str, str, str], ...] = (),
) -> SandboxExecutor:
    """Synchronously construct the executor for the given config.

    Object creation is always sync and cheap. Sandbox creation runs later
    inside executor.start() / __aenter__(), which raises SandboxInitError if
    the backend cannot be started.

    extra_volumes: additional (host_path, guest_path, mode) mounts appended to
    ``sandbox_cfg.extra_volumes``, e.g. keeping agent home reachable inside
    the sandbox when the mounted workspace does not already cover it.
    """
    backend = sandbox_cfg.backend if sandbox_cfg else "none"

    if backend == "none":
        global _warned_no_sandbox
        if not _warned_no_sandbox:
            _warned_no_sandbox = True
            logger.warning(
                "Sandbox backend is 'none' — agent commands run directly on the "
                "host with no isolation. Prompt-injected commands execute with "
                "full host privileges. Set tools.sandbox.backend to 'openshell' "
                "to contain them."
            )
        return DirectExecutor()

    if backend == "openshell":
        try:
            import openshell as _  # noqa: F401 — probe availability before constructing executor
        except ImportError as exc:
            raise SandboxInitError(f"No sandbox backend available: {exc}\n  • {sandbox_install_hint()}") from exc
        from raven.sandbox.openshell_executor import OpenShellExecutor

        return OpenShellExecutor(
            image=sandbox_cfg.image,
            workspace=workspace,
            cpus=sandbox_cfg.cpus,
            memory_mib=sandbox_cfg.memory_mib,
            allow_net=sandbox_cfg.allow_net,
            extra_volumes=[*sandbox_cfg.extra_volumes, *[list(v) for v in extra_volumes]],
            default_timeout=sandbox_cfg.default_timeout,
            verify_timeout=sandbox_cfg.verify_timeout,
            create_timeout=sandbox_cfg.create_timeout,
            gateway=sandbox_cfg.gateway,
        )

    raise SandboxInitError(f"Unknown sandbox backend: {backend!r}. Valid values: 'none', 'openshell'.")
