"""Raven agents started by forking a process that has already loaded their code.

A Raven agent is a whole Raven: launched fresh, each one imports the same few
thousand modules and holds its own copy of them, about 120 MiB before it has
done anything. On Linux a process that has imported them once can ``fork``, and
the children share those pages with it until they write to them. So the host
keeps one such process -- the template -- and asks it for each Raven agent it
launches; measured on the product agents, what each one costs on its own falls
by about two thirds.

This module is the host's side: when to ask, starting and ending the template,
and the endpoint a forked agent runs behind. The template's own side -- what it
loads, the fork and the agent it becomes -- is a launcher and lives beside the
CLI (``raven.cli._fork_template``). This shelf starts it through Raven's entry
point, as ``python -m raven --fork-template``, and never imports it.

**The template** is started through the same parent-death wrapper as any agent
(``leftovers``), so it ends with the host, and its stdin is a lifeline only the
host holds: letting go of it asks the template to stop. It belongs to the event
loop it was started on -- its pipes and the watcher that reaps it are that
loop's -- so it is ended with that loop (``ForkTemplate._hold``), and a host on a
later loop starts another. A loop running beside it in another thread launches
the ordinary way rather than end the agents that loop still has.

**A fork.** The host hands over the agent's argv, environment and working
directory, and the three pipe ends that become its stdin, stdout and stderr.
The child leads a session of its own, so the host ends it with ``killpg`` as it
would any agent, and the template reports its exit status over the request's
connection, which stays open for that.

**What is eligible.** Only an agent this interpreter would run as Raven: the
host's own interpreter starting ``-m raven`` or a product launcher (a ``.py``
beside a ``subagent.json``). Anything else, any failure to start the template
or to fork, and every system but Linux, launches the ordinary way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: How long after starting an agent writes the modules it has imported. Long
#: enough to cover its first session and turn, where nearly all of them load.
LEARN_AFTER_S = 30.0
_LEARNED_FILE = "modules.json"

#: How the template process is started: through Raven's entry point, which settles
#: the certificate store before anything is imported and hands this argument to the
#: launcher beside the CLI. That launcher imports the surfaces agents import, by
#: name, which this shelf may not.
TEMPLATE_ARG = "--fork-template"
_TEMPLATE_PROGRAM: tuple[str, ...] = ("-m", "raven", TEMPLATE_ARG)

_READY_TIMEOUT_S = 60.0
_REQUEST_TIMEOUT_S = 30.0
_GRACE_POLL_S = 0.02
_TEARDOWN_GRACE_S = 1.0
_READER_LIMIT = 8 * 1024 * 1024


def raven_agent_argv(argv: Sequence[str]) -> bool:
    """Whether ``argv`` starts a Raven agent on this very interpreter.

    The interpreter is compared by path, not by file: two virtual environments
    link the same base interpreter, and an agent installed in another one must
    run with its own packages, not with the ones this process has loaded.
    """
    if len(argv) < 2:
        return False
    try:
        same = os.path.normcase(os.path.abspath(argv[0])) == os.path.normcase(os.path.abspath(sys.executable))
    except (TypeError, ValueError):
        return False
    if not same:
        return False
    if argv[1] == "-m":
        return len(argv) >= 3 and argv[2] == "raven"
    script = Path(argv[1])
    try:
        return script.suffix == ".py" and (script.parent / "subagent.json").is_file()
    except OSError:
        return False


# ---- the host's side ---------------------------------------------------------


class ForkedEndpoint:
    """An agent the template forked: its pipes, its group, and the exit status the template reports.

    The same surface as a launched child's endpoint (``client.Endpoint``). The
    agent is not this process's child, so its status cannot be waited for here;
    the template reaps it and says how it ended on the connection the request
    came in on.
    """

    def __init__(
        self,
        pid: int,
        *,
        stdin: asyncio.StreamWriter,
        stdout: asyncio.StreamReader,
        stderr: asyncio.StreamReader,
        control: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        template: ForkTemplate,
    ) -> None:
        self.pid = pid
        self.grace: float | None = None
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr
        self._control_reader, self._control_writer = control
        self._template = template
        self._exited: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._watch = asyncio.get_running_loop().create_task(self._await_exit(), name=f"forked-agent-{pid}")

    async def _await_exit(self) -> None:
        from raven.providers import relay_wire

        code = -signal.SIGKILL
        try:
            frame = await relay_wire.read_frame(self._control_reader)
            if isinstance(frame, dict) and isinstance(frame.get("exit"), int):
                code = frame["exit"]
        except (ConnectionError, relay_wire.FrameError, asyncio.IncompleteReadError):
            pass
        finally:
            # No report means the template went, and the parent-death signal
            # it armed took the agent with it.
            if not self._exited.done():
                self._exited.set_result(code)
            self._template.forget(self)

    @property
    def returncode(self) -> int | None:
        return self._exited.result() if self._exited.done() else None

    def describe(self) -> str:
        return f"pid {self.pid} (forked)"

    async def write(self, data: bytes) -> None:
        if self._stdin.is_closing():
            raise BrokenPipeError("stdin is closed")
        self._stdin.write(data)
        await self._stdin.drain()

    async def readline(self) -> bytes:
        return await self._stdout.readline()

    async def readerr(self) -> bytes:
        return await self._stderr.readline()

    async def end(self) -> None:
        try:
            if self.returncode is None and not self._stdin.is_closing():
                self._stdin.close()
                loop = asyncio.get_running_loop()
                deadline = loop.time() + (_TEARDOWN_GRACE_S if self.grace is None else self.grace)
                while self.returncode is None and loop.time() < deadline:
                    await asyncio.sleep(_GRACE_POLL_S)
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.pid, signal.SIGKILL)

    async def wait(self) -> int | None:
        return await asyncio.shield(self._exited)

    def ended(self) -> str | None:
        return None

    def release(self) -> None:
        from raven.acp_client import leftovers

        for stream in (self._stdin, self._control_writer):
            with contextlib.suppress(Exception):
                stream.close()
        for reader in (self._stdout, self._stderr):
            transport = getattr(reader, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
        self._watch.cancel()
        leftovers.forget(self.pid)


class ForkTemplate:
    """The host's handle on one template process: starts it, asks it for agents, closes it when idle."""

    def __init__(self, *, skip: Sequence[str] = ()) -> None:
        self._skip = tuple(skip)
        self._proc: asyncio.subprocess.Process | None = None
        self._dir: Path | None = None
        self._socket = ""
        self._children: set[ForkedEndpoint] = set()
        self._forking = 0
        self._idle_task: asyncio.Task | None = None
        self._log_task: asyncio.Task | None = None
        self._hold_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.loop = asyncio.get_running_loop()
        """The loop its process and pipes were opened under; the handle is no use to another."""
        self.broken = ""
        """Why this template will not be used again, once it has failed."""

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def children(self) -> int:
        return len(self._children)

    def forget(self, child: ForkedEndpoint) -> None:
        self._children.discard(child)
        if not self._children:
            from raven.config.live import default_live, subagent_runtime

            budget = subagent_runtime(default_live()).idle_close_s
            self.close_when_idle(budget if budget is not None and budget > 0 else None)

    async def _start(self, env: dict[str, str]) -> None:
        from raven.acp_client import leftovers
        from raven.config.paths import get_runtime_subdir

        self._dir = Path(tempfile.mkdtemp(prefix="raven-fork-template-"))
        self._socket = str(self._dir / "template.sock")
        learned = get_runtime_subdir("acp-template") / _LEARNED_FILE
        argv = [sys.executable, "-P", *_TEMPLATE_PROGRAM, "--socket", self._socket, "--learned", str(learned)]
        argv += ["--learn-after", str(LEARN_AFTER_S)]
        for prefix in self._skip:
            argv += ["--skip", prefix]
        argv = leftovers.bind_to_host(argv, env, str(self._dir))
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._dir),
            env=env,
            start_new_session=True,
        )
        leftovers.record(self._proc.pid, "fork-template")
        self._log_task = asyncio.get_running_loop().create_task(self._log_stderr(self._proc), name="fork-template-log")
        self._hold_task = asyncio.get_running_loop().create_task(self._hold(self._proc), name="fork-template-hold")
        ready = await asyncio.wait_for(self._ready_report(self._proc), timeout=_READY_TIMEOUT_S)
        if not ready.get("ready"):
            raise RuntimeError(str(ready.get("reason") or "the template exited before it was ready"))
        from loguru import logger

        logger.info(
            "fork template: ready (pid {}, {} modules, {:.1f}s)",
            self._proc.pid,
            ready.get("modules"),
            ready.get("seconds", 0.0),
        )

    @staticmethod
    async def _ready_report(proc: asyncio.subprocess.Process) -> dict[str, Any]:
        """The template's one line saying whether it is ready; ``{}`` if it ended without one."""
        if proc.stdout is None:
            return {}
        while line := await proc.stdout.readline():
            try:
                report = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(report, dict) and "ready" in report:
                return report
        return {}

    @staticmethod
    async def _log_stderr(proc: asyncio.subprocess.Process) -> None:
        from loguru import logger

        if proc.stderr is None:
            return
        while line := await proc.stderr.readline():
            logger.debug("fork template: {}", line.decode("utf-8", "replace").rstrip())

    async def spawn(
        self, argv: list[str], env: dict[str, str], cwd: str | None, base_env: dict[str, str]
    ) -> ForkedEndpoint:
        """An agent forked from the template, starting the template first if needed. Raises when it cannot."""
        if self.broken:
            raise RuntimeError(self.broken)
        async with self._lock:
            if not self.running:
                if self._proc is not None:
                    # A template that ended on its own: clear what it left before the next.
                    await self.close()
                try:
                    await self._start(base_env)
                except BaseException as exc:
                    self.broken = f"the fork template did not start: {exc}"
                    await self.close()
                    raise RuntimeError(self.broken) from exc
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
            self._forking += 1
        try:
            child = await self._fork(argv, env, cwd)
        finally:
            self._forking -= 1
        self._children.add(child)
        return child

    async def _fork(self, argv: list[str], env: dict[str, str], cwd: str | None) -> ForkedEndpoint:
        from raven.acp_client import leftovers
        from raven.providers import relay_wire

        loop = asyncio.get_running_loop()
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        stderr_r, stderr_w = os.pipe()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(_REQUEST_TIMEOUT_S)

            def ask() -> dict[str, Any]:
                sock.connect(self._socket)
                socket.send_fds(sock, [b"F"], [stdin_r, stdout_w, stderr_w])
                relay_wire.send_frame(sock, {"argv": argv, "env": env, "cwd": cwd or os.getcwd()})
                return relay_wire.recv_frame(sock)

            reply = await asyncio.to_thread(ask)
        except BaseException:
            for fd in (stdin_w, stdout_r, stderr_r):
                os.close(fd)
            sock.close()
            raise
        finally:
            for fd in (stdin_r, stdout_w, stderr_w):
                os.close(fd)
        pid = reply.get("pid") if isinstance(reply, dict) else None
        if not isinstance(pid, int):
            for fd in (stdin_w, stdout_r, stderr_r):
                os.close(fd)
            sock.close()
            raise RuntimeError(f"the fork template refused: {reply.get('error') if isinstance(reply, dict) else reply}")
        leftovers.record(pid, os.path.basename(argv[1]) if len(argv) > 1 else "agent")
        sock.setblocking(False)
        control = await asyncio.open_unix_connection(sock=sock)
        stdout = asyncio.StreamReader(limit=_READER_LIMIT, loop=loop)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stdout, loop=loop), os.fdopen(stdout_r, "rb", 0)
        )
        stderr = asyncio.StreamReader(limit=_READER_LIMIT, loop=loop)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stderr, loop=loop), os.fdopen(stderr_r, "rb", 0)
        )
        transport, protocol = await loop.connect_write_pipe(
            lambda: asyncio.streams.FlowControlMixin(loop=loop), os.fdopen(stdin_w, "wb", 0)
        )
        stdin = asyncio.StreamWriter(transport, protocol, None, loop)
        return ForkedEndpoint(pid, stdin=stdin, stdout=stdout, stderr=stderr, control=control, template=self)

    def close_when_idle(self, after: float | None) -> None:
        """Close the template once it has had no agent for ``after`` seconds; ``None`` keeps it."""
        if after is None or self._children or not self.running:
            return
        if self._idle_task is not None and not self._idle_task.done():
            return

        async def wait_then_close() -> None:
            await asyncio.sleep(after)
            async with self._lock:
                if not self._children and not self._forking:
                    await self.close()

        self._idle_task = asyncio.get_running_loop().create_task(wait_then_close(), name="fork-template-idle")

    async def _hold(self, proc: asyncio.subprocess.Process) -> None:
        """Keep the template for as long as its loop runs, and end it with the loop.

        The template's pipes and the watcher that reaps it belong to the loop it
        was started on. ``asyncio.run`` cancels the tasks still pending when its
        coroutine returns and lets them finish before it closes the loop, so,
        cancelled here, the template is ended, reaped and its pipes closed while
        that loop still can. A loop closed any other way leaves it to :meth:`abandon`.
        """
        try:
            await proc.wait()
        except asyncio.CancelledError:
            if self._proc is proc:
                await self.close()
            raise

    def abandon(self) -> None:
        """End the template without the loop it was started on, which may be closed by now.

        The fallback for a loop that closed without letting :meth:`_hold` finish.
        The agents it forked end with it (they armed its death signal); a loop
        that has ended has no use for them either, their pipes being its. Closing
        the transport here may fail on that loop, but it marks the transport closed
        first, so its finalizer does not try again on a loop that is gone.
        """
        from raven.acp_client import leftovers

        proc, self._proc = self._proc, None
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            leftovers.forget(proc.pid)
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
        if self._dir is not None:
            import shutil

            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    async def close(self) -> None:
        from raven.acp_client import leftovers

        proc, self._proc = self._proc, None
        hold, self._hold_task = self._hold_task, None
        try:
            if proc is not None and proc.returncode is None:
                # Letting go of its stdin is how the template is asked to stop; the
                # group kill after the grace is for one that does not, and is waited
                # out too, so the template is reaped before this returns.
                if proc.stdin is not None:
                    with contextlib.suppress(Exception):
                        proc.stdin.close()
                with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                    await asyncio.wait_for(proc.wait(), timeout=_TEARDOWN_GRACE_S)
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                        await asyncio.wait_for(proc.wait(), timeout=_TEARDOWN_GRACE_S)
        finally:
            # Also when a wait above is cancelled, as when its loop ends: the
            # template is not left running, and its pipes are closed while that
            # loop can still close them.
            if proc is not None:
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(proc.pid, signal.SIGKILL)
                leftovers.forget(proc.pid)
                transport = getattr(proc, "_transport", None)
                if transport is not None:
                    with contextlib.suppress(Exception):
                        transport.close()
            if self._log_task is not None:
                self._log_task.cancel()
                self._log_task = None
            if hold is not None and hold is not asyncio.current_task():
                hold.cancel()
            if self._dir is not None:
                import shutil

                shutil.rmtree(self._dir, ignore_errors=True)
                self._dir = None


_TEMPLATE: ForkTemplate | None = None


def template_available() -> bool:
    """Whether this system can fork agents from a template at all."""
    return sys.platform.startswith("linux") and hasattr(socket, "send_fds") and hasattr(os, "fork")


async def fork_launch(
    argv: list[str], env: dict[str, str], cwd: str | None, *, base_env: dict[str, str], skip: Sequence[str] = ()
) -> ForkedEndpoint | None:
    """The agent ``argv`` names, forked from this host's template; ``None`` to launch it the ordinary way.

    ``base_env`` is what the template itself starts with: the environment every
    agent shares, before any one agent's own entries. ``skip`` names module
    prefixes the template does not load (LiteLLM, when the agents' model calls
    go to the host). A template that cannot start is not tried again in this
    process; one fork that fails is logged and this launch goes the ordinary way.
    """
    from loguru import logger

    global _TEMPLATE
    if not template_available() or not raven_agent_argv(argv):
        return None
    if cwd is not None and not os.path.isdir(cwd):
        # The ordinary launch refuses a missing directory in the host, where the
        # caller hears why; a fork would only see the agent die.
        return None
    if _TEMPLATE is not None and _TEMPLATE.loop is not asyncio.get_running_loop():
        if _TEMPLATE.loop.is_running():
            # A loop in another thread (a CLI command the RPC server runs on a
            # worker) still holds it, and its agents would die with it: this
            # one launches the ordinary way.
            return None
        # Started under a loop that has ended (a CLI turn is one asyncio.run).
        _TEMPLATE.abandon()
        _TEMPLATE = None
    if _TEMPLATE is None or (_TEMPLATE._skip != tuple(skip) and not _TEMPLATE.children):
        if _TEMPLATE is not None:
            await _TEMPLATE.close()
        _TEMPLATE = ForkTemplate(skip=skip)
    if _TEMPLATE.broken:
        return None
    try:
        return await _TEMPLATE.spawn(argv, env, cwd, base_env)
    except Exception as exc:  # noqa: BLE001 - the ordinary launch is always there to fall back to
        logger.warning("fork template: launching {} the ordinary way: {}", argv[1:2], exc)
        return None


async def close_template() -> None:
    """Close the template. For process shutdown and for tests."""
    global _TEMPLATE
    template, _TEMPLATE = _TEMPLATE, None
    if template is not None and template.loop is not asyncio.get_running_loop():
        template.abandon()
    elif template is not None:
        await template.close()
