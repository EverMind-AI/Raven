"""DirectExecutor: runs commands directly on the host process (no isolation)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
from pathlib import Path

from raven.sandbox.interfaces import ExecResult, ExecSession, SandboxExecutor, SessionOutput

_DEFAULT_TIMEOUT = 60

# Models write bash (bashisms like [[ ]], source, arrays are routine), but
# create_subprocess_shell uses /bin/sh, which is dash on Debian-family systems
# and fails or silently diverges on them. Prefer bash when the host has it.
_SHELL_BIN = "/bin/bash" if os.path.exists("/bin/bash") else None

# Under a container memory limit the OOM killer picks the process with the
# highest badness score — often the agent itself, since tool workloads (pip,
# make -j) spread their memory across many small children while the agent is
# the single largest process. Raising the foreground command shell's
# oom_score_adj (inherited across fork/exec by its whole subtree) makes the
# kill land on the recoverable tool command instead of ending the run.
# Foreground only: sessions and background jobs may host deliverables (servers)
# that must not become preferred kill targets.
_OOM_CHILD_PREFIX = (
    "{ echo 500 > /proc/self/oom_score_adj; } 2>/dev/null; " if os.path.exists("/proc/self/oom_score_adj") else ""
)

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


class PtyExecSession(ExecSession):
    """Session backed by a shell attached to a pseudo-terminal.

    A pty rather than plain pipes because the programs sessions exist for -
    ssh password prompts, REPLs, pagers, curses UIs - check isatty() and change
    behaviour (or refuse to run) when stdin is not a terminal.
    """

    _READ_CHUNK = 65536
    # Once output starts flowing, keep draining until the stream goes quiet for
    # this long. Returning on the first chunk would routinely cut a line in half.
    _QUIET_PERIOD_S = 0.25

    def __init__(self, process: asyncio.subprocess.Process, master_fd: int) -> None:
        self._process = process
        self._master_fd = master_fd
        self._buffer = bytearray()
        self._data_ready = asyncio.Event()
        self._closed = False
        self._loop = asyncio.get_running_loop()
        os.set_blocking(master_fd, False)
        self._loop.add_reader(master_fd, self._on_readable)

    @property
    def running(self) -> bool:
        return not self._closed and self._process.returncode is None

    @property
    def exit_code(self) -> int | None:
        return self._process.returncode

    def _on_readable(self) -> None:
        try:
            chunk = os.read(self._master_fd, self._READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            chunk = b""
        if chunk:
            self._buffer.extend(chunk)
        else:
            self._stop_reading()
        self._data_ready.set()

    def _stop_reading(self) -> None:
        with contextlib.suppress(Exception):
            self._loop.remove_reader(self._master_fd)

    def _take(self) -> str:
        data = bytes(self._buffer)
        self._buffer.clear()
        self._data_ready.clear()
        return data.decode("utf-8", errors="replace")

    async def write(self, data: str) -> None:
        if self._closed:
            raise RuntimeError("session is closed")
        os.write(self._master_fd, data.encode("utf-8"))

    async def read(self, timeout: float) -> SessionOutput:
        deadline = self._loop.time() + max(timeout, 0.0)
        if not self._buffer:
            remaining = deadline - self._loop.time()
            if remaining > 0:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._data_ready.wait(), timeout=remaining)
        while self._loop.time() < deadline:
            self._data_ready.clear()
            try:
                await asyncio.wait_for(self._data_ready.wait(), timeout=self._QUIET_PERIOD_S)
            except asyncio.TimeoutError:
                break
        return SessionOutput(output=self._take(), running=self.running, exit_code=self.exit_code)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_reading()
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
        with contextlib.suppress(OSError):
            os.close(self._master_fd)


class DirectExecutor(SandboxExecutor):
    """No-op sandbox: runs commands directly on the host (current behavior)."""

    def __init__(self, inherit_env: bool = False):
        # Disposable environments (benchmark containers) opt in: the task
        # image's ENV (LD_LIBRARY_PATH, HF_*, JAVA_HOME, ...) is part of the
        # environment the workload needs, silently stripping it breaks tasks
        # in ways that look unrelated, and there are no host credentials to
        # protect. On a real host the allowlist stays the default.
        self._inherit_env = inherit_env

    def _env(self) -> dict[str, str]:
        if self._inherit_env:
            return dict(os.environ)
        return _baseline_env()

    @property
    def is_sandboxed(self) -> bool:
        return False

    _SESSION_INIT_DRAIN_S = 1.0

    @property
    def supports_sessions(self) -> bool:
        return os.name == "posix"

    async def open_session(
        self,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecSession:
        if not self.supports_sessions:
            return await super().open_session(cwd=cwd, env=env)
        import pty

        master_fd, slave_fd = pty.openpty()
        try:
            process = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-i",
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env={**self._env(), "TERM": "dumb", **(env or {})},
                start_new_session=True,
            )
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        session = PtyExecSession(process, master_fd)
        # Silence terminal echo and the interactive prompt, then swallow the
        # startup banner. Otherwise every read would return the command itself
        # and a shell prompt alongside the output the caller actually asked for.
        await session.write("stty -echo 2>/dev/null; PS1=''; unset PROMPT_COMMAND\n")
        await session.read(timeout=self._SESSION_INIT_DRAIN_S)
        return session

    @property
    def supports_background_jobs(self) -> bool:
        # POSIX only: the contract needs setsid semantics and process-group signals,
        # neither of which maps cleanly onto Windows.
        return os.name == "posix"

    async def start_background(
        self,
        command: str,
        *,
        cwd: str,
        env: dict[str, str] | None,
        log_path: str,
        status_path: str,
    ) -> int:
        # No PTY anywhere in here. A background job that owns a terminal dies of
        # SIGHUP the moment that terminal is closed, which is precisely what made
        # session-hosted "background" commands fail to outlive their agent.
        #
        # The status file is written by the wrapper rather than recorded from the
        # Process object, so the exit code survives this interpreter: whoever asks
        # later — a subsequent agent turn, a fresh process, an operator — reads the
        # same answer.
        # An EXIT trap rather than trailing lines: a command ending in an explicit
        # `exit` never reaches a line appended after it, and servers routinely do.
        # The signal traps re-raise as 128+signo so a cancelled job is distinguishable
        # from one that finished cleanly - without them bash runs the EXIT trap with
        # $? still 0 and a killed server reports success.
        pid_path = f"{status_path}.pid"
        wrapper = (
            f"__raven_status={shlex.quote(status_path)}\n"
            f'printf %s "$$" > {shlex.quote(pid_path)}\n'
            'trap \'printf %s "$?" > "$__raven_status"\' EXIT\n'
            "trap 'exit 143' TERM\n"
            "trap 'exit 130' INT\n"
            "trap 'exit 129' HUP\n"
            f"{command}\n"
        )
        # Launch through a short-lived shell that backgrounds the job under `setsid`,
        # rather than spawning the job as our own child. A direct child would stay
        # tied to this process's asyncio child watcher for its whole life - the exact
        # kind of hidden coupling to the agent that a detached job exists to avoid.
        # The launcher exits immediately and is awaited here, so nothing is left to
        # reap; the job itself is reparented to init.
        launcher = f"setsid /bin/bash -c {shlex.quote(wrapper)} < /dev/null >> {shlex.quote(log_path)} 2>&1 &\n"
        process = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-c",
            launcher,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**self._env(), **(env or {})},
            start_new_session=True,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"could not launch background job: {stderr.decode('utf-8', 'replace').strip()}")
        # The job reports its own pid; $! would name the launcher's child, which is
        # not necessarily the session leader once setsid is in the middle.
        for _ in range(200):
            try:
                return int(Path(pid_path).read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                await asyncio.sleep(0.01)
        raise RuntimeError("background job did not report its pid within 2s")

    async def signal_background(self, pid: int, sig: int) -> None:
        try:
            # Negative pid targets the whole group. start_background made the job a
            # session leader, so its pgid equals its pid, and the real workload it
            # spawned is inside that group.
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)

    @staticmethod
    async def _drain(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
        if stream is None:
            return
        with contextlib.suppress(Exception):
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                buf.extend(chunk)

    @staticmethod
    async def _wait_returncode(process: asyncio.subprocess.Process, timeout: float) -> bool:
        """True once the process has exited, False when the timeout elapses first.

        Not Process.wait(): that also waits for the stdout/stderr pipes to hit
        EOF, so a `cmd &` grandchild inheriting them would stall the call until
        *its* exit even though the shell itself is long gone.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while process.returncode is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.02, remaining))
        return True

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        # No ceiling here: ExecTool owns the per-command clamp. A second clamp
        # in the executor silently defeated configured ceilings above 600s.
        effective_timeout = _DEFAULT_TIMEOUT if timeout is None else timeout
        spawn_env = {**self._env(), **(env or {})}
        command = _OOM_CHILD_PREFIX + command
        if _SHELL_BIN:
            process = await asyncio.create_subprocess_exec(
                _SHELL_BIN,
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=spawn_env,
            )
        else:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=spawn_env,
            )
        # Drain the pipes continuously instead of communicate(): on timeout the
        # output captured so far survives (a killed 600s build must not report
        # zero bytes), and a `cmd &` grandchild holding the pipe open no longer
        # blocks the call until its own exit.
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        drains = (
            asyncio.ensure_future(self._drain(process.stdout, stdout_buf)),
            asyncio.ensure_future(self._drain(process.stderr, stderr_buf)),
        )
        timed_out = not await self._wait_returncode(process, effective_timeout)
        if timed_out:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await self._wait_returncode(process, 5.0)
        # Bounded: a surviving grandchild keeping the pipe open must not wedge
        # the call after the shell itself has exited.
        _, pending = await asyncio.wait(drains, timeout=2.0)
        if pending:
            for task in pending:
                task.cancel()
            # A grandchild still holds the pipes; close them so the transport
            # neither buffers its output forever nor leaks the descriptors.
            with contextlib.suppress(Exception):
                process._transport.close()  # type: ignore[attr-defined]  # noqa: SLF001
        stderr_text = stderr_buf.decode("utf-8", errors="replace")
        if timed_out:
            stderr_text = (
                f"{stderr_text}\nTimed out after {effective_timeout}s"
                if stderr_text
                else (f"Timed out after {effective_timeout}s")
            )
        return ExecResult(
            stdout=stdout_buf.decode("utf-8", errors="replace"),
            stderr=stderr_text,
            exit_code=-1 if timed_out else process.returncode,
            timed_out=timed_out,
        )
