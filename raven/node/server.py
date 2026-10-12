"""The machine side: a daemon that runs the host's agents and answers its file calls, outliving any one connection.

Started on a registered machine by the host's relay (``python -I -m raven.node
--attach --dir <node dir>``, over ssh) when none is up, and spoken to over a
unix socket under that directory; or, for tests and as a fallback, on this
process's stdin and stdout (``--stdio``). A request is a JSON-RPC 2.0 object
on one line; its answer is one line back. Each file call runs the very tool
the host would run on its own disk -- ``read_file``, ``list_dir``, ``find``,
``grep`` -- so a read of a remote file comes back exactly as a local one
would: the same line numbers, the same limits, the same footer.

An agent the host starts here (``agent/launch``) runs in its own process group,
in the login environment this daemon captures once at its first need, under a
*task* id of the host's. Its ACP frames travel as ``agent/frame``
notifications on a channel of the connection that asked, and are kept in the
task's record on disk as they arrive -- so when that connection goes, the
agent keeps running, and the next connection takes the task over from the
frame the host last read (``agent/attach``). Ending a task ends its whole
process tree, the commands the agent put in the background or in a session
of its own included. Only ``--stdio`` ties the agents to the connection.

Deliberately small. It imports the file tools and nothing that talks to a
model, reads a config or opens a network port, so a machine needs Python
3.12 and the packages in :data:`raven.node.protocol.REQUIREMENTS` beside
Raven's own code, not the whole of Raven's dependencies (about 1 GB,
measured 2026-10-10).

Requests run concurrently and are answered as they finish; what an agent
writes is forwarded as it arrives. One lock per connection orders its lines.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import stat as filemode
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any, BinaryIO

from raven.node import bundle, protocol

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

#: A request line the node will read; a file call is a few hundred bytes, a
#: chunk of a file sent for writing about 1.4 MB.
_LINE_LIMIT = 2 * 1024 * 1024

#: What an agent may put on one stdout line before the node stops reading it:
#: the frame the ACP stdio protocol allows, and what the host reads.
_AGENT_LINE_LIMIT = 8 * 1024 * 1024

#: Login shells whose ``-lic`` reads the profile and rc chain an interactive
#: session does and prints nothing of its own on the way: the two the host
#: drives for its own agents (``raven.agent.subagent.backends.env``). A user
#: of any other shell gets the environment ssh started this node with.
_DRIVABLE_SHELLS = frozenset({"bash", "zsh"})
#: Enough PATH to find ``env`` when ssh gave the node none.
_BOOTSTRAP_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_CAPTURE_TIMEOUT_S = 15.0

#: How long an agent gets to leave on its own after its stdin closes, before
#: its group is ended.
_CLOSE_GRACE_S = 3.0
#: How long the node waits for an ended group to be reaped on its way out.
_END_WAIT_S = 2.0
#: How long the last answers get to reach a host that has hung up.
_ANSWER_DRAIN_S = 1.0


class _RefusedError(Exception):
    """A request answered with a JSON-RPC error rather than a result."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _text(params: dict[str, Any], name: str, *, default: str | None = None) -> str:
    value = params.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise _RefusedError(_INVALID_PARAMS, f"{name} must be a non-empty string")
    return value


def _number(params: dict[str, Any], name: str, default: int | None = None) -> int | None:
    value = params.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _RefusedError(_INVALID_PARAMS, f"{name} must be a whole number")
    return value


def _flag(params: dict[str, Any], name: str) -> bool:
    value = params.get(name, False)
    if not isinstance(value, bool):
        raise _RefusedError(_INVALID_PARAMS, f"{name} must be true or false")
    return value


def _argv(params: dict[str, Any]) -> list[str]:
    try:
        argv = shlex.split(_text(params, "command"))
    except ValueError as exc:
        raise _RefusedError(_INVALID_PARAMS, f"command cannot be parsed: {exc}") from None
    if not argv:
        raise _RefusedError(_INVALID_PARAMS, "command is empty")
    return argv


def _env(params: dict[str, Any]) -> dict[str, str]:
    extra = params.get("env") or {}
    if not isinstance(extra, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items()):
        raise _RefusedError(_INVALID_PARAMS, "env must be an object of strings")
    return dict(extra)


def _roots(params: dict[str, Any]) -> tuple[Path, ...]:
    """The directories this call may touch, as given, made absolute on this machine.

    Required and never empty: the file tools read an empty fence as "no fence",
    so a call that named none would reach the whole disk.
    """
    raw = params.get("roots")
    if not isinstance(raw, list) or not raw or not all(isinstance(r, str) and r.strip() for r in raw):
        raise _RefusedError(_INVALID_PARAMS, "roots must be a non-empty list of directories")
    out = []
    for root in raw:
        path = Path(root.strip()).expanduser()
        if not path.is_absolute():
            raise _RefusedError(_INVALID_PARAMS, f"root {root!r} is not an absolute directory")
        out.append(path)
    return tuple(out)


def _tool(cls: Any, params: dict[str, Any]) -> Any:
    """One of Raven's file tools, fenced to this call's roots.

    Relative paths resolve against the home directory, as a path after the
    colon in ``host:path`` does for scp. ``follow_binding=False``: there is no
    turn here whose working directory could widen the fence.
    """
    return cls(workspace=Path.home(), allowed_dirs=_roots(params), follow_binding=False)


def _answer(value: Any) -> dict[str, Any]:
    """A tool's return value as the wire carries it: the text, an image's blocks, a write's diff."""
    if isinstance(value, str):
        return {"text": value}
    out: dict[str, Any] = {
        "text": value.model_text,
        "display": value.display_text,
        "ok": bool(value.ok),
        "blocks": list(getattr(value, "blocks", None) or []),
    }
    if getattr(value, "diff", None) is not None:
        out["diff"] = value.diff
    change = getattr(value, "file_change", None)
    if change is not None:
        out["file_change"] = {"path": change.path, "after": change.after, "before": change.before}
    return out


def _fenced(params: dict[str, Any]) -> Path:
    """The call's path placed on this machine, inside its roots, whether or not it exists yet.

    The same resolution the file tools make (symlinks followed, the roots
    checked after), for the calls that are not a tool: a stat, a chunk, a
    change listing.
    """
    from raven.agent.tools.filesystem import resolve_path

    try:
        return resolve_path(_text(params, "path"), Path.home(), _roots(params))
    except PermissionError as exc:
        raise _RefusedError(_INVALID_PARAMS, str(exc)) from None


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(protocol.CHUNK), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _as_it_is(path: Path) -> str:
    """How ``fs/write``'s ``expect`` names a file's state: its digest, or ``absent``."""
    return _sha256(path) if path.is_file() else "absent"


async def _hello(params: dict[str, Any]) -> dict[str, Any]:
    asked = params.get("protocol")
    if asked is not None and asked != protocol.PROTOCOL:
        raise _RefusedError(_INVALID_REQUEST, f"this node speaks protocol {protocol.PROTOCOL}, not {asked!r}")
    from raven import __version__
    from raven.agent.tools.file_search import _resolve_rg

    return {
        "protocol": protocol.PROTOCOL,
        "version": __version__,
        "digest": bundle.digest(),
        "python": platform.python_version(),
        "os": platform.system(),
        "home": str(Path.home()),
        "pid": os.getpid(),
        # Whether ``grep`` runs rg here or Raven's own Python search: the two
        # differ on regex corners, and the host may want to say which ran.
        "rg": _resolve_rg() is not None,
        "limits": {"agents": protocol.MAX_AGENTS, "line": _LINE_LIMIT},
    }


async def _ping(params: dict[str, Any]) -> dict[str, Any]:
    return {"t": params.get("t")}


async def _read(params: dict[str, Any]) -> dict[str, Any]:
    from raven.agent.tools.filesystem import ReadFileTool

    tool = _tool(ReadFileTool, params)
    path = _text(params, "path")
    out = _answer(await tool.execute(path, offset=_number(params, "offset", 1) or 1, limit=_number(params, "limit")))
    # The whole file's digest beside the page read, so a later write of the
    # whole file can say what it expects to replace (``fs/write``, ``expect``).
    try:
        placed = tool._resolve(path)
    except PermissionError:
        return out
    if placed.is_file():
        out["sha256"] = _sha256(placed)
    return out


async def _list(params: dict[str, Any]) -> dict[str, Any]:
    from raven.agent.tools.filesystem import ListDirTool

    tool = _tool(ListDirTool, params)
    return _answer(
        await tool.execute(
            _text(params, "path"), recursive=_flag(params, "recursive"), max_entries=_number(params, "max_entries")
        )
    )


async def _find(params: dict[str, Any]) -> dict[str, Any]:
    from raven.agent.tools.file_search import FindTool

    tool = _tool(FindTool, params)
    return _answer(
        await tool.execute(
            _text(params, "pattern"), path=_text(params, "path", default="."), limit=_number(params, "limit")
        )
    )


async def _grep(params: dict[str, Any]) -> dict[str, Any]:
    from raven.agent.tools.file_search import GrepTool

    tool = _tool(GrepTool, params)
    glob = params.get("glob")
    if glob is not None and not isinstance(glob, str):
        raise _RefusedError(_INVALID_PARAMS, "glob must be a string")
    mode = params.get("output_mode", "content")
    if not isinstance(mode, str):
        raise _RefusedError(_INVALID_PARAMS, "output_mode must be a string")
    return _answer(
        await tool.execute(
            _text(params, "pattern"),
            path=_text(params, "path", default="."),
            glob=glob,
            output_mode=mode,
            case_insensitive=_flag(params, "case_insensitive"),
            context=_number(params, "context", 0) or 0,
            limit=_number(params, "limit"),
        )
    )


async def _stat(params: dict[str, Any]) -> dict[str, Any]:
    """What is at a path: whether it exists, what kind, how big, when it changed, and on request its digest."""
    path = _fenced(params)
    try:
        st = path.stat()
    except FileNotFoundError:
        return {"exists": False, "kind": "missing", "size": 0, "mtime": None}
    except OSError as exc:
        raise _RefusedError(_INTERNAL_ERROR, f"could not stat {path}: {exc.strerror or exc}") from None
    if filemode.S_ISDIR(st.st_mode):
        kind = "dir"
    elif filemode.S_ISREG(st.st_mode):
        kind = "file"
    else:
        kind = "other"
    out: dict[str, Any] = {"exists": True, "kind": kind, "size": st.st_size, "mtime": st.st_mtime}
    if kind == "file" and _flag(params, "hash"):
        out["sha256"] = _sha256(path)
    return out


async def _write(params: dict[str, Any]) -> dict[str, Any]:
    """The host's ``write_file`` on this machine.

    ``expect`` is the file as the host last read it -- its digest, or
    ``absent`` -- and a file that is not so any more is left alone, with
    :data:`raven.node.protocol.CHANGED`. The check and the write run in one
    worker thread under the daemon's write lock (:func:`_apart`), so no other
    write comes between them: what was checked is what is written over, and
    the loop is not held while a whole file is hashed.
    """
    from raven.agent.tools.filesystem import WriteFileTool

    tool = _tool(WriteFileTool, params)
    path = _text(params, "path")
    content = params.get("content")
    if not isinstance(content, str):
        raise _RefusedError(_INVALID_PARAMS, "content must be a string")
    mode = params.get("mode", "overwrite")
    if not isinstance(mode, str):
        raise _RefusedError(_INVALID_PARAMS, "mode must be a string")
    expect = params.get("expect")
    if expect is not None:
        if not isinstance(expect, str):
            raise _RefusedError(_INVALID_PARAMS, "expect must be a digest or 'absent'")
        try:
            placed = tool._resolve(path)
        except PermissionError as exc:
            return {"text": f"Error: {exc}"}
        if _as_it_is(placed) != expect.strip().lower():
            raise _RefusedError(protocol.CHANGED, f"{placed} changed after it was read; nothing was written")
    return _answer(await tool.execute(path, content, mode=mode))


async def _edit(params: dict[str, Any]) -> dict[str, Any]:
    """The host's ``edit_file`` on this machine: one read, the replacement, one write, with nothing between (under the write lock, :func:`_apart`)."""
    from raven.agent.tools.filesystem import EditFileTool

    tool = _tool(EditFileTool, params)
    new_text = params.get("new_text")
    if not isinstance(new_text, str):
        raise _RefusedError(_INVALID_PARAMS, "new_text must be a string")
    return _answer(
        await tool.execute(
            _text(params, "path"), _text(params, "old_text"), new_text, replace_all=_flag(params, "replace_all")
        )
    )


async def _pull(params: dict[str, Any]) -> dict[str, Any]:
    """One chunk of a file the host fetches; the first chunk carries the whole file's digest."""
    path = _fenced(params)
    if not path.is_file():
        raise _RefusedError(_INVALID_PARAMS, f"{path} is not a file on this machine")
    offset = _number(params, "offset", 0) or 0
    length = _number(params, "length", protocol.CHUNK) or protocol.CHUNK
    if offset < 0 or not 0 < length <= protocol.CHUNK:
        raise _RefusedError(_INVALID_PARAMS, f"offset must be a whole number and length between 1 and {protocol.CHUNK}")
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(length)
        out: dict[str, Any] = {
            "data": base64.b64encode(data).decode("ascii"),
            "total": size,
            "eof": offset + len(data) >= size,
        }
        if offset == 0:
            out["sha256"] = _sha256(path)
    except OSError as exc:
        raise _RefusedError(_INTERNAL_ERROR, f"could not read {path}: {exc.strerror or exc}") from None
    return out


async def _session_dir(params: dict[str, Any]) -> dict[str, Any]:
    """Make the session directory the host names and say where it is, as this machine resolves it.

    The name is the host's: it knows the agent's root and the handle's safe
    spelling. ``since`` is this machine's clock as the directory is made, so a
    later listing of what changed can be asked for what came after.
    """
    path = Path(_text(params, "path")).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _RefusedError(_INTERNAL_ERROR, f"could not make {path}: {exc.strerror or exc}") from None
    return {"path": str(path.resolve()), "since": time.time()}


async def _probe_machine(params: dict[str, Any]) -> dict[str, Any]:
    """One look at this machine: room, Python, shell, uv, load."""
    disk: dict[str, int | None] = {}
    for where in (Path.home(), Path.cwd()):
        try:
            disk[str(where)] = shutil.disk_usage(where).free // 1024
        except OSError:
            disk[str(where)] = None
    try:
        load: list[float] | None = [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        load = None
    return {
        "disk_free_kb": disk,
        "python": platform.python_version(),
        "shell": os.environ.get("SHELL", ""),
        "uv": shutil.which("uv") or "",
        "load": load,
    }


# --- what the node starts ---------------------------------------------------------


def capture_login_env() -> dict[str, str]:
    """This machine user's login environment, as a shell session would have it.

    Taken the way the host takes its own: one ``-lic env -0`` of the login
    shell, so an rc file's exports (nvm, a local bin dir) are in it and
    whatever the rc file prints is not. Over the environment ssh started this
    node with, not a minimal base: that environment is already the login one
    PAM and sshd made (an ``/etc/environment`` PATH holding the machine's
    conda, measured 2026-10-12), and the profile only adds to it. A login
    shell this cannot drive, or a capture that fails, leaves it as it is.
    """
    shell = os.environ.get("SHELL", "").strip()
    if not shell or os.path.basename(shell) not in _DRIVABLE_SHELLS:
        return dict(os.environ)
    base = dict(os.environ)
    base.setdefault("PATH", _BOOTSTRAP_PATH)
    try:
        done = subprocess.run(
            [shell, "-lic", "env -0"],
            env=base,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_CAPTURE_TIMEOUT_S,
            # ``-i`` reaches for a controlling terminal; a session of its own
            # gives it none to take over.
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return dict(os.environ)
    captured = {
        key: value
        for key, sep, value in (entry.partition("=") for entry in done.stdout.decode("utf-8", "replace").split("\0"))
        if key and sep
    }
    if done.returncode != 0 or not captured.get("PATH"):
        return dict(os.environ)
    return captured


def _descendants(pid: int) -> list[int]:
    """Every process under ``pid`` by parent chain, as ``ps`` sees them now."""
    ps = shutil.which("ps") or "/bin/ps"
    try:
        done = subprocess.run([ps, "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    out: list[int] = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            out.append(child)
            stack.append(child)
    return out


def _end_tree(pid: int) -> None:
    """End ``pid``, the process group it leads and every process under it, in that group or out of it.

    The descendants are found before the first signal: afterwards they are
    init's, and the tree that said whose they were is gone. Out of the group
    is the case that matters: an agent may put a command in a session of its
    own -- Claude Code's shell tool does (measured 2026-10-12: a loop it
    started outlived the group's SIGKILL) -- and that session's group goes
    with it. The node's own group is never signalled, whatever is found in
    it: a descendant there is ended by itself.
    """
    strays = _descendants(pid)
    own = os.getpgrp()
    if pid != own:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
    for stray in strays:
        try:
            group = os.getpgid(stray)
        except (ProcessLookupError, PermissionError):
            continue
        if group not in (pid, own):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(stray, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


#: Under the node directory the host registered (``--dir``): the socket and
#: lock of each install under ``run/``, the task records under ``tasks/``, the
#: daemon's own log under ``log/``.
RUN_DIR, TASKS_DIR, LOG_DIR = "run", "tasks", "log"

#: How long a daemon with no connection and no running task waits before it
#: leaves; the next relay starts it again.
IDLE_EXIT_S = 600.0
_IDLE_CHECK_S = 15.0
#: What a task's record says its agent ended with when the daemon that ran it
#: went first: the host reads it the way it reads a lost link.
LOST_RC = -1

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,119}$")


#: The most bytes a unix socket's path may have on the systems a node runs on (macOS: 104).
_SOCKET_PATH_MAX = 100


def socket_path(root: Path) -> Path:
    """Where the daemon of this install listens for ``root``: under ``run/``, or when that path is too long for a socket, in a directory of this user's own in the temp directory.

    A unix socket's path is short by the system's rule, and a node directory
    is wherever the owner put it. The fallback is named after ``root`` so a
    relay and a daemon given the same directory meet, and it lies in a
    directory only this user may open (:func:`own_dir`), as ``run/`` is, so
    what listens there is this user's own daemon and nothing else.
    """
    under = root / RUN_DIR / f"{bundle.install_name()}.sock"
    if len(os.fsencode(under)) <= _SOCKET_PATH_MAX:
        return under
    tag = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"raven-node-{os.getuid()}" / f"{tag}.sock"


def own_dir(path: Path) -> Path:
    """``path`` as a directory only this user may open: made so, or refused.

    What the socket's directory and the records' must be for what is in them
    to be this user's alone: a directory of this user's, not a link, with no
    group or other bits. One of this user's with wider bits is narrowed;
    anything else is left as it is and named, never used.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            st = path.lstat()
        except OSError as exc:
            raise PermissionError(f"could not make {path}: {exc.strerror or exc}") from None
    ours = filemode.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
    if ours and st.st_mode & 0o077:
        try:
            os.chmod(path, 0o700)
            st = path.lstat()
        except OSError as exc:
            raise PermissionError(f"could not make {path} this user's own: {exc.strerror or exc}") from None
    if not ours or st.st_mode & 0o077:
        raise PermissionError(
            f"{path} is not a directory only this user may open; remove it, or give the node a shorter directory"
        )
    return path


def owned(path: Path) -> bool:
    """Whether what is at ``path`` is this user's own, or nothing is there yet.

    What a relay checks before it forwards to a socket: in a directory of this
    user's own (:func:`own_dir`) anything that is somebody else's is root's,
    and the relay neither forwards to it nor removes it.
    """
    try:
        return path.lstat().st_uid == os.getuid()
    except FileNotFoundError:
        return True


def own_file(path: Path, mode: str = "ab") -> Any:
    """``path`` opened to append (or, ``"w"``, to write over), made with no group or other bits when it is new.

    A task's record holds the agent's frames as they were -- its replies, its
    tool results, which quote what it read -- so it is its owner's alone, as
    the host's own journal is.
    """
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    if "b" in mode:
        return os.fdopen(fd, mode)
    return os.fdopen(fd, mode, encoding="utf-8")


def pid_path(root: Path) -> Path:
    """Where the daemon of this install writes its pid under ``root``."""
    return root / RUN_DIR / f"{bundle.install_name()}.pid"


def _task_id(params: dict[str, Any]) -> str:
    raw = params.get("task")
    if raw is None:
        return f"t-{uuid.uuid4().hex[:12]}"
    if not isinstance(raw, str) or not _TASK_ID.match(raw):
        raise _RefusedError(_INVALID_PARAMS, "task must be a short name of letters, digits, '.', '_', '@' or '-'")
    return raw


class _StreamOut:
    """Lines out to one host, over stdout or a socket; one lock orders them."""

    def __init__(self, out: BinaryIO | asyncio.StreamWriter) -> None:
        self._out = out
        self._lock = asyncio.Lock()

    async def write_line(self, line: bytes) -> None:
        async with self._lock:
            if isinstance(self._out, asyncio.StreamWriter):
                if self._out.is_closing():
                    raise ConnectionError("the host's connection is closed")
                self._out.write(line)
                await self._out.drain()
            else:
                self._out.write(line)
                self._out.flush()


class _Task:
    """One agent the daemon started for a host, under the host's task id; it outlives the connection that asked.

    What the agent writes goes to the task's record on disk as it arrives
    (``frames.jsonl``, ``stderr.log``) and, while a connection holds the
    task on a channel, to that connection. A connection that goes leaves the
    agent running and the record growing; ``attach`` replays the record from
    the frame the host last read and takes over from there.
    """

    def __init__(
        self, task_id: str, proc: asyncio.subprocess.Process, daemon: Daemon, record: Path, *, command: str, cwd: str
    ) -> None:
        self.id = task_id
        self.proc = proc
        self.daemon = daemon
        self.record = record
        self.command = command
        self.cwd = cwd
        self.started = time.time()
        self.ended: float | None = None
        self.rc: int | None = None
        self.frames = 0
        self.subscriber: tuple[Node, int] | None = None
        self._send_lock = asyncio.Lock()
        self._frames_file = own_file(record / "frames.jsonl")
        self._stderr_file = own_file(record / "stderr.log")
        self.write_record("running")
        self._forwarders = [
            asyncio.create_task(self._forward(proc.stdout, frames=True)),
            asyncio.create_task(self._forward(proc.stderr, frames=False)),
        ]
        self._exit_task = asyncio.create_task(self._exit())

    @property
    def channel(self) -> int | None:
        return self.subscriber[1] if self.subscriber is not None else None

    def write_record(self, state: str) -> None:
        data = {
            "task": self.id,
            "command": self.command,
            "cwd": self.cwd,
            "pid": self.proc.pid,
            "state": state,
            "rc": self.rc,
            "started": self.started,
            "ended": self.ended,
            "frames": self.frames,
        }
        _write_json(self.record / "task.json", data)

    def summary(self) -> dict[str, Any]:
        return {
            "task": self.id,
            "state": "running" if self.rc is None else "exited",
            "rc": self.rc,
            "pid": self.proc.pid,
            "command": self.command,
            "cwd": self.cwd,
            "started": self.started,
            "ended": self.ended,
            "frames": self.frames,
            "attached": self.subscriber is not None,
        }

    async def _tell(self, node: Node, method: str, params: dict[str, Any]) -> None:
        """One notification to the connection holding this task; a connection that is gone loses the task."""
        try:
            await node.notify(method, params)
        except (ConnectionError, OSError, ValueError):
            if self.subscriber is not None and self.subscriber[0] is node:
                self.subscriber = None
            node.channels.pop(params.get("channel"), None)

    async def _forward(self, stream: asyncio.StreamReader | None, *, frames: bool) -> None:
        if stream is None:  # pragma: no cover - PIPE is always requested
            return
        while True:
            try:
                raw = await stream.readline()
            except ValueError:
                # Past the reader's limit; the reader has dropped it and goes on.
                continue
            if not raw:
                return
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not text:
                continue
            async with self._send_lock:
                handle = self._frames_file if frames else self._stderr_file
                handle.write(text.encode("utf-8") + b"\n")
                handle.flush()
                if frames:
                    self.frames += 1
                if self.subscriber is not None:
                    node, channel = self.subscriber
                    if frames:
                        await self._tell(
                            node, protocol.AGENT_FRAME, {"channel": channel, "line": text, "n": self.frames}
                        )
                    else:
                        await self._tell(node, protocol.AGENT_STDERR, {"channel": channel, "text": text})

    async def _exit(self) -> None:
        rc = await self.proc.wait()
        # After the output is drained, so the last frame lands before the
        # exit; bounded, because a grandchild the agent left behind can hold
        # the pipes open past the agent itself.
        _done, pending = await asyncio.wait(self._forwarders, timeout=_ANSWER_DRAIN_S)
        for task in pending:
            task.cancel()
        async with self._send_lock:
            self.rc = rc
            self.ended = time.time()
            self._frames_file.close()
            self._stderr_file.close()
            self.write_record("exited")
            if self.subscriber is not None:
                node, channel = self.subscriber
                self.subscriber = None
                node.channels.pop(channel, None)
                await self._tell(node, protocol.AGENT_EXIT, {"channel": channel, "rc": rc})
        self.daemon.touch()

    async def attach(self, node: Node, channel: int, after: int) -> dict[str, Any]:
        """Hand the task to ``node``'s ``channel``: the frames past the ``after``-th first, then whatever comes."""
        async with self._send_lock:
            replayed = await _replay(self.record, node, channel, after)
            if self.rc is None:
                old = self.subscriber
                if old is not None and old[0] is not node:
                    old[0].channels.pop(old[1], None)
                self.subscriber = (node, channel)
            else:
                await node.notify(protocol.AGENT_EXIT, {"channel": channel, "rc": self.rc})
            return {"pid": self.proc.pid, "frames": self.frames, "replayed": replayed, "rc": self.rc}

    async def write(self, line: str) -> None:
        if self.proc.stdin is None or self.proc.stdin.is_closing():
            return
        self.proc.stdin.write(line.encode("utf-8") + b"\n")
        try:
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def end(self) -> None:
        """End the agent and everything it started, in its group or out of it."""
        if self.proc.returncode is not None:
            return
        if hasattr(os, "killpg"):
            _end_tree(self.proc.pid)
        else:  # pragma: no cover - a node runs on POSIX
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()

    async def close(self) -> int | None:
        """Close the agent's stdin and give it a moment to leave, then end it."""
        if self.proc.stdin is not None and not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        try:
            return await asyncio.wait_for(self.proc.wait(), timeout=_CLOSE_GRACE_S)
        except asyncio.TimeoutError:
            self.end()
            return await self.proc.wait()


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    with own_file(tmp, "w") as handle:
        handle.write(json.dumps(data))
    os.replace(tmp, path)


async def _replay(record: Path, node: Node, channel: int, after: int) -> int:
    """Send ``node`` the frames of ``record`` past the ``after``-th, numbered; how many went."""
    sent = 0
    frames = record / "frames.jsonl"
    if not frames.is_file():
        return 0
    with frames.open("rb") as handle:
        for n, raw in enumerate(handle, 1):
            if n <= after:
                continue
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            if text:
                await node.notify(protocol.AGENT_FRAME, {"channel": channel, "line": text, "n": n})
                sent += 1
    return sent


class Daemon:
    """What outlives a connection: the tasks, the login environment, the records under ``root``.

    ``ephemeral`` is the ``--stdio`` way: one connection, whose end ends the
    agents too, with records in a directory nobody comes back to.
    """

    def __init__(self, root: Path, *, ephemeral: bool = False) -> None:
        self.root = root
        self.ephemeral = ephemeral
        self.tasks: dict[str, _Task] = {}
        self.connections: set[Node] = set()
        self._env: dict[str, str] | None = None
        self._env_lock = asyncio.Lock()
        self.last_activity = time.monotonic()
        own_dir(root / TASKS_DIR)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def env(self) -> dict[str, str]:
        """The login environment every agent here starts from, captured at the first need."""
        async with self._env_lock:
            if self._env is None:
                self._env = await asyncio.to_thread(capture_login_env)
            return self._env

    def running(self) -> list[_Task]:
        return [task for task in self.tasks.values() if task.rc is None]

    def record_dir(self, task_id: str) -> Path:
        return self.root / TASKS_DIR / task_id

    async def launch(self, node: Node, channel: int, params: dict[str, Any]) -> _Task:
        task_id = _task_id(params)
        known = self.tasks.get(task_id)
        if known is not None and known.rc is None:
            raise _RefusedError(_INVALID_PARAMS, f"task {task_id!r} is already running; attach to it")
        if len(self.running()) >= protocol.MAX_AGENTS:
            raise _RefusedError(protocol.BUSY, f"{protocol.MAX_AGENTS} agents are already running on this machine")
        argv = _argv(params)
        env = {**await self.env(), **_env(params)}
        found = shutil.which(argv[0], path=env.get("PATH"))
        if found is None:
            raise _RefusedError(protocol.NOT_FOUND, f"{argv[0]!r} is not on this machine's PATH")
        cwd = Path(_text(params, "cwd")).expanduser()
        record = self.record_dir(task_id)
        try:
            cwd.mkdir(parents=True, exist_ok=True)
            if record.exists():
                shutil.rmtree(record)
            record.mkdir(mode=0o700, parents=True)
        except OSError as exc:
            raise _RefusedError(_INTERNAL_ERROR, f"could not make {cwd}: {exc.strerror or exc}") from None
        try:
            proc = await asyncio.create_subprocess_exec(
                found,
                *argv[1:],
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=_AGENT_LINE_LIMIT,
            )
        except OSError as exc:
            raise _RefusedError(_INTERNAL_ERROR, f"could not start {argv[0]!r}: {exc.strerror or exc}") from None
        task = _Task(task_id, proc, self, record, command=shlex.join(argv), cwd=str(cwd))
        task.subscriber = (node, channel)
        self.tasks[task_id] = task
        self.touch()
        return task

    async def attach(self, node: Node, channel: int, task_id: str, after: int) -> dict[str, Any]:
        """``task_id`` onto ``node``'s ``channel``: a running task is taken over, a finished one replayed."""
        task = self.tasks.get(task_id)
        if task is not None:
            return await task.attach(node, channel, after)
        record = self.record_dir(task_id)
        info = _read_record(record)
        if info is None:
            raise _RefusedError(protocol.UNKNOWN_TASK, f"no task {task_id!r} is running or recorded on this machine")
        if info.get("state") == "running":
            # The daemon that ran it is gone, and the agent with it: the
            # record says so from now on, so no host reads a run that ended
            # as one still going.
            info["state"], info["rc"], info["ended"] = "lost", LOST_RC, info.get("ended") or time.time()
            _write_json(record / "task.json", info)
        replayed = await _replay(record, node, channel, after)
        rc = info.get("rc") if isinstance(info.get("rc"), int) else LOST_RC
        await node.notify(protocol.AGENT_EXIT, {"channel": channel, "rc": rc})
        return {"pid": info.get("pid"), "frames": info.get("frames"), "replayed": replayed, "rc": rc}

    def list_tasks(self) -> list[dict[str, Any]]:
        """Every task this machine knows: the running ones, and the records of finished ones."""
        out: dict[str, dict[str, Any]] = {task_id: task.summary() for task_id, task in self.tasks.items()}
        tasks_dir = self.root / TASKS_DIR
        for record in sorted(tasks_dir.iterdir()) if tasks_dir.is_dir() else []:
            if record.name in out or not record.is_dir():
                continue
            info = _read_record(record)
            if info is not None:
                out[record.name] = {
                    "task": record.name,
                    "state": info.get("state"),
                    "rc": info.get("rc"),
                    "pid": info.get("pid"),
                    "command": info.get("command"),
                    "cwd": info.get("cwd"),
                    "started": info.get("started"),
                    "ended": info.get("ended"),
                    "frames": info.get("frames"),
                    "attached": False,
                }
        return sorted(out.values(), key=lambda row: row.get("started") or 0)

    def sweep(self) -> int:
        """Remove the records of tasks that ended longer ago than :data:`protocol.TASK_KEEP_S`; how many."""
        gone = 0
        tasks_dir = self.root / TASKS_DIR
        for record in list(tasks_dir.iterdir()) if tasks_dir.is_dir() else []:
            if record.name in self.tasks:
                continue
            info = _read_record(record)
            ended = info.get("ended") if info else None
            if info is None or info.get("state") == "running":
                continue
            if isinstance(ended, (int, float)) and time.time() - ended > protocol.TASK_KEEP_S:
                shutil.rmtree(record, ignore_errors=True)
                gone += 1
        return gone

    async def end_all(self) -> None:
        for task in self.running():
            task.end()
        for task in self.running():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(task.proc.wait(), timeout=_END_WAIT_S)


def _read_record(record: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((record / "task.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Node:
    """One connection to a host: its channels onto the daemon's tasks, and the one way out.

    Without a ``daemon`` it is a connection of its own, the ``--stdio`` way:
    an ephemeral daemon in a temporary directory, whose agents end with it.
    """

    def __init__(self, out: BinaryIO | asyncio.StreamWriter, daemon: Daemon | None = None) -> None:
        self._out = _StreamOut(out)
        self.daemon = daemon or Daemon(Path(tempfile.mkdtemp(prefix="raven-node-")), ephemeral=True)
        self.channels: dict[int, _Task] = {}
        self.stopping = False
        self.stopped = asyncio.Event()
        self.daemon.connections.add(self)
        self.daemon.touch()

    @property
    def agents(self) -> dict[int, _Task]:
        """The tasks this connection holds, by channel."""
        return self.channels

    async def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        await self._out.write_line(line)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def env(self) -> dict[str, str]:
        return await self.daemon.env()

    async def end_all(self) -> None:
        await self.daemon.end_all()

    def hang_up(self) -> None:
        """Close the way out, when it is a socket: the daemon is leaving and takes the connection with it."""
        out = self._out._out
        if isinstance(out, asyncio.StreamWriter) and not out.is_closing():
            out.close()

    async def detach_all(self) -> None:
        """This connection is over: its tasks go on, held by nobody until the next connection attaches."""
        for channel, task in list(self.channels.items()):
            if task.subscriber is not None and task.subscriber[0] is self:
                task.subscriber = None
        self.channels.clear()
        self.daemon.connections.discard(self)
        self.daemon.touch()

    def _channel(self, params: dict[str, Any], *, free: bool) -> int:
        channel = _number(params, "channel")
        if channel is None:
            raise _RefusedError(_INVALID_PARAMS, "channel must be a whole number")
        if free and channel in self.channels:
            raise _RefusedError(_INVALID_PARAMS, f"channel {channel} is in use")
        if not free and channel not in self.channels:
            raise _RefusedError(_INVALID_PARAMS, f"no agent on channel {channel}")
        return channel

    def _task(self, params: dict[str, Any]) -> _Task:
        return self.channels[self._channel(params, free=False)]

    async def hello(self, params: dict[str, Any]) -> dict[str, Any]:
        answer = await _hello(params)
        answer["mode"] = "stdio" if self.daemon.ephemeral else "daemon"
        answer["tasks"] = len(self.daemon.running())
        return answer

    async def launch(self, params: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(params, free=True)
        task = await self.daemon.launch(self, channel, params)
        self.channels[channel] = task
        return {"pid": task.proc.pid, "task": task.id}

    async def attach(self, params: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel(params, free=True)
        task_id = _task_id({"task": params.get("task", "")})
        after = _number(params, "after", 0) or 0
        if after < 0:
            raise _RefusedError(_INVALID_PARAMS, "after must be a whole number")
        answer = await self.daemon.attach(self, channel, task_id, after)
        task = self.daemon.tasks.get(task_id)
        if task is not None and task.rc is None:
            self.channels[channel] = task
        return answer

    async def frame(self, params: dict[str, Any]) -> None:
        line = params.get("line")
        channel = params.get("channel")
        task = self.channels.get(channel) if isinstance(channel, int) else None
        if task is not None and isinstance(line, str):
            await task.write(line)

    async def cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        self._task(params).end()
        return {}

    async def close(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"rc": await self._task(params).close()}

    async def tasks(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tasks": self.daemon.list_tasks()}

    async def probe_agent(self, params: dict[str, Any]) -> dict[str, Any]:
        """Whether an agent's command is on this machine, on the PATH an agent here gets."""
        argv = _argv(params)
        path = _env(params).get("PATH") or (await self.env()).get("PATH")
        return {"found": shutil.which(argv[0], path=path)}

    async def shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        """The host is leaving: a stdio node ends its agents with it; a daemon's tasks go on."""
        self.stopping = True
        if self.daemon.ephemeral:
            await self.daemon.end_all()
        return {}


#: The file calls that touch a whole file -- a page read beside the file's
#: digest, a stat with one, a chunk, a write checked against one -- answered
#: in a thread, so the loop that answers the heartbeat and forwards every
#: agent's frames is not held for as long as a large file takes (measured
#: 2026-10-12: 3.5 s for a 1 GiB file, with no ping answered meanwhile).
_WHOLE_FILE = frozenset({protocol.READ, protocol.STAT, protocol.WRITE, protocol.EDIT, protocol.PULL})
#: The ones that change a file, one at a time: what a write checked is what it writes over.
_WRITERS = frozenset({protocol.WRITE, protocol.EDIT})
_WRITE_LOCK = threading.Lock()


def _apart(
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]], params: dict[str, Any], *, serial: bool
) -> dict[str, Any]:
    """``handler`` run to completion in this worker thread, on a loop of its own; a writer one at a time."""
    if not serial:
        return asyncio.run(handler(params))
    with _WRITE_LOCK:
        return asyncio.run(handler(params))


_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    protocol.PING: _ping,
    protocol.PROBE_MACHINE: _probe_machine,
    protocol.SESSION_DIR: _session_dir,
    protocol.READ: _read,
    protocol.LIST: _list,
    protocol.FIND: _find,
    protocol.GREP: _grep,
    protocol.STAT: _stat,
    protocol.WRITE: _write,
    protocol.EDIT: _edit,
    protocol.PULL: _pull,
}


def _node_handlers(node: Node) -> dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]:
    return {
        protocol.HELLO: node.hello,
        protocol.AGENT_LAUNCH: node.launch,
        protocol.AGENT_ATTACH: node.attach,
        protocol.AGENT_FRAME: node.frame,
        protocol.AGENT_CANCEL: node.cancel,
        protocol.AGENT_CLOSE: node.close,
        protocol.TASK_LIST: node.tasks,
        protocol.PROBE_AGENT: node.probe_agent,
        protocol.SHUTDOWN: node.shutdown,
    }


async def handle(frame: Any, node: Node | None = None) -> dict[str, Any] | None:
    """The answer to one decoded line, or ``None`` when nothing is owed (a notification).

    ``node`` holds the methods that need the connection's state -- its
    channels, the daemon behind it -- and is absent only where none of them
    is asked for.
    """
    if not isinstance(frame, dict) or not isinstance(frame.get("method"), str):
        return None
    method = frame["method"]
    request_id = frame.get("id")
    params = frame.get("params")
    handler = _HANDLERS.get(method) or (_node_handlers(node).get(method) if node is not None else None)
    if handler is None and method == protocol.HELLO:
        handler = _hello
    if request_id is None:
        # A notification: answered by doing, never by a frame.
        if handler is not None and isinstance(params, dict):
            with contextlib.suppress(Exception):
                await handler(params)
        return None
    try:
        if handler is None:
            raise _RefusedError(_METHOD_NOT_FOUND, f"unknown method {method!r}")
        params = params or {}
        if not isinstance(params, dict):
            raise _RefusedError(_INVALID_PARAMS, "params must be an object")
        if method in _WHOLE_FILE:
            result = await asyncio.to_thread(_apart, handler, params, serial=method in _WRITERS)
        else:
            result = await handler(params)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except _RefusedError as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}}
    except Exception as exc:  # noqa: BLE001 - the host gets a reason; the daemon keeps serving
        message = f"{type(exc).__name__}: {exc}"[:500]
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": _INTERNAL_ERROR, "message": message}}


async def _serve_one(node: Node, text: bytes) -> None:
    """Answer one line, on a task of its own, so a slow call holds nothing behind it."""
    try:
        frame = json.loads(text)
    except ValueError:
        answer: dict[str, Any] | None = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": _PARSE_ERROR, "message": "not a JSON object"},
        }
    else:
        answer = await handle(frame, node)
    if answer is not None:
        with contextlib.suppress(OSError, ValueError, ConnectionError):
            await node.send(answer)
    if node.stopping:
        node.stopped.set()


async def serve(
    reader: asyncio.StreamReader, out: BinaryIO | asyncio.StreamWriter, daemon: Daemon | None = None
) -> None:
    """Answer one host until it hangs up or says shutdown; then its tasks are left running, or ended (stdio)."""
    node = Node(out, daemon)
    answering: set[asyncio.Task[None]] = set()
    stopped = asyncio.ensure_future(node.stopped.wait())
    try:
        while True:
            reading = asyncio.ensure_future(reader.readline())
            done, _ = await asyncio.wait({reading, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if reading not in done:
                reading.cancel()
                return
            try:
                line = reading.result()
            except ValueError:
                # Longer than any request; the reader has dropped it and goes on.
                continue
            except (ConnectionError, OSError):
                return
            if not line:
                return
            text = line.strip()
            if not text:
                continue
            task = asyncio.create_task(_serve_one(node, text))
            answering.add(task)
            task.add_done_callback(answering.discard)
    finally:
        stopped.cancel()
        if answering:
            # The answers already under way get a moment to go out; a host
            # that hung up is not waited for.
            _, late = await asyncio.wait(answering, timeout=_ANSWER_DRAIN_S)
            for task in late:
                task.cancel()
        if node.daemon.ephemeral:
            await node.end_all()
        else:
            await node.detach_all()


def run_stdio() -> int:
    """Serve on this process's stdin and stdout; return when the host hangs up, the agents ended with it."""
    out = sys.stdout.buffer
    # stdout is the protocol. Anything that prints by accident -- a library, a
    # stray debug line -- goes to stderr instead of corrupting a frame.
    sys.stdout = sys.stderr

    async def main() -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=_LINE_LIMIT)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        await serve(reader, out)

    asyncio.run(main())
    return 0


async def _connection(daemon: Daemon, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await serve(reader, writer, daemon)
    finally:
        with contextlib.suppress(OSError, ConnectionError):
            writer.close()
            await writer.wait_closed()


async def run_daemon_async(root: Path, *, idle_s: float = IDLE_EXIT_S) -> int:
    """Serve on the socket under ``root`` until idle for ``idle_s`` or told to stop; 0 when another daemon already does."""
    run_dir = own_dir(root / RUN_DIR)
    sock = socket_path(root)
    if sock.parent != run_dir:
        own_dir(sock.parent)
    lock = (run_dir / f"{bundle.install_name()}.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return 0
    try:
        sock.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        # Whatever is there is not this daemon's to remove -- in a directory
        # of this user's own, root's -- and the daemon does not listen beside it.
        lock.close()
        raise PermissionError(
            f"{sock} is in the way of the daemon and cannot be removed: {exc.strerror or exc}"
        ) from None
    pid_path(root).write_text(str(os.getpid()), encoding="utf-8")
    daemon = Daemon(root)
    daemon.sweep()
    server = await asyncio.start_unix_server(partial(_connection, daemon), path=str(sock), limit=_LINE_LIMIT)
    os.chmod(sock, 0o600)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    async def _idle_watch() -> None:
        while not stop.is_set():
            await asyncio.sleep(min(_IDLE_CHECK_S, max(0.1, idle_s / 2)))
            quiet = not daemon.connections and not daemon.running()
            if quiet and time.monotonic() - daemon.last_activity > idle_s:
                stop.set()

    watcher = asyncio.create_task(_idle_watch())
    try:
        await stop.wait()
    finally:
        watcher.cancel()
        server.close()
        # Told to stop (a signal) with agents running: they end with the
        # daemon, as a deliberate stop should, and their exits go out on the
        # connections still open before those are closed; an idle exit has
        # neither. ``wait_closed`` waits for the connections, so they are hung
        # up first.
        await daemon.end_all()
        await asyncio.sleep(0)
        for node in list(daemon.connections):
            node.hang_up()
        with contextlib.suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=_ANSWER_DRAIN_S)
        with contextlib.suppress(FileNotFoundError):
            sock.unlink()
        with contextlib.suppress(FileNotFoundError):
            pid_path(root).unlink()
        lock.close()
    return 0


def run_daemon(root: Path, *, idle_s: float = IDLE_EXIT_S) -> int:
    """Run the daemon of this install under ``root`` in this process; see :func:`run_daemon_async`."""
    sys.stdout = sys.stderr
    return asyncio.run(run_daemon_async(root, idle_s=idle_s))


__all__ = [
    "IDLE_EXIT_S",
    "LOST_RC",
    "Daemon",
    "Node",
    "capture_login_env",
    "handle",
    "run_daemon",
    "run_daemon_async",
    "pid_path",
    "run_stdio",
    "serve",
    "socket_path",
]
