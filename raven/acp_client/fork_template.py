"""Raven agents started by forking a process that has already loaded their code.

A Raven agent is a whole Raven: launched fresh, each one imports the same few
thousand modules and holds its own copy of them, about 120 MiB before it has
done anything. On Linux a process that has imported them once can ``fork``, and
the children share those pages with it until they write to them. So the host
keeps one such process -- the template -- and asks it for each Raven agent it
launches; measured on the product agents, what each one costs on its own falls
by about two thirds.

**The template.** ``python -m raven.acp_client.fork_template``, started by the
host through the same parent-death wrapper as any agent (``leftovers``), so it
ends with the host. It imports a seed of the modules every agent needs plus
the modules agents were seen to import before -- each agent writes its module
list beside the template after a while, under its product, and the next
template loads what at least two products share (one product's own list while
only one has been seen; see :func:`preload_names`) -- then collects, freezes the heap out of the collector's reach -- a collection writes
to every object it visits, which would copy the shared pages back apart -- and
then answers fork requests on a socket in a private directory. It runs no
thread, and refuses to fork if an import started one.

**A fork.** The host hands over the agent's argv, environment and working
directory, and the three pipe ends that become its stdin, stdout and stderr.
The child starts a session of its own (so the host ends it with ``killpg`` as
it would any agent), arms the parent-death signal against the template, and
runs the agent's program in place of the ``exec`` a fresh interpreter would
have done: a product launcher's own ``os.execv`` of ``python -m raven`` is
answered the same way, by running Raven in the same process. The template
reaps the child and tells the host its exit status over the request's
connection, which stays open for that.

**What is eligible.** Only an agent this interpreter would run as Raven: the
host's own interpreter starting ``-m raven`` or a product launcher (a ``.py``
beside a ``subagent.json``). Anything else, any failure to start the template
or to fork, and every system but Linux, launches the ordinary way.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import gc
import importlib
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: Modules every Raven agent imports before it serves, imported by every
#: template whatever it has learned.
SEED_MODULES = (
    "raven.config.product_render",
    "raven.cli.commands",
    "raven.acp.server",
    "raven.acp.methods",
    "raven.rpc.bootstrap",
    "raven.agent.loop",
)

#: How long after starting an agent writes the modules it has imported. Long
#: enough to cover its first session and turn, where nearly all of them load.
LEARN_AFTER_S = 30.0
_LEARNED_FILE = "modules.json"
_LEARNED_CAP = 20_000

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
        argv = [sys.executable, "-P", "-m", __name__, "--socket", self._socket, "--learned", str(learned)]
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

    def abandon(self) -> None:
        """End the template without the loop it was started on, which may be closed by now.

        The agents it forked end with it (they armed its death signal); a loop
        that has ended has no use for them either, their pipes being its.
        """
        from raven.acp_client import leftovers

        proc, self._proc = self._proc, None
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            leftovers.forget(proc.pid)
        if self._dir is not None:
            import shutil

            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    async def close(self) -> None:
        from raven.acp_client import leftovers

        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            # Letting go of its stdin is how the template is asked to stop; the
            # group kill after the grace is for one that does not.
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), timeout=_TEARDOWN_GRACE_S)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        if proc is not None:
            leftovers.forget(proc.pid)
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
        if self._log_task is not None:
            self._log_task.cancel()
            self._log_task = None
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


# ---- the template's side -------------------------------------------------------


#: Not loaded ahead whatever was learned: the entry point itself, and this
#: module under its import name (it already runs as ``__main__``).
_NEVER_LOAD = frozenset({"__main__", "raven.acp_client.fork_template"})


def product_of(argv: Sequence[str]) -> str:
    """The name an agent's modules are learned under: its product folder, or ``raven`` for ``-m raven``."""
    if len(argv) > 1 and argv[1] != "-m":
        return Path(argv[1]).parent.name or "raven"
    return "raven"


def preload_names(learned: Any) -> list[str]:
    """What a template loads ahead of the agents: the modules they share, or one product's own.

    A module only one product imports is shared by nobody -- a host normally
    runs one process per product -- so loading it ahead would hold it in the
    template, unused, while any other product runs. Measured on the five
    shipped products: they share about two thousand modules, each adds a few
    dozen of its own, and the deck engine adds about a thousand. With only one
    product learned yet, its own set is all there is to share.
    """
    if not isinstance(learned, dict) or learned.get("python") != list(sys.version_info[:2]):
        return []
    products = learned.get("products")
    if not isinstance(products, dict):
        return []
    sets = [{n for n in names if isinstance(n, str)} for names in products.values() if isinstance(names, list)]
    if len(sets) == 1:
        return sorted(sets[0])
    counts = collections.Counter(name for names in sets for name in names)
    return sorted(name for name, seen in counts.items() if seen >= 2)


def _load(seed: Sequence[str], learned: Path, skip: Sequence[str]) -> int:
    """Import what the agents will import; the module count afterwards."""
    names = list(seed)
    try:
        names += preload_names(json.loads(learned.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    for name in names:
        if name in sys.modules or name in _NEVER_LOAD or name.endswith(".__main__") or name.startswith(tuple(skip)):
            continue
        try:
            importlib.import_module(name)
        except BaseException:  # noqa: BLE001 - an import that fails here fails the same way in the agent
            continue
    # Not an import: every agent loads the token estimator's table on its first
    # count, and the table's core is read-only once built, so loaded here it is
    # shared by every agent for as long as they run.
    with contextlib.suppress(Exception):
        from raven.utils.tokens import encoding

        encoding()
    return len(sys.modules)


def _learn(path: Path, skip: Sequence[str], product: str) -> None:
    """Add the modules this agent imported to what its product is known to import."""
    try:
        import fcntl

        mine = {
            name
            for name, module in list(sys.modules.items())
            if getattr(module, "__spec__", None) is not None
            and name != "__main__"
            and not name.endswith(".__main__")
            and not name.startswith(tuple(skip))
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            products = data.get("products") if isinstance(data, dict) else None
            if not isinstance(products, dict) or data.get("python") != list(sys.version_info[:2]):
                products = {}
            known = products.get(product)
            known = {n for n in known if isinstance(n, str)} if isinstance(known, list) else set()
            merged = sorted(known | mine)[:_LEARNED_CAP]
            if merged != sorted(known):
                products[product] = merged
                tmp = path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(
                    json.dumps({"python": list(sys.version_info[:2]), "products": products}), encoding="utf-8"
                )
                os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - learning is an optimisation for the next template
        pass


def _arm_parent_death(
    parent: int,
) -> None:  # pragma: no cover - runs only in a forked agent; see tests/test_acp_fork_template.py
    """End this process when the template does; it ends with the host the same way."""
    try:
        import ctypes

        armed = ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) == 0
    except Exception:  # noqa: BLE001 - without the signal the agent still ends on its closed stdin
        armed = False
    if armed and os.getppid() != parent:
        os._exit(1)


def _finish(code: int) -> None:  # pragma: no cover - runs only in a forked agent; see tests/test_acp_fork_template.py
    """End the agent the way an interpreter ends: threads joined, exit handlers run, streams flushed.

    Never by unwinding: the frames below the agent's program are the template's,
    and a launcher's own exit handling must not run after Raven, which it would
    never have reached past a real ``exec``.
    """
    import atexit

    with contextlib.suppress(Exception):
        threading._shutdown()  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        atexit._run_exitfuncs()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    os._exit(code)


def _exit_code(exc: BaseException) -> int:
    """What an interpreter exits with when ``exc`` ends its program, printing what it would print."""
    if isinstance(exc, SystemExit):
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        with contextlib.suppress(Exception):
            print(exc.code, file=sys.stderr)
        return 1
    with contextlib.suppress(Exception):
        sys.excepthook(type(exc), exc, exc.__traceback__)
    return 1


def _run_as_raven(args: Sequence[str]) -> None:
    """``python -m raven <args>`` in this process, then the end of it."""
    import runpy

    sys.argv = ["raven", *args]
    sys.path.insert(0, os.getcwd())
    try:
        runpy.run_module("raven", run_name="__main__", alter_sys=True)
        code = 0
    except BaseException as exc:  # noqa: BLE001 - the program's end, however it came
        code = _exit_code(exc)
    _finish(code)


def _run_agent(argv: list[str]) -> None:
    """Run the agent's program here, as a fresh interpreter would have run it, and end the process."""
    import runpy

    real_execv = os.execv

    def execv(path: Any, args: Any) -> None:
        args = [str(a) for a in args]
        if os.path.abspath(str(path)) == os.path.abspath(sys.executable) and args[1:3] == ["-m", "raven"]:
            os.execv = real_execv
            _run_as_raven(args[3:])
        real_execv(path, args)

    os.execv = execv
    if argv[1] == "-m":
        _run_as_raven(argv[3:])
    script = os.path.abspath(argv[1])
    sys.argv = [argv[1], *argv[2:]]
    sys.path.insert(0, os.path.dirname(script))
    try:
        runpy.run_path(script, run_name="__main__")
        code = 0
    except BaseException as exc:  # noqa: BLE001 - the program's end, however it came
        code = _exit_code(exc)
    _finish(code)


def _become(  # pragma: no cover - runs only in a forked agent; see tests/test_acp_fork_template.py
    request: dict[str, Any], fds: list[int], template: int, learned: Path, skip: Sequence[str], learn_after: float
) -> None:
    """Turn this freshly forked child into the agent the request names. Does not return."""
    os.setsid()
    _arm_parent_death(template)
    for signum in (signal.SIGCHLD, signal.SIGTERM):
        signal.signal(signum, signal.SIG_DFL)
    with contextlib.suppress(ValueError):
        signal.set_wakeup_fd(-1)
    for target, fd in zip((0, 1, 2), fds, strict=True):
        os.dup2(fd, target)
        os.close(fd)
    os.chdir(request["cwd"])
    os.environ.clear()
    os.environ.update({str(k): str(v) for k, v in (request.get("env") or {}).items()})
    timer = threading.Timer(learn_after, _learn, args=(learned, skip, product_of(request["argv"])))
    timer.daemon = True
    timer.start()
    _run_agent([str(a) for a in request["argv"]])


def _serve(path: str, *, lifeline: int) -> tuple[dict[str, Any], list[int]] | None:
    """Fork agents on request until the host lets go of ``lifeline``.

    In a forked child this returns the request it is to become; in the template
    it returns ``None`` once ``lifeline`` -- the template's stdin, whose other end
    only the host holds -- reads as closed.
    """
    import selectors

    from raven.providers import relay_wire

    template = os.getpid()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(64)
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGCHLD, lambda *_: None)
    selector = selectors.DefaultSelector()
    selector.register(listener, selectors.EVENT_READ)
    selector.register(wake_r, selectors.EVENT_READ)
    selector.register(lifeline, selectors.EVENT_READ)
    reporting: dict[int, socket.socket] = {}

    def let_go(*, child: bool) -> None:
        selector.close()
        listener.close()
        for conn in reporting.values():
            conn.close()
        if not child:
            signal.set_wakeup_fd(-1)
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        os.close(wake_r)
        os.close(wake_w)

    print(
        json.dumps(
            {"ready": True, "pid": template, "modules": len(sys.modules), "seconds": time.monotonic() - _STARTED}
        ),
        flush=True,
    )
    while True:
        for key, _ in selector.select():
            if key.fileobj == lifeline:
                if not os.read(lifeline, 4096):
                    let_go(child=False)
                    return None
                continue
            if key.fileobj == wake_r:
                with contextlib.suppress(BlockingIOError):
                    os.read(wake_r, 4096)
                _report_exits(reporting)
                continue
            conn, _ = listener.accept()
            conn.settimeout(_REQUEST_TIMEOUT_S)
            fds: list[int] = []
            try:
                _, fds, _, _ = socket.recv_fds(conn, 1, 3)
                request = relay_wire.recv_frame(conn)
                if len(fds) != 3 or not isinstance(request.get("argv"), list) or not request.get("cwd"):
                    raise ValueError("a fork request carries an argv, a cwd and three descriptors")
                if threading.active_count() != 1:
                    raise RuntimeError("the template is running a thread and cannot fork safely")
                pid = os.fork()
            except Exception as exc:  # noqa: BLE001 - one bad request must not end the template
                for fd in fds:
                    os.close(fd)
                with contextlib.suppress(OSError):
                    relay_wire.send_frame(conn, {"error": f"{type(exc).__name__}: {exc}"})
                conn.close()
                continue
            if pid == 0:  # pragma: no cover - the forked agent's side of the fork
                conn.close()
                let_go(child=True)
                return request, fds
            for fd in fds:
                os.close(fd)
            try:
                relay_wire.send_frame(conn, {"pid": pid})
                reporting[pid] = conn
            except OSError:
                conn.close()


def _report_exits(reporting: dict[int, socket.socket]) -> None:
    """Reap every child that has ended and tell its requester how."""
    from raven.providers import relay_wire

    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        conn = reporting.pop(pid, None)
        if conn is not None:
            with contextlib.suppress(OSError):
                relay_wire.send_frame(conn, {"exit": os.waitstatus_to_exitcode(status)})
            conn.close()


_STARTED = time.monotonic()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="raven-fork-template")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--learned", required=True)
    parser.add_argument("--skip", action="append", default=[])
    parser.add_argument("--learn-after", type=float, default=LEARN_AFTER_S)
    args = parser.parse_args(argv)
    learned = Path(args.learned)
    # stdout is the channel the host reads the ready line from; whatever an
    # import prints goes to stderr instead, where the host only logs it.
    sys.stdout.flush()
    ready_channel = os.dup(1)
    os.dup2(2, 1)
    _load(SEED_MODULES, learned, args.skip)
    sys.stdout.flush()
    os.dup2(ready_channel, 1)
    os.close(ready_channel)
    gc.collect()
    gc.freeze()
    if threading.active_count() != 1:
        names = ", ".join(t.name for t in threading.enumerate() if t is not threading.current_thread())
        print(json.dumps({"ready": False, "reason": f"an import started a thread ({names})"}), flush=True)
        return 3
    forked = _serve(args.socket, lifeline=0)
    if forked is None:
        return 0
    request, fds = forked
    _become(request, fds, os.getppid(), learned, args.skip, args.learn_after)
    return 1


if __name__ == "__main__":
    sys.exit(main())
