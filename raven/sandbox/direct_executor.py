"""DirectExecutor: runs commands directly on the host process (no isolation)."""

from __future__ import annotations

import asyncio
import os
import signal

from raven.sandbox.interfaces import ExecResult, SandboxExecutor

_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600
_READ_CHUNK = 65536

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
    # This instance's own identity: which home a `raven ...` child resolves its
    # config, registry and stores against. Paths, not secrets. Measured
    # 2026-08-31 on a cold-start test: the loop ran `raven ops connection add`
    # from a tool call, this filter stripped RAVEN_HOME, and the row landed in
    # the default home rather than the one this instance and its sub-agents
    # read -- so the machine the owner had just registered stayed invisible,
    # and the loop offered to ssh in by hand.
    "RAVEN_HOME",
    "RAVEN_CONNECTIONS",
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
    # Windows OS basics: absent on POSIX (filtered out by _baseline_env), but
    # required on Windows for cmd.exe/PowerShell and any spawned tool to
    # resolve temp dirs, the user profile, and system DLLs. Omitting these
    # leaves the child with no SystemRoot/TEMP/etc. (temp files land in cwd,
    # SSL/winsock/.NET tools fail). None are crown-jewel secrets.
    "SystemRoot",
    "SystemDrive",
    "windir",
    "COMSPEC",
    "ComSpec",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "USERNAME",
    "USERDOMAIN",
)


def _baseline_env() -> dict[str, str]:
    return {k: v for k in _ENV_ALLOWLIST if (v := os.environ.get(k)) is not None}


class DirectExecutor(SandboxExecutor):
    """No-op sandbox: runs commands directly on the host (current behavior)."""

    @property
    def is_sandboxed(self) -> bool:
        return False

    @staticmethod
    def _kill_process_group(process: asyncio.subprocess.Process, pgid: int) -> None:
        """SIGKILL the whole group, not just the shell.

        ``create_subprocess_shell`` runs ``sh -c <command>``, and the shell is
        rarely the process doing the work: ``sh -c "npm test"`` leaves node as a
        child, so ``process.kill()`` reaps the shell and lets the child keep
        running, keep the pipes open, and keep writing to the workspace.

        ``pgid`` must be the value captured at spawn time rather than
        ``os.getpgid(process.pid)`` read now: the shell may already have exited
        and had its pid recycled, and signalling a recycled pid would kill an
        unrelated group. Same reasoning as
        ``CliAgentBackend._kill_process_group``.
        """
        if not hasattr(os, "killpg"):
            # Windows has neither killpg nor SIGKILL, and ignores
            # ``start_new_session``, so the single-process kill is the whole of
            # what the platform offers.
            #
            # Guarded for the same reason as the POSIX branch below, against a
            # narrower window: on win32 ``Process.kill()`` reaches
            # ``BaseSubprocessTransport._check_proc``, which raises
            # ``ProcessLookupError`` once ``_proc`` has been cleared -- and it is
            # cleared by ``_call_connection_lost``, the same callback that wakes
            # ``wait()``. A cancellation arriving in the loop iteration after the
            # process finished, but before the reap below resumed, would let
            # that error replace the ``CancelledError`` on the way out; the
            # caller's ``except Exception`` would then report a failed tool call
            # for a turn that was cancelled, which is the confusion this change
            # exists to remove.
            try:
                process.kill()
            except ProcessLookupError:
                pass
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

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
            # Own session, so the shell's pid doubles as its group's pgid and a
            # cancelled turn can kill the whole tree instead of only the shell.
            start_new_session=True,
        )
        # Read before the first await: this is the last point where the pid is
        # guaranteed to still belong to the shell we just spawned.
        pgid = process.pid

        # Drained into buffers this frame owns rather than through
        # ``communicate()``: a timeout cancels whatever is being awaited, and
        # communicate's reads take the output already produced down with them.
        # The command that outruns the clock is usually the last of several
        # joined by ``;``, so that output is the only account of the steps
        # that did finish -- discarding it spends the whole timeout to say
        # nothing and leaves the caller no choice but to run them again.
        stdout_buf = bytearray()
        stderr_buf = bytearray()

        async def _drain(stream: asyncio.StreamReader | None, into: bytearray) -> None:
            if stream is None:
                return
            while chunk := await stream.read(_READ_CHUNK):
                into.extend(chunk)

        async def _collect() -> None:
            await asyncio.gather(
                _drain(process.stdout, stdout_buf),
                _drain(process.stderr, stderr_buf),
            )
            # Reaped inside the deadline on purpose: a command may close both
            # pipes and keep running, and waiting for it after the timeout
            # window would hang for longer than the caller asked for.
            await process.wait()

        try:
            await asyncio.wait_for(_collect(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            self._kill_process_group(process, pgid)
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            # Note first, partial stderr under it: ExecResult.as_text renders
            # this as "STDERR:\n<stderr>", and oncall-flow's cap-kill hook
            # anchors on "Timed out after" sitting directly after that header.
            # A capped command has usually written something by then, so
            # appending the note instead would hide the kill from the one
            # reader that routes it away from a retry.
            note = f"Timed out after {effective_timeout}s"
            partial_err = stderr_buf.decode("utf-8", errors="replace")
            return ExecResult(
                stdout=stdout_buf.decode("utf-8", errors="replace"),
                stderr=f"{note}\n{partial_err}" if partial_err else note,
                exit_code=-1,
            )
        except asyncio.CancelledError:
            # Without this branch a cancelled turn drops the process object and
            # the command survives for the life of the Raven process:
            # ``CancelledError`` derives from ``BaseException``, so neither
            # ``ExecTool.execute``'s nor ``ToolRegistry.execute``'s
            # ``except Exception`` ever sees it.
            self._kill_process_group(process, pgid)
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # A second cancellation interrupts the reap. The SIGKILL above
                # has already landed either way, so the corpse is left to
                # asyncio's child watcher rather than held onto here.
                pass
            raise
        return ExecResult(
            stdout=stdout_buf.decode("utf-8", errors="replace"),
            stderr=stderr_buf.decode("utf-8", errors="replace"),
            exit_code=process.returncode,
        )
