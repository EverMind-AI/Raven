"""The host's side of a Raven node: one live link per registered machine.

A node is reached over ssh (:func:`raven.acp_client.remote.ssh_command`): the
relay the ssh runs bridges it to the daemon on the machine, starting one when
none is up. It is
spoken to with the client an ACP agent is (:class:`raven.acp_client.client.AcpClient`),
because the framing is the same. The link is kept for as long as the machine
is in use: every file call to it, every agent started there and every probe
of it travels on that one connection. A link that has carried nothing past
:data:`IDLE_CLOSE_S` and runs no agent is closed the next time any node is
asked for, and its ssh goes with it.

The link is watched from here, not by ssh: a ``node/ping`` every
:data:`HEARTBEAT_S` seconds, and one unanswered for :data:`HEARTBEAT_LOST_S`
means the link is gone -- not the agents. They run on under the machine's
daemon, which keeps their frames; each agent's endpoint here waits, holding
what the host writes meanwhile, and the link is tried again on a backoff
(:data:`RECONNECT_BACKOFF_S`) for as long as any agent is waiting. A link
that comes back attaches each agent again by its task id, from the frame the
host last read, and the turn goes on as if nothing had happened. Only a task
the machine no longer knows ends an endpoint with the sentence that says so.

An MCP server the host lends an agent on the machine is carried on the link
too: the agent runs the node's bridge as a stdio server, the daemon tells this
side a stream opened (``mcp/open``). This side checks the authenticated task
against the task bound to the dispatch's grant (:meth:`NodePool.lend`), then
connects to its endpoint here and pumps the frames both ways in pieces (``mcp/frame``),
and the stream ends with the dispatch, the link or either end.

Before a node is used, ``node/hello`` has to show the same protocol and the
same code as this Raven (:func:`raven.node.bundle.digest`): a node that differs
would answer a file call differently from the tool it stands in for. Every
failure here is one sentence that names the machine, never its address, and
says what to do.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from loguru import logger

from raven.acp_client import remote
from raven.acp_client.client import AcpClient
from raven.acp_client.protocol import AcpConnectionError, AcpError, AcpRemoteError, AcpTimeoutError
from raven.mcp.bridge import iter_raw_frames, unix_sockets_available
from raven.node import bundle, protocol
from raven.ops import remote_path

#: Where a machine keeps its nodes when its registry row names nowhere else
#: (``node_dir``). ``~`` is the registered user's home there.
DEFAULT_NODE_DIR = "~/.raven-node"

#: How long a link may carry nothing, with no agent on it, before the next acquire closes it.
IDLE_CLOSE_S = 600.0

#: How often the host pings a node it holds a link to.
HEARTBEAT_S = 5.0
#: A ping unanswered this long means the link is lost: three heartbeats.
HEARTBEAT_LOST_S = 15.0
#: The waits before each try at a lost link, in order; after the last, the next call tries again.
RECONNECT_BACKOFF_S = (2.0, 4.0, 8.0, 16.0)

#: What an agent's connection reads as its exit code when its link went, not the agent.
LOST_RC = -1

_HELLO_TIMEOUT_S = 60.0
_CALL_TIMEOUT_S = 120.0
_LAUNCH_TIMEOUT_S = 60.0
_ATTACH_TIMEOUT_S = 30.0
#: How many frames an endpoint holds for a link that is down before it gives up on it.
_PENDING_FRAMES = 1000
_CANCEL_TIMEOUT_S = 5.0

#: The node's own refusal codes, as the file tools hear of them (``NodeCallError.reason``).
_REASONS = {
    protocol.CHANGED: "changed",
    protocol.NOT_FOUND: "not-found",
    protocol.BUSY: "busy",
    protocol.UNKNOWN_TASK: "unknown-task",
}

#: How the machine's shell ends when the node's Python is not there: 127 from
#: dash and bash 5, 126 from bash 3.2 (macOS's ``/bin/sh``), which reports an
#: ``exec`` of a missing path as "cannot execute" (measured 2026-10-10).
_MISSING_RCS = frozenset({remote.NOT_FOUND_RC, 126})


class NodeError(remote_path.NodeCallError):
    """A call to a machine that could not be made. The message is safe to show."""


def node_dir(target: remote.Machine) -> str:
    """The directory on ``target`` that holds its nodes."""
    return str(target.field("node_dir") or "").strip().rstrip("/") or DEFAULT_NODE_DIR


def node_python(target: remote.Machine, install_name: str) -> str:
    """The Python of the node of ``install_name`` on ``target``."""
    return f"{node_dir(target)}/{install_name}/bin/python"


def install_hint(target: remote.Machine) -> str:
    """The command that puts this Raven's node on ``target``."""
    return f"raven ops connection install-node {target.id}"


_TASK_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._@-")


def task_id(name: str) -> str:
    """A task id for an agent called ``name``: its spelling the machine accepts, and a tail nobody repeats."""
    safe = "".join(ch if ch in _TASK_CHARS else "-" for ch in name.strip())[:40].strip("-.") or "agent"
    return f"{safe}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class MachineStatus:
    """What the listing says about a machine's link, with no round trip."""

    state: str
    """``connected`` (agents running), ``idle`` (a link, no agent), ``lost`` (the link is down, agents
    wait for it), ``off`` (no link), or ``error``."""
    detail: str = ""
    """The one sentence for ``error`` and ``lost``."""
    agents: int = 0
    version: str = ""
    """The node's Raven version, when a link is up."""

    def said(self) -> str:
        """The parenthetical a listing shows, or ``""`` for a machine nothing has reached yet."""
        if self.state == "connected":
            return f"connected, {self.agents} agent{'s' if self.agents != 1 else ''} running"
        if self.state == "idle":
            return "connected, idle"
        if self.state == "lost":
            tasks = f"{self.agents} task{'s continue' if self.agents != 1 else ' continues'}"
            return f"link lost: {self.detail}; {tasks} on the machine; reconnecting"
        if self.state == "error":
            return f"connection error: {self.detail}"
        return ""


class ChannelEndpoint:
    """An agent a node started, as the far end of an :class:`AcpClient`.

    The client reads and writes it as it would a child process's pipes; what
    it gets are the ``agent/frame`` and ``agent/stderr`` notifications of one
    channel, each read back as a pipe gives a line, with its line break (a
    blank line is a frame too: a CLI agent's answer is its lines as printed),
    and what it writes goes back the same way -- a frame per line
    (``write``), or a CLI agent's whole prompt and the end of its stdin
    (``send_input``, ``agent/stdin``). ``end`` is
    ``agent/cancel``: the node ends the agent's whole process tree, and the
    ``agent/exit`` that follows is what settles ``wait``. A link that goes
    before the agent does leaves the endpoint waiting (``detach``): the agent
    runs on under the machine's daemon, what the client writes meanwhile is
    held, and once the pool has the link back it attaches the task again
    (``reattach``) and the held frames go out. Only a task the machine no
    longer knows ends the endpoint with the sentence that says so, which the
    client then raises instead of an exit code.
    """

    def __init__(
        self, link: AcpClient, channel: int, *, target: remote.Machine, agent: str, task: str | None = None
    ) -> None:
        self._link = link
        self.channel = channel
        self.target = target
        self.agent = agent
        self.task = task or task_id(agent)
        self.pid: int | None = None
        self.received = 0
        """How many of the agent's frames reached here: where an attach goes on from."""
        self.watchers: list[Callable[[int], None]] = []
        """Told, after each frame the connection has dealt with, how many it has read: what a record of the
        run keeps until its prompt is out, so a process that comes after this one attaches from there and reads
        the turn whole (:mod:`raven.acp_client.remote_runs`)."""
        self._handing = 0
        self._frames: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue()
        self._stderr: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._rc: int | None = None
        self._lost: str | None = None
        self._detached: str | None = None
        self._pending: list[tuple[str, dict[str, Any]]] = []
        """What was written while the link was down, by method, to go out on the next link in order."""
        self._cancel_wanted = False
        self._done = asyncio.Event()

    @property
    def returncode(self) -> int | None:
        if self._rc is not None:
            return self._rc
        return LOST_RC if self._lost is not None else None

    def describe(self) -> str:
        pid = f", pid {self.pid}" if self.pid else ""
        return f"task {self.task} on machine {self.target.label}{pid}"

    @property
    def detached(self) -> str | None:
        """Why the endpoint is waiting for its link, or ``None`` while it has one."""
        return self._detached

    @property
    def stays(self) -> bool:
        """Whether the agent stays with its machine when this endpoint is ended: this process is stopping."""
        return remote.leaving()

    def consumed(self) -> None:
        """The connection has dealt with the frame last handed over: the watchers hear how many it has read."""
        for watcher in list(self.watchers):
            try:
                watcher(self._handing)
            except Exception as exc:  # noqa: BLE001 - a record that cannot be kept must not stop the frames
                logger.warning("raven node: a watcher of {} failed: {}", self.describe(), exc)

    async def write(self, data: bytes) -> None:
        if self.returncode is not None:
            raise BrokenPipeError(f"the agent on {self.describe()} has ended")
        line = data.decode("utf-8", "replace").rstrip("\r\n")
        await self._send(protocol.AGENT_FRAME, {"line": line})

    async def send_input(self, text: str, *, eof: bool = False) -> None:
        """``text`` to the agent's stdin as it is, and the end of its stdin when ``eof``.

        For an agent that reads a prompt rather than frames: a CLI agent takes
        its task this way when its command names no placeholder for it, and
        every CLI agent is told where its stdin ends, since the node opens one
        for each agent it starts and a command that reads to the end would
        otherwise wait for it.
        """
        if self.returncode is not None:
            raise BrokenPipeError(f"the agent on {self.describe()} has ended")
        await self._send(protocol.AGENT_STDIN, {"data": text, "eof": eof})

    async def _send(self, method: str, params: dict[str, Any]) -> None:
        if self._detached is not None or not self._link.alive:
            # Down, or gone and not yet said so by the heartbeat: it waits for the next link.
            self._hold(method, params)
            return
        try:
            await self._link.notify(method, {"channel": self.channel, **params})
        except AcpConnectionError:
            # The pool's heartbeat will say the link is lost; it waits for the next one.
            self._hold(method, params)

    def _hold(self, method: str, params: dict[str, Any]) -> None:
        if len(self._pending) >= _PENDING_FRAMES:
            raise BrokenPipeError(
                f"the link to machine {self.target.label} has been down too long: {len(self._pending)} frames wait"
            )
        self._pending.append((method, params))

    async def readline(self) -> bytes:
        """The next line the agent wrote, with its line break, as a pipe gives it; ``b""`` once the agent is gone."""
        item = await self._frames.get()
        if item is None:
            return b""
        self._handing, line = item
        return line

    async def readerr(self) -> bytes:
        item = await self._stderr.get()
        return b"" if item is None else item

    async def end(self) -> None:
        """End the agent and what it started, through the node; while the link is down, as soon as it is back.

        Not while this process is stopping (:func:`raven.acp_client.remote.leave_agents_running`):
        then the agent stays with the machine's daemon, this end is only the
        endpoint's, and the run is picked up by the next process.

        Wanted until delivered: a link that dies under the request, or that
        has died and not been said so yet, keeps the cancel for the next
        link, and only the node's answer settles it -- the cancel taken, or
        nothing there to cancel.
        """
        if self.returncode is not None:
            return
        if self.stays:
            self._lost = f"left running on {self.describe()}: this computer stopped"
            self._settle()
            return
        self._cancel_wanted = True
        if self._detached is not None or not self._link.alive:
            return
        try:
            await self._link.request(protocol.AGENT_CANCEL, {"channel": self.channel}, timeout=_CANCEL_TIMEOUT_S)
        except AcpRemoteError:
            # The node answered: no such channel or task there, so nothing is left to cancel.
            self._cancel_wanted = False
        except AcpError:
            # The link is going (the pool parks this endpoint) or slow: the cancel goes with the next link.
            return
        else:
            self._cancel_wanted = False

    async def wait(self) -> int | None:
        await self._done.wait()
        return self.returncode

    def ended(self) -> str | None:
        return self._lost

    def release(self) -> None:
        """Nothing is held: the link is the pool's."""

    # --- fed by the pool, off the link's notifications ---------------------------

    def feed_frame(self, line: Any, n: Any = None) -> None:
        if isinstance(line, str) and not self._done.is_set():
            self.received = n if isinstance(n, int) and not isinstance(n, bool) else self.received + 1
            self._frames.put_nowait((self.received, line.encode("utf-8") + b"\n"))

    def feed_stderr(self, text: Any) -> None:
        if isinstance(text, str) and not self._done.is_set():
            self._stderr.put_nowait(text.encode("utf-8") + b"\n")

    def exited(self, rc: Any, reason: Any = None) -> None:
        if self._done.is_set():
            return
        self._rc = rc if isinstance(rc, int) and not isinstance(rc, bool) else LOST_RC
        if isinstance(reason, str) and reason:
            # The node's own sentence for an end it did not watch -- a daemon
            # restarted under the agent (``raven.node.server.Daemon.recover``)
            # -- handed on in place of the bare exit code, as a lost link's is.
            self._lost = reason
        self._settle()

    @property
    def offline(self) -> bool:
        """Whether nothing sent now reaches the agent: its link is known to be gone, or this endpoint is parked
        for the next one. What a caller that would wait for the agent's answer reads first."""
        return self._detached is not None or not self._link.alive

    def lost(self, reason: str) -> None:
        if self._done.is_set():
            return
        self._lost = reason
        self._settle()

    def detach(self, reason: str) -> None:
        """The link went; the agent did not. Wait for the pool to bring a link back."""
        if not self._done.is_set():
            self._detached = reason

    async def reattach(self, link: AcpClient, channel: int) -> None:
        """The task is on ``channel`` of ``link`` again: send what waited, and the cancel if one was asked.

        A frame leaves the held list only once the link took it: a link that
        goes under the replay leaves the rest held for the next one, and the
        pool parks the endpoint again with them.
        """
        self._link, self.channel = link, channel
        self._detached = None
        while self._pending:
            method, params = self._pending[0]
            await self._link.notify(method, {"channel": channel, **params})
            self._pending.pop(0)
        if self._cancel_wanted:
            await self.end()

    def _settle(self) -> None:
        self._frames.put_nowait(None)
        self._stderr.put_nowait(None)
        self._done.set()


class _HostStream:
    """The host's end of one MCP stream: the connection into the endpoint it opened for the server, and the pump out of it.

    What the bridge on the machine is to the agent, this is to the endpoint:
    the pieces off the link are put together and written into the socket,
    and every frame the socket yields goes out as pieces. A stream is the
    dispatch's (``lend``), so it ends with the dispatch's endpoints.
    """

    def __init__(self, stream_id: int, lend: str, server: str, writer: asyncio.StreamWriter) -> None:
        self.id = stream_id
        self.lend = lend
        self.server = server
        self.writer = writer
        self.pump: asyncio.Task[None] | None = None
        self.parts: list[str] = []
        self.size = 0


@dataclass
class _LentServers:
    """Host endpoints granted by one dispatch to one daemon-authenticated agent task."""

    task: str
    paths: dict[str, str]


@dataclass
class _Node:
    client: AcpClient
    target: remote.Machine
    log: Path
    """The ssh log of this link, where ssh's own words go."""
    hello: dict[str, Any]
    channels: dict[int, ChannelEndpoint]
    used: float = field(default_factory=time.monotonic)
    calls: int = 0
    """Requests on their way to the node, not yet answered: use, to the idle close, as much as an answer is."""
    heartbeat: asyncio.Task[None] | None = None
    next_channel: int = 1
    streams: dict[int, _HostStream] = field(default_factory=dict)
    """The MCP streams open on this link, by the daemon's id for them."""

    @property
    def home(self) -> str:
        """The login home on the machine, as the node reported it at its handshake."""
        return str(self.hello.get("home") or "")


class NodePool:
    """Live links, at most one per machine, shared by every caller in this process."""

    def __init__(
        self,
        *,
        idle_close_s: float = IDLE_CLOSE_S,
        heartbeat_s: float = HEARTBEAT_S,
        lost_after_s: float = HEARTBEAT_LOST_S,
        backoff_s: tuple[float, ...] = RECONNECT_BACKOFF_S,
    ) -> None:
        self._nodes: dict[str, _Node] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._errors: dict[str, str] = {}
        self._lent: dict[str, dict[str, _LentServers]] = {}
        """Per machine and dispatch, endpoints bound to the agent task the host actually launched."""
        self._reconnects: dict[str, asyncio.Task[None]] = {}
        self._parked: dict[str, list[ChannelEndpoint]] = {}
        """The endpoints waiting for a machine's link to come back, by machine."""
        self._idle_close_s = idle_close_s
        self._heartbeat_s = heartbeat_s
        self._lost_after_s = lost_after_s
        self._backoff_s = tuple(backoff_s)
        self._closing = False

    # --- what a caller asks of a machine -------------------------------------------

    async def call(
        self, machine_id: str, method: str, params: dict[str, Any], *, timeout: float = _CALL_TIMEOUT_S
    ) -> dict[str, Any]:
        """``method`` on the node of ``machine_id``, starting one if none is live."""
        node = await self._acquire(machine_id)
        label = node.target.label
        node.calls += 1
        try:
            result = await node.client.request(method, params, timeout=timeout)
        except AcpRemoteError as exc:
            raise NodeError(
                f"the Raven node on machine {label} refused {method}: {exc.message}", reason=_REASONS.get(exc.code)
            ) from None
        except AcpTimeoutError:
            await self._lost(
                machine_id, node, f"the Raven node on machine {label} did not answer {method} within {timeout:.0f}s"
            )
            raise NodeError(
                f"the Raven node on machine {label} did not answer {method} within {timeout:.0f}s"
            ) from None
        except AcpConnectionError as exc:
            said = self._ended(node.target, node.log, exc)
            await self._lost(machine_id, node, said)
            raise NodeError(said) from None
        finally:
            node.calls -= 1
            node.used = time.monotonic()
        return result if isinstance(result, dict) else {}

    async def hello(self, machine_id: str) -> dict[str, Any]:
        """The handshake of the node of ``machine_id``: its protocol, digest, Python and home."""
        return await self.call(machine_id, protocol.HELLO, {"protocol": protocol.PROTOCOL})

    async def home(self, machine_id: str) -> str:
        """The login home on ``machine_id``, as its node reported it; starts the node if none is live."""
        node = await self._acquire(machine_id)
        return node.home

    async def session_dir(self, machine_id: str, path: str) -> tuple[str, float]:
        """Make ``path`` on the machine and return it resolved there, with the machine's clock as it was made."""
        got = await self.call(machine_id, protocol.SESSION_DIR, {"path": path})
        resolved = str(got.get("path") or "")
        if not resolved.startswith("/"):
            label = (await self._acquire(machine_id)).target.label
            raise NodeError(f"the Raven node on machine {label} did not say where it made {path}")
        since = got.get("since")
        return resolved, float(since) if isinstance(since, (int, float)) else 0.0

    async def probe_machine(self, machine_id: str) -> dict[str, Any]:
        """One look at the machine: free disk, Python, shell, uv, load."""
        return await self.call(machine_id, protocol.PROBE_MACHINE, {})

    async def probe_agent(self, machine_id: str, command: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Whether ``command`` is on the machine, on the PATH an agent there gets (``env`` may override it)."""
        params: dict[str, Any] = {"command": command}
        if env and isinstance(env.get("PATH"), str):
            params["env"] = {"PATH": env["PATH"]}
        return await self.call(machine_id, protocol.PROBE_AGENT, params)

    async def stat(self, machine_id: str, path: str, *, roots: Sequence[str], hash: bool = False) -> dict[str, Any]:
        """What is at ``path`` on the machine: exists, kind, size, mtime, and its digest when asked."""
        return await self.call(machine_id, protocol.STAT, {"path": path, "roots": list(roots), "hash": hash})

    async def write_file(self, machine_id: str, path: str, content: str, *, roots: Sequence[str]) -> None:
        """Write ``content`` to ``path`` on the machine, whole, inside ``roots``; the directories above it are made."""
        params = {"path": path, "content": content, "roots": list(roots)}
        answer = await self.call(machine_id, protocol.WRITE, params)
        said = str(answer.get("text") or "")
        # A refusal is the tool's own sentence ("Error: ...", "Error writing file: ..."), never an exception.
        if answer.get("ok") is False or said.startswith("Error"):
            label = (await self._acquire(machine_id)).target.label
            reason = said.split(": ", 1)[1] if ": " in said else said
            raise NodeError(f"{path} could not be written on machine {label}: {reason.strip() or 'refused'}")

    async def tasks(self, machine_id: str) -> list[dict[str, Any]]:
        """Every task the machine's daemon runs or remembers: id, state, exit code, times, frames."""
        answer = await self.call(machine_id, protocol.TASK_LIST, {})
        rows = answer.get("tasks")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    async def pull_file(self, machine_id: str, remote_file: str, local: Path, *, roots: Sequence[str]) -> int:
        """Bring ``remote_file`` from the machine to ``local``, in chunks, checked whole; the bytes received.

        Written beside its destination first and moved into place only when
        the digest the node gave with the first chunk matches, so a file that
        did not arrive whole is never left looking as if it had.
        """
        local.parent.mkdir(parents=True, exist_ok=True)
        part = local.with_name(local.name + ".raven-part")
        hasher = hashlib.sha256()
        expected: str | None = None
        offset = 0
        try:
            with part.open("wb") as handle:
                while True:
                    params = {"path": remote_file, "offset": offset, "length": protocol.CHUNK, "roots": list(roots)}
                    got = await self.call(machine_id, protocol.PULL, params)
                    data = base64.b64decode(str(got.get("data") or ""))
                    if offset == 0 and isinstance(got.get("sha256"), str):
                        expected = got["sha256"]
                    handle.write(data)
                    hasher.update(data)
                    offset += len(data)
                    if got.get("eof") or not data:
                        break
            if expected is not None and hasher.hexdigest() != expected:
                label = (await self._acquire(machine_id)).target.label
                raise NodeError(f"{remote_file} on machine {label} did not arrive whole; what arrived was removed")
            os.replace(part, local)
        finally:
            part.unlink(missing_ok=True)
        return offset

    async def open_channel(
        self, machine_id: str, *, name: str, command: str, cwd: str, env: Mapping[str, str], task: str | None = None
    ) -> ChannelEndpoint:
        """Start ``command`` on the machine, in ``cwd`` with ``env`` over its login environment.

        The endpoint returned is what an :class:`AcpClient` runs over
        (``AcpClient.over``). ``env`` travels on the link, never on a command
        line, and the link is not journaled. ``task`` names the run on the
        machine, where its record outlives this link; one is made up from
        ``name`` when the caller has none.
        """
        node = await self._acquire(machine_id)
        label = node.target.label
        channel = node.next_channel
        node.next_channel += 1
        task = task or task_id(name)
        endpoint = ChannelEndpoint(node.client, channel, target=node.target, agent=name, task=task)
        # Registered before the launch, so a frame the agent writes at once has somewhere to go.
        node.channels[channel] = endpoint
        params = {"channel": channel, "task": task, "command": command, "cwd": cwd, "env": dict(env)}
        try:
            started = await node.client.request(protocol.AGENT_LAUNCH, params, timeout=_LAUNCH_TIMEOUT_S)
        except AcpRemoteError as exc:
            node.channels.pop(channel, None)
            raise NodeError(self._refused_launch(node, name, exc)) from None
        except AcpTimeoutError:
            node.channels.pop(channel, None)
            said = f"the Raven node on machine {label} did not answer {protocol.AGENT_LAUNCH} within {_LAUNCH_TIMEOUT_S:.0f}s"
            await self._lost(machine_id, node, said)
            raise NodeError(said) from None
        except AcpConnectionError as exc:
            node.channels.pop(channel, None)
            said = self._ended(node.target, node.log, exc)
            await self._lost(machine_id, node, said)
            raise NodeError(said) from None
        if isinstance(started, dict) and isinstance(started.get("pid"), int):
            endpoint.pid = started["pid"]
        node.used = time.monotonic()
        logger.info("acp agent {!r}: started on {}", name, endpoint.describe())
        return endpoint

    async def attach(
        self, machine_id: str, task: str, *, name: str, after: int = 0
    ) -> tuple[ChannelEndpoint, dict[str, Any]]:
        """``task`` on the machine, onto a channel of its link, from the frame past the ``after``-th.

        What a process that comes after the one that started the agent uses
        to pick the run up: the daemon replays the frames past the record's
        count -- the turn whole, from where its prompt went out -- and hands
        the rest over as it comes, or, for a task that has ended, replays its
        record and says so. The endpoint returned is what an
        :class:`AcpClient` runs over; the answer says the pid, the frames
        and the exit code (``None`` while the agent runs).
        """
        node = await self._acquire(machine_id)
        label = node.target.label
        channel = node.next_channel
        node.next_channel += 1
        endpoint = ChannelEndpoint(node.client, channel, target=node.target, agent=name, task=task)
        endpoint.received = after
        node.channels[channel] = endpoint
        params = {"channel": channel, "task": task, "after": after}
        try:
            answer = await node.client.request(protocol.AGENT_ATTACH, params, timeout=_ATTACH_TIMEOUT_S)
        except AcpRemoteError as exc:
            node.channels.pop(channel, None)
            raise NodeError(
                f"machine {label} no longer runs {name!r} (task {task}): {exc.message}", reason=_REASONS.get(exc.code)
            ) from None
        except AcpTimeoutError:
            node.channels.pop(channel, None)
            said = f"the Raven node on machine {label} did not answer {protocol.AGENT_ATTACH}"
            await self._lost(machine_id, node, said)
            raise NodeError(said) from None
        except AcpConnectionError as exc:
            node.channels.pop(channel, None)
            said = self._ended(node.target, node.log, exc)
            await self._lost(machine_id, node, said)
            raise NodeError(said) from None
        answer = answer if isinstance(answer, dict) else {}
        if isinstance(answer.get("pid"), int):
            endpoint.pid = answer["pid"]
        node.used = time.monotonic()
        logger.info("acp agent {!r}: picked up on {} from frame {}", name, endpoint.describe(), after)
        return endpoint, answer

    def status(self, machine_id: str) -> MachineStatus:
        """The link's state, with no round trip: for a listing."""
        node = self._nodes.get(machine_id)
        if node is not None and node.client.alive:
            agents = len(node.channels)
            version = str(node.hello.get("version") or "")
            return MachineStatus("connected" if agents else "idle", agents=agents, version=version)
        if waiting := self._parked.get(machine_id):
            said = self._errors.get(machine_id, "").removesuffix("; reconnecting")
            return MachineStatus("lost", said, agents=len(waiting))
        if said := self._errors.get(machine_id):
            return MachineStatus("error", said)
        return MachineStatus("off")

    def waiting(self, machine_id: str) -> int:
        """How many agents wait for the machine's link to come back."""
        return len(self._parked.get(machine_id, ()))

    def live(self) -> list[str]:
        """The machines that have a node running for this process."""
        return [machine_id for machine_id, node in self._nodes.items() if node.client.alive]

    async def close(self, machine_id: str) -> None:
        self._stop_reconnect(machine_id)
        await self._drop(machine_id)

    # --- MCP lent across the link -----------------------------------------------------

    def lend(self, machine_id: str, lend: str, paths: Mapping[str, str], *, task: str) -> None:
        """The host's endpoints for dispatch ``lend`` on the machine: by server name, the socket a stream of it pumps into.

        Said before the agent is handed the bridge stanzas, so a bridge that
        opens at once finds its server. The endpoints themselves are the
        caller's (``raven.mcp.endpoint``); this is the pairing table. ``task``
        is taken from the acquired agent's endpoint, never from its bridge.
        """
        if not isinstance(task, str) or not task:
            raise ValueError("an MCP grant must name its agent task")
        self._lent.setdefault(machine_id, {})[lend] = _LentServers(task, dict(paths))

    async def unlend(self, machine_id: str, lend: str) -> None:
        """The dispatch is over: its pairing is forgotten, and the streams on it ended, the bridges told."""
        self._lent.get(machine_id, {}).pop(lend, None)
        node = self._nodes.get(machine_id)
        if node is None:
            return
        for stream in [s for s in node.streams.values() if s.lend == lend]:
            await self._end_stream(node, stream, "the dispatch ended", tell=True)

    async def bridge_argv(self, machine_id: str) -> list[str] | None:
        """What an agent on the machine runs as a stdio MCP server to reach a host endpoint; ``None`` from a node with no daemon."""
        node = await self._acquire(machine_id)
        argv = node.hello.get("bridge")
        return [str(part) for part in argv] if isinstance(argv, list) and argv else None

    async def _on_stream(self, machine_id: str, method: str, params: dict[str, Any]) -> None:
        """One ``mcp/*`` notification off the link: a stream opened, a piece of a frame, or a stream over."""
        node = self._nodes.get(machine_id)
        stream_id = params.get("stream")
        if node is None or not isinstance(stream_id, int) or isinstance(stream_id, bool):
            return
        if method == protocol.MCP_OPEN:
            await self._open_stream(node, stream_id, params)
            return
        stream = node.streams.get(stream_id)
        if stream is None:
            return
        if method == protocol.MCP_CLOSE:
            await self._end_stream(node, stream, "closed by the machine", tell=False)
            return
        text = params.get("text")
        if not isinstance(text, str):
            return
        stream.parts.append(text)
        stream.size += len(text)
        if stream.size > protocol.MCP_MAX_FRAME:
            await self._end_stream(node, stream, f"a frame exceeds {protocol.MCP_MAX_FRAME} bytes", tell=True)
            return
        if not params.get("end", True):
            return
        frame = "".join(stream.parts).encode("utf-8")
        stream.parts, stream.size = [], 0
        try:
            stream.writer.write(frame + b"\n")
            await stream.writer.drain()
        except (ConnectionError, OSError):
            await self._end_stream(node, stream, "the host's endpoint closed", tell=True)

    async def _open_stream(self, node: _Node, stream_id: int, params: dict[str, Any]) -> None:
        lend, server, task = params.get("lend"), params.get("server"), params.get("task")
        granted = self._lent.get(node.target.id, {}).get(str(lend))
        # The daemon authenticates the task; the bridge chooses lend/server.
        # Those names cannot authorize access to another task's host endpoint.
        path = granted.paths.get(str(server)) if granted is not None and granted.task == task else None
        if path is None:
            await self._tell(
                node, protocol.MCP_CLOSE, {"stream": stream_id, "reason": f"server {server!r} is not lent to that run"}
            )
            return
        if not unix_sockets_available():
            await self._tell(
                node,
                protocol.MCP_CLOSE,
                {"stream": stream_id, "reason": "the host does not support Unix-socket MCP endpoints"},
            )
            return
        try:
            reader, writer = await asyncio.open_unix_connection(path)
        except OSError as exc:
            await self._tell(
                node,
                protocol.MCP_CLOSE,
                {"stream": stream_id, "reason": f"the host's endpoint for {server!r} is gone: {exc}"},
            )
            return
        # An unlend or a replaced link can run while the socket is opening.
        # Never register a stream under an authorization that ended meanwhile.
        if (
            self._lent.get(node.target.id, {}).get(str(lend)) is not granted
            or self._nodes.get(node.target.id) is not node
            or not node.client.alive
        ):
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
            await self._tell(node, protocol.MCP_CLOSE, {"stream": stream_id, "reason": "the dispatch or link ended"})
            return
        stream = _HostStream(stream_id, str(lend), str(server), writer)
        node.streams[stream_id] = stream
        stream.pump = asyncio.create_task(self._pump_out(node, stream, reader), name=f"mcp-stream-{stream_id}")

    async def _pump_out(self, node: _Node, stream: _HostStream, reader: asyncio.StreamReader) -> None:
        """Every frame the endpoint yields, out on the link in pieces; its end, or a frame too large, ends the stream."""
        reason = "the host's endpoint closed"
        try:
            async for frame in iter_raw_frames(reader, max_frame_bytes=protocol.MCP_MAX_FRAME):
                for text, end in protocol.pieces(frame):
                    await node.client.notify(protocol.MCP_FRAME, {"stream": stream.id, "text": text, "end": end})
        except ValueError as exc:
            reason = str(exc)
        except (AcpConnectionError, ConnectionError, OSError):
            # The link is gone: its streams go with it, and nothing can be told.
            node.streams.pop(stream.id, None)
            stream.writer.close()
            return
        if node.streams.get(stream.id) is stream:
            await self._end_stream(node, stream, reason, tell=True)

    async def _end_stream(self, node: _Node, stream: _HostStream, reason: str, *, tell: bool) -> None:
        node.streams.pop(stream.id, None)
        if stream.pump is not None and stream.pump is not asyncio.current_task():
            stream.pump.cancel()
        with contextlib.suppress(OSError, ConnectionError):
            stream.writer.close()
        if tell:
            await self._tell(node, protocol.MCP_CLOSE, {"stream": stream.id, "reason": reason})

    @staticmethod
    async def _tell(node: _Node, method: str, params: dict[str, Any]) -> None:
        with contextlib.suppress(AcpConnectionError, ConnectionError, OSError, ValueError):
            await node.client.notify(method, params)

    async def close_all(self) -> None:
        """Close every link. The agents on the machines run on under their daemons."""
        self._closing = True
        try:
            for machine_id in list(self._reconnects):
                self._stop_reconnect(machine_id)
            for machine_id in list(self._nodes):
                await self._drop(machine_id)
        finally:
            self._closing = False

    # --- the link ------------------------------------------------------------------

    async def _acquire(self, machine_id: str) -> _Node:
        await self._close_idle(keep=machine_id)
        lock = self._locks.setdefault(machine_id, asyncio.Lock())
        async with lock:
            node = self._nodes.get(machine_id)
            if node is not None and node.client.alive:
                return node
            if node is not None:
                # Gone before the heartbeat said so: its agents wait, as they
                # would have for the heartbeat, and the new link brings them.
                await self._drop(machine_id, f"the Raven node on machine {node.target.label} stopped")
            try:
                node = await self._launch(machine_id)
            except NodeError as exc:
                self._errors[machine_id] = str(exc)
                if self._parked.get(machine_id):
                    # Agents wait for this machine: the link is tried again
                    # on the backoff, as after a heartbeat loss, and not only
                    # by whatever call comes next.
                    self._errors[machine_id] = f"{exc}; reconnecting"
                    self._retry_later(machine_id)
                raise
            self._nodes[machine_id] = node
            self._errors.pop(machine_id, None)
            node.heartbeat = asyncio.create_task(self._heartbeat(machine_id, node), name=f"node-heartbeat-{machine_id}")
            if self._parked.get(machine_id):
                # Whichever path made this link, the agents parked for the
                # machine ride on it; a link that goes under the attach is
                # dropped again, and the call that asked for it hears why.
                await self._reattach(machine_id)
                if self._nodes.get(machine_id) is not node:
                    said = self._errors.get(machine_id, "").removesuffix("; reconnecting")
                    raise NodeError(said or f"the link to machine {node.target.label} was lost")
        return node

    def _prepare(self, machine_id: str) -> tuple[remote.Machine, Path, list[str], str]:
        """The machine, its ssh log (emptied), the ssh argv and the install name. Blocking: reads the registry."""
        target = remote.machine(machine_id)
        install_name = bundle.install_name()
        log = remote.ssh_log_path(f"raven-node@{target.id}")
        try:
            # Emptied per launch, so a failure is read from this launch's lines.
            log.write_text("", encoding="utf-8")
        except OSError:
            pass
        # ``-I``: the login shell's PYTHONPATH or PYTHONHOME would otherwise
        # reach the node, and a shadow ``raven`` on the former answers the
        # handshake with another digest that no reinstall cures. Started from
        # the home directory, which the ssh login lands in: the node makes no
        # directory of its own.
        # ``exec``: the shell that reads the line becomes the node, so the ssh
        # session has one process at its far end and a signal to it reaches
        # the node, not a wrapper.
        # ``--attach``: the relay, which bridges this ssh to the machine's
        # daemon and starts one when none is up; the daemon, not the ssh,
        # owns the agents.
        command = (
            f"exec {remote.shell_path(node_python(target, install_name))} -I -m raven.node --attach "
            f"--dir {remote.shell_path(node_dir(target))}"
        )
        try:
            argv = remote.ssh_command(target, command, ssh_log=log)
        except ValueError as exc:
            raise remote.RemoteMachineError(f"machine {target.label}: {exc}") from None
        return target, log, argv, install_name

    async def _launch(self, machine_id: str) -> _Node:
        try:
            target, log, argv, install_name = await asyncio.to_thread(self._prepare, machine_id)
        except remote.RemoteMachineError as exc:
            raise NodeError(str(exc)) from None
        channels: dict[int, ChannelEndpoint] = {}
        try:
            client = await AcpClient.launch(
                name=f"raven-node@{target.id}",
                # The argv itself: a line would be read by this host's own
                # grammar, which is not the far shell's (a Windows host's is not).
                command=argv,
                # Fixed rather than any caller's workspace: the ssh needs none.
                cwd=str(Path.home()),
                env={},
                on_notification=partial(self._notified, channels, machine_id),
            )
        except AcpConnectionError as exc:
            raise NodeError(f"could not start ssh to machine {target.label}: {exc}") from None
        try:
            hello = await client.request(protocol.HELLO, {"protocol": protocol.PROTOCOL}, timeout=_HELLO_TIMEOUT_S)
        except AcpConnectionError as exc:
            await client.close()
            raise NodeError(self._ended(target, log, exc, install_name)) from None
        except AcpError as exc:
            await client.close()
            reason = exc.message if isinstance(exc, AcpRemoteError) else str(exc)
            raise NodeError(
                f"the Raven node on machine {target.label} did not complete its handshake: {reason}"
            ) from None
        digest = hello.get("digest") if isinstance(hello, dict) else None
        if not isinstance(hello, dict) or hello.get("protocol") != protocol.PROTOCOL or digest != bundle.digest():
            await client.close()
            raise NodeError(
                f"the Raven node on machine {target.label} runs different code from this Raven; "
                f"update it with `{install_hint(target)}`"
            )
        node = _Node(client, target, log, hello, channels)
        remote_path.remember_home(target.id, node.home)
        return node

    async def _notified(
        self, channels: dict[int, ChannelEndpoint], machine_id: str, method: str, params: dict[str, Any]
    ) -> None:
        """One notification off the link, handed to the channel it names -- or to the stream, for ``mcp/*``."""
        if method in (protocol.MCP_OPEN, protocol.MCP_FRAME, protocol.MCP_CLOSE):
            await self._on_stream(machine_id, method, params)
            return
        channel = params.get("channel")
        endpoint = channels.get(channel) if isinstance(channel, int) and not isinstance(channel, bool) else None
        if endpoint is None:
            logger.debug("raven node: {} for channel {!r}, which is not open", method, channel)
            return
        if method == protocol.AGENT_FRAME:
            endpoint.feed_frame(params.get("line"), params.get("n"))
        elif method == protocol.AGENT_STDERR:
            endpoint.feed_stderr(params.get("text"))
        elif method == protocol.AGENT_EXIT:
            channels.pop(channel, None)
            endpoint.exited(params.get("rc"), params.get("reason"))

    async def _heartbeat(self, machine_id: str, node: _Node) -> None:
        """Ping the node until one ping goes unanswered; then the link is lost."""
        while True:
            await asyncio.sleep(self._heartbeat_s)
            try:
                await node.client.request(protocol.PING, {"t": time.time()}, timeout=self._lost_after_s)
            except AcpTimeoutError:
                said = f"machine {node.target.label} did not answer a heartbeat within {self._lost_after_s:.0f}s"
            except AcpConnectionError as exc:
                said = self._ended(node.target, node.log, exc)
            except AcpError:
                # A refusal is an answer: the node is there.
                continue
            else:
                continue
            await self._lost(machine_id, node, said)
            return

    async def _lost(self, machine_id: str, node: _Node, said: str) -> None:
        """The link to ``node`` is gone: park its agents, close it, and try again on the backoff."""
        if self._nodes.get(machine_id) is not node:
            # Already replaced or closed by another path.
            return
        logger.warning("raven node on machine {}: link lost: {}", node.target.id, said)
        self._errors[machine_id] = f"{said}; reconnecting"
        await self._drop(machine_id, said)
        self._retry_later(machine_id)

    def _retry_later(self, machine_id: str) -> None:
        """Try the link again on the backoff, unless a try is under way already or the pool is closing."""
        if not self._closing and machine_id not in self._reconnects:
            self._reconnects[machine_id] = asyncio.create_task(
                self._reconnect(machine_id), name=f"node-reconnect-{machine_id}"
            )

    async def _reconnect(self, machine_id: str) -> None:
        """Try the link again on the backoff; for as long as agents wait for it, keep trying at the last delay."""
        tried = 0
        try:
            if not self._backoff_s:
                # No backoff at all: the link is tried again by the next call only.
                self._errors[machine_id] = self._errors.get(machine_id, "").removesuffix("; reconnecting")
                return
            while True:
                delay = self._backoff_s[min(tried, len(self._backoff_s) - 1)]
                tried += 1
                await asyncio.sleep(delay)
                try:
                    await self._acquire(machine_id)
                except NodeError as exc:
                    self._errors[machine_id] = f"{exc}; reconnecting"
                else:
                    logger.info("raven node on machine {}: link restored", machine_id)
                    await self._reattach(machine_id)
                    if not self._parked.get(machine_id):
                        return
                    continue
                if tried >= len(self._backoff_s) and not self._parked.get(machine_id):
                    # Given up for now: the sentence stands until the next call tries again.
                    self._errors[machine_id] = self._errors.get(machine_id, "").removesuffix("; reconnecting")
                    return
        finally:
            self._reconnects.pop(machine_id, None)

    async def _reattach(self, machine_id: str) -> None:
        """Every endpoint parked for the machine onto a channel of its new link, from the frame it last read.

        A link that goes while this runs -- under the attach, or under the
        replay of what an endpoint held -- parks that endpoint and every one
        not yet reached again, with everything they still hold, and the link
        is tried again: nothing that waited is dropped on the way.
        """
        node = self._nodes.get(machine_id)
        waiting = self._parked.pop(machine_id, [])
        if node is None or not node.client.alive:
            if waiting:
                self._parked[machine_id] = waiting
            return
        for i, endpoint in enumerate(waiting):
            if endpoint.returncode is not None:
                continue
            channel = node.next_channel
            node.next_channel += 1
            node.channels[channel] = endpoint
            params = {"channel": channel, "task": endpoint.task, "after": endpoint.received}
            try:
                await node.client.request(protocol.AGENT_ATTACH, params, timeout=_ATTACH_TIMEOUT_S)
                if endpoint.returncode is None:
                    await endpoint.reattach(node.client, channel)
            except AcpRemoteError as exc:
                node.channels.pop(channel, None)
                endpoint.lost(
                    f"machine {node.target.label} no longer runs {endpoint.agent!r} (task {endpoint.task}): {exc.message}"
                )
                continue
            except (AcpTimeoutError, AcpConnectionError) as exc:
                # This link is gone too: this endpoint and every one not yet
                # reached wait for the next one, with what they still hold.
                node.channels.pop(channel, None)
                said = (
                    self._ended(node.target, node.log, exc)
                    if isinstance(exc, AcpConnectionError)
                    else f"the Raven node on machine {node.target.label} did not answer {protocol.AGENT_ATTACH}"
                )
                rest = [e for e in waiting[i:] if e.returncode is None]
                for e in rest:
                    e.detach(said)
                self._parked.setdefault(machine_id, []).extend(rest)
                await self._lost(machine_id, node, said)
                return
            logger.info("acp agent {!r}: attached again on {}", endpoint.agent, endpoint.describe())

    def _stop_reconnect(self, machine_id: str) -> None:
        task = self._reconnects.pop(machine_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _refused_launch(self, node: _Node, name: str, exc: AcpRemoteError) -> str:
        if exc.code == protocol.NOT_FOUND:
            return remote.agent_missing(node.target)
        if exc.code == protocol.BUSY:
            limits = node.hello.get("limits") if isinstance(node.hello.get("limits"), dict) else {}
            limit = limits.get("agents") or protocol.MAX_AGENTS
            return f"machine {node.target.label} already runs {limit} agents; wait for one to finish"
        return f"the Raven node on machine {node.target.label} could not start {name!r}: {exc.message}"

    def _ended(
        self, target: remote.Machine, log: Path, exc: AcpConnectionError, install_name: str | None = None
    ) -> str:
        """``exc`` in one sentence: not installed, an ssh failure, or the node's own last word."""
        if exc.returncode in _MISSING_RCS:
            installed = f"Raven node {install_name}" if install_name else "this Raven's node"
            return f"{installed} is not installed on machine {target.label}; install it with `{install_hint(target)}`"
        if (said := remote.failure(target, log, exc)) is not None:
            return said
        lines = [line.strip() for line in (exc.stderr or "").splitlines() if line.strip()]
        # The node's own reason, a traceback's last line, closes its stderr.
        last = lines[-1][:200] if lines else ""
        return f"the Raven node on machine {target.label} stopped" + (f": {last}" if last else "")

    async def _drop(self, machine_id: str, reason: str | None = None) -> None:
        """Forget the link and close it.

        With a ``reason`` the link was lost: its agents are parked to wait for
        the next one. Without, it was closed on purpose, and the agents on it
        are told so; they run on under the machine's daemon, which a later
        link can list (``tasks``).
        """
        node = self._nodes.pop(machine_id, None)
        if node is None:
            return
        if node.heartbeat is not None and node.heartbeat is not asyncio.current_task():
            node.heartbeat.cancel()
        for endpoint in list(node.channels.values()):
            if reason is None:
                endpoint.lost(f"the link to machine {node.target.label} was closed")
            else:
                endpoint.detach(reason)
                self._parked.setdefault(machine_id, []).append(endpoint)
        node.channels.clear()
        # The streams go with the link: the daemon hangs the bridges up on
        # its side, and the endpoints here see their connections end.
        for stream in list(node.streams.values()):
            await self._end_stream(node, stream, "the link ended", tell=False)
        await node.client.close()

    async def _close_idle(self, *, keep: str) -> None:
        """Close the links to other machines that nothing uses: no agent on them, no call on its way to them."""
        now = time.monotonic()
        for machine_id, node in list(self._nodes.items()):
            idle = not node.channels and not node.calls and now - node.used > self._idle_close_s
            if machine_id != keep and idle:
                await self._drop(machine_id)


_POOL: NodePool | None = None


def get_node_pool() -> NodePool:
    """The process-wide pool."""
    global _POOL
    if _POOL is None:
        _POOL = NodePool()
    return _POOL


async def close_node_pool() -> None:
    """Close every node this process started, and forget the pool."""
    global _POOL
    pool, _POOL = _POOL, None
    if pool is not None:
        await pool.close_all()


__all__ = [
    "DEFAULT_NODE_DIR",
    "HEARTBEAT_LOST_S",
    "HEARTBEAT_S",
    "IDLE_CLOSE_S",
    "LOST_RC",
    "RECONNECT_BACKOFF_S",
    "ChannelEndpoint",
    "MachineStatus",
    "NodeError",
    "NodePool",
    "close_node_pool",
    "get_node_pool",
    "install_hint",
    "node_dir",
    "node_python",
]
