"""DirectExecutor: runs commands directly on the host process (no isolation)."""

from __future__ import annotations

import asyncio
import os

from raven.sandbox.interfaces import ExecResult, SandboxExecutor

_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600

# DirectExecutor runs on the host with no isolation, so commands the agent is
# coaxed into running (via prompt injection) would otherwise inherit every host
# env var — including credentials. Pass only a minimal, non-sensitive baseline
# plus whatever the caller explicitly supplies.
_ENV_ALLOWLIST = (
    # Locale / shell basics
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "USER",
    "LOGNAME",
    "SHELL",
    "PWD",
    "TZ",
    "TMPDIR",
    # Language runtimes (so python / node / venv-based tools resolve correctly)
    "PYTHONPATH",
    "VIRTUAL_ENV",
    # TLS trust + proxy (so git / curl / https tools work behind corp setups).
    # These are config, not crown-jewel secrets (API keys / cloud creds / SSH
    # are deliberately NOT here).
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def _baseline_env() -> dict[str, str]:
    return {k: v for k in _ENV_ALLOWLIST if (v := os.environ.get(k)) is not None}


class DirectExecutor(SandboxExecutor):
    """No-op sandbox: runs commands directly on the host (current behavior)."""

    @property
    def is_sandboxed(self) -> bool:
        return False

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        effective_timeout = min(
            _DEFAULT_TIMEOUT if timeout is None else timeout,
            _MAX_TIMEOUT,
        )
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**_baseline_env(), **(env or {})},
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            return ExecResult(stdout="", stderr=f"Timed out after {effective_timeout}s", exit_code=-1)
        return ExecResult(
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            exit_code=process.returncode,
        )
