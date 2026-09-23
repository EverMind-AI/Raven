"""DirectExecutor: runs commands directly on the host process (no isolation)."""

from __future__ import annotations

import asyncio
import os
import signal

from raven.sandbox.interfaces import ExecResult, SandboxExecutor
from raven.utils import fonts

_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600
_READ_CHUNK = 65536
# The StreamReader limit ``create_subprocess_shell`` applies when none is given.
_STREAM_LIMIT = 2**16
# How long a finished command's pipes get to reach EOF before the call returns
# without them. Ordinary commands close both within a loop iteration or two of
# the exit notice, so this is headroom for scheduling, not a wait anyone
# expects to use; a command that leaves a process behind pays it in full, and
# 60 s was the price before. Bounded by the caller's deadline in ``exec``.
_DETACHED_PIPE_GRACE = 1.0

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
    # Which fonts a child can draw with. Paths, not secrets, and a host that set
    # them chose its fonts on purpose -- dropping them would quietly give every
    # renderer the agent starts a different set from the one the owner's own
    # terminal has.
    "FONTCONFIG_FILE",
    "FONTCONFIG_PATH",
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
    env = {k: v for k in _ENV_ALLOWLIST if (v := os.environ.get(k)) is not None}
    # The fonts a renderer the agent starts can reach. The model checks a deck by
    # running LibreOffice itself -- `soffice --convert-to pdf`, or a render.sh it
    # wrote that does -- and on a Mac the converter's bundled fontconfig starts
    # with no configuration, so without this every Chinese page it looked at came
    # back blank and was approved unread. Measured 2026-09-23 (macOS 15.7,
    # LibreOffice 26.8, no CJK face in the app bundle): the same command drew no
    # Han without it and every row with it. Unchanged where the host's
    # fontconfig already reads the system's fonts, which is everywhere else.
    return fonts.render_env(env)


class _ExitNotifyingProtocol(asyncio.subprocess.SubprocessStreamProtocol):
    """The protocol ``create_subprocess_shell`` builds, plus an exit signal
    that does not wait for the pipes.

    ``Process.wait()`` cannot be that signal. asyncio wakes its waiters from
    ``_call_connection_lost``, and ``_try_finish`` schedules that only once
    every pipe has disconnected (CPython 3.12 ``base_subprocess.py``). A
    command that leaves a background child behind -- ``server & curl ...`` --
    exits at once while the child keeps the inherited stdout open, so
    ``wait()`` hangs for the child's whole life and the caller's timeout fires
    on a command that finished in seconds. ``process_exited`` runs as soon as
    the child watcher reports the shell, after the return code is recorded,
    and that is the moment ``exec`` waits on.
    """

    def __init__(self, exited: asyncio.Event, *, limit: int, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(limit=limit, loop=loop)
        self._exited = exited

    def process_exited(self) -> None:
        super().process_exited()
        self._exited.set()


class _PipeDrain:
    """Reads one pipe to EOF, keeping what arrives until ``release`` is called.

    The read end stays open on purpose after ``exec`` has returned. Closing it
    would hand whatever still holds the write end -- the server the command
    just started -- EPIPE or SIGPIPE on its next log line, killing it silently
    where the timeout at least killed it loudly. Leaving it unread is no
    better: once the kernel pipe buffer and the reader's own buffer are full,
    that process blocks on write. So a released drain keeps reading and drops
    what it reads, at the cost of one pipe fd and one idle task for as long as
    that process lives; both go away with its EOF.
    """

    def __init__(self, stream: asyncio.StreamReader | None, name: str, transport: asyncio.SubprocessTransport) -> None:
        self.data = bytearray()
        self._keep = True
        self._transport = transport
        self.task = asyncio.get_running_loop().create_task(self._run(stream), name=name)

    async def _run(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        try:
            while chunk := await stream.read(_READ_CHUNK):
                if self._keep:
                    self.data.extend(chunk)
        except asyncio.CancelledError:
            # Nothing in this module cancels a drain; the loop's shutdown does
            # (``asyncio.run`` cancels every pending task before closing). The
            # read end goes with it: left open, the transport closes itself
            # from ``__del__`` once the loop is gone and prints "Event loop is
            # closed" on the way out of the process.
            self._transport.close()
            raise

    def release(self) -> None:
        self._keep = False


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

    @staticmethod
    async def _spawn(
        command: str, cwd: str | None, env: dict[str, str]
    ) -> tuple[asyncio.subprocess.Process, asyncio.Event, asyncio.SubprocessTransport]:
        """``create_subprocess_shell`` with the exit event wired in; see ``_ExitNotifyingProtocol``."""
        loop = asyncio.get_running_loop()
        exited = asyncio.Event()
        transport, protocol = await loop.subprocess_shell(
            lambda: _ExitNotifyingProtocol(exited, limit=_STREAM_LIMIT, loop=loop),
            command,
            # Pointed at /dev/null rather than inherited, so a read returns
            # EOF at once -- an open read-only fd, not a closed one, and the
            # same thing the background executor passes. Inherited, the command
            # shares this process's stdin -- and when raven runs as an ACP
            # sub-agent that is the pipe the client answers permission
            # requests on. A command that
            # reads its stdin (ssh without -n, cat, python3 -) then consumes the
            # frames arriving while it runs, and every other session sharing the
            # process waits out the 300 s approval deadline on an answer that was
            # written and eaten. Measured 2026-09-15: four sessions, one process,
            # 18 of 183 approvals lost, each inside another session's ssh.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            # Own session, so the shell's pid doubles as its group's pgid and a
            # cancelled turn can kill the whole tree instead of only the shell.
            start_new_session=True,
        )
        return asyncio.subprocess.Process(transport, protocol, loop), exited, transport

    @staticmethod
    async def _settle(drains: tuple[_PipeDrain, ...], grace: float) -> None:
        """Give the pipes ``grace`` seconds to reach EOF, then stop keeping what they carry.

        A command whose children have all exited closes both pipes within a
        loop iteration or two of its exit notice; the grace only absorbs the
        ordering between the child watcher's callback and the selector's.
        Pipes still open past it belong to a process the shell did not wait
        for, and what that process writes from here on is nobody's output.
        """
        await asyncio.wait([drain.task for drain in drains], timeout=grace)
        for drain in drains:
            drain.release()
            if drain.task.done() and not drain.task.cancelled() and (exc := drain.task.exception()) is not None:
                raise exc

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
        process, exited, transport = await self._spawn(command, cwd, {**_baseline_env(), **(env or {})})
        # Read before the first await: this is the last point where the pid is
        # guaranteed to still belong to the shell we just spawned.
        pgid = process.pid
        deadline = asyncio.get_running_loop().time() + effective_timeout

        # Drained into buffers this frame owns rather than through
        # ``communicate()``: a timeout cancels whatever is being awaited, and
        # communicate's reads take the output already produced down with them.
        # The command that outruns the clock is usually the last of several
        # joined by ``;``, so that output is the only account of the steps
        # that did finish -- discarding it spends the whole timeout to say
        # nothing and leaves the caller no choice but to run them again.
        drains = (
            _PipeDrain(process.stdout, "exec-stdout", transport),
            _PipeDrain(process.stderr, "exec-stderr", transport),
        )

        try:
            await asyncio.wait_for(exited.wait(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            self._kill_process_group(process, pgid)
            try:
                await asyncio.wait_for(exited.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            await self._settle(drains, _DETACHED_PIPE_GRACE)
            # Note first, partial stderr under it: ExecResult.as_text renders
            # this as "STDERR:\n<stderr>", and oncall-flow's cap-kill hook
            # anchors on "Timed out after" sitting directly after that header.
            # A capped command has usually written something by then, so
            # appending the note instead would hide the kill from the one
            # reader that routes it away from a retry.
            note = f"Timed out after {effective_timeout}s"
            partial_err = drains[1].data.decode("utf-8", errors="replace")
            return ExecResult(
                stdout=drains[0].data.decode("utf-8", errors="replace"),
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
            for drain in drains:
                drain.release()
            try:
                await asyncio.wait_for(exited.wait(), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # A second cancellation interrupts the reap. The SIGKILL above
                # has already landed either way, so the corpse is left to
                # asyncio's child watcher rather than held onto here.
                pass
            raise
        remaining = max(deadline - asyncio.get_running_loop().time(), 0.0)
        await self._settle(drains, min(_DETACHED_PIPE_GRACE, remaining))
        return ExecResult(
            stdout=drains[0].data.decode("utf-8", errors="replace"),
            stderr=drains[1].data.decode("utf-8", errors="replace"),
            exit_code=process.returncode,
        )
