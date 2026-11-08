"""The fork template's own process: load what Raven agents load, then fork them on request.

Started by the host as ``python -P -m raven --fork-template`` (see
``raven.acp_client.fork_template``, the host's side): through Raven's entry
point like every Raven process, so the certificate store is settled before it
loads anything, and the TLS contexts its imports build are the ones its agents
would have built. It is a launcher: it
imports, by name, the modules a Raven agent imports -- the CLI, the ACP server,
the RPC stack, the agent loop -- and runs ``python -m raven`` in the agents it
forks, which is why it sits beside the CLI rather than with the client that
starts it.

**Loading.** A seed of the modules every agent needs plus the modules agents
were seen to import before: each agent writes its module list beside the
template after a while, under its product, and the next template loads what at
least two products share (one product's own list while only one has been seen;
see :func:`preload_names`). Then it collects and freezes the heap out of the
collector's reach -- a collection writes to every object it visits, which would
copy the shared pages back apart -- and answers fork requests on a socket in a
private directory. It runs no thread, and refuses to fork if an import started
one.

**A fork.** The child starts a session of its own, arms the parent-death signal
against the template, and runs the agent's program in place of the ``exec`` a
fresh interpreter would have done: a product launcher's own ``os.execv`` of
``python -m raven`` is answered the same way, by running Raven in the same
process. The template reaps the child and tells the host its exit status.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import gc
import importlib
import json
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from raven.acp_client.fork_template import LEARN_AFTER_S

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

_LEARNED_CAP = 20_000
_REQUEST_TIMEOUT_S = 30.0


#: Not loaded ahead whatever was learned: the program an agent ran as, which is
#: its own and not the template's, and this module, which is already running.
_NEVER_LOAD = frozenset({"__main__", "raven.cli._fork_template"})


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
