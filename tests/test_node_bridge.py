"""raven.node.bridge: the stdio MCP server an agent on a machine spawns, carrying one stream to a host endpoint.

The bridge is run the way an agent runs it: spawned by a process the daemon
started as a task (``node_bridge_agent.py`` stands in for the agent, relaying
its stdin and stdout), on the same machine as a real daemon; the host's end is
this test, on the daemon's socket. What is checked is the carrying -- the
agent's frames reach the host in pieces and whole, the host's reach the
agent's stdout whole, the stream ends when either side does -- and whose a
bridge is: the daemon knows it by the process that connected, so one spawned
by this test, or by another task's agent, never begins. The bridge's code is
also driven in this process, with the daemon told the test is a process of
the task, for the lines the subprocess cannot show.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.mcp.bridge import unix_sockets_available

if sys.platform == "win32" or not unix_sockets_available():
    pytest.skip("the daemon is POSIX: it locks with fcntl and listens on a unix socket", allow_module_level=True)

from raven.node import bridge, protocol, server

pytestmark = pytest.mark.slow

_AGENT = Path(__file__).with_name("node_bridge_agent.py")


def test_a_frame_is_cut_into_the_pieces_a_line_holds_and_the_last_is_marked(monkeypatch):
    monkeypatch.setattr(protocol, "MCP_CHUNK", 4)

    assert protocol.pieces(b'{"a":1}') == [('{"a"', False), (":1}", True)]
    assert protocol.pieces(b"") == [("", True)], "an empty frame is one piece, so the end is said"
    assert protocol.pieces("héllo".encode()) == [("héll", False), ("o", True)], "by characters, never inside one"


class _Host:
    """The host's end of a daemon's connection, over its socket: asks, and every notification as it comes."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.frames: list[dict[str, Any]] = []
        self._collecting = asyncio.create_task(self._collect())

    async def _collect(self) -> None:
        while line := await self.reader.readline():
            self.frames.append(json.loads(line))

    async def ask(self, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.writer.write(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode() + b"\n"
        )
        await self.writer.drain()
        await self.until(lambda: any(f.get("id") == request_id for f in self.frames))
        return next(f for f in self.frames if f.get("id") == request_id)

    async def tell(self, method: str, params: dict[str, Any]) -> None:
        self.writer.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}).encode() + b"\n")
        await self.writer.drain()

    def notified(self, method: str) -> list[dict[str, Any]]:
        return [f["params"] for f in self.frames if f.get("method") == method]

    def lines(self, channel: int = 1) -> list[str]:
        """What the agent on ``channel`` wrote: the bridge's stdout as the agent relays it, and its last word."""
        return [p["line"] for p in self.notified(protocol.AGENT_FRAME) if p.get("channel") == channel]

    async def say(self, text: str, *, channel: int = 1, eof: bool = False) -> None:
        """A line to the agent's stdin, which the agent hands to its bridge; ``eof`` ends both."""
        await self.tell(protocol.AGENT_STDIN, {"channel": channel, "data": text, "eof": eof})

    async def until(self, check: Any, *, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while not check():
            assert time.monotonic() < deadline, "did not happen in time"
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        self._collecting.cancel()
        self.writer.close()
        await self.writer.wait_closed()


async def _daemon(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Task[int], _Host]:
    monkeypatch.setattr(server, "_IDLE_CHECK_S", 0.1)
    running = asyncio.create_task(server.run_daemon_async(root, idle_s=0.5))
    sock = server.socket_path(root)
    deadline = time.monotonic() + 10
    while not sock.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    # Lines of pieces, each up to MCP_CHUNK characters plus their JSON: the
    # default 64 KiB limit would drop the collector on the first big one.
    reader, writer = await asyncio.open_unix_connection(str(sock), limit=4 * 1024 * 1024)
    host = _Host(reader, writer)
    await host.ask(1, protocol.HELLO, {"protocol": protocol.PROTOCOL})
    return running, host


async def _wind_down(host: _Host, running: asyncio.Task[int], *, channels: tuple[int, ...] = (1,)) -> None:
    """End the agents, hang up, and see the daemon leave idle; a daemon that does not is cancelled rather than waited on."""
    try:
        for n, channel in enumerate(channels):
            await host.ask(90 + n, protocol.AGENT_CANCEL, {"channel": channel})
    finally:
        await host.close()
        try:
            assert await asyncio.wait_for(running, timeout=15) == 0, "nothing to do: the daemon leaves"
        except asyncio.TimeoutError:
            running.cancel()
            raise


def _bridge_argv(root: Path, *, task: str = "a@box-1", lend: str = "d1", server_name: str = "pg") -> list[str]:
    argv = [sys.executable, "-I", "-m", "raven.node", "--mcp", "--dir", str(root)]
    return [*argv, "--task", task, "--lend", lend, "--server", server_name]


def _bridge(root: Path, **kw: Any) -> subprocess.Popen[bytes]:
    """A bridge this test spawns itself: a process of no task's, as the daemon sees it."""
    return subprocess.Popen(
        _bridge_argv(root, **kw), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )  # noqa: S603


async def _agent(host: _Host, root: Path, tmp_path: Path, *, task: str, channel: int = 1, **kw: Any) -> int:
    """An agent of ``task`` on the daemon, spawning the bridge with ``kw``'s names; its pid."""
    command = shlex.join([sys.executable, str(_AGENT), *_bridge_argv(root, **kw)])
    started = await host.ask(
        10 + channel,
        protocol.AGENT_LAUNCH,
        {"channel": channel, "task": task, "command": command, "cwd": str(tmp_path)},
    )
    assert started["result"]["task"] == task
    return started["result"]["pid"]


async def test_the_bridge_an_agent_spawns_carries_its_frames_to_the_host_and_the_host_s_back_whole(
    tmp_path, monkeypatch
):
    root = tmp_path / "n"
    running, host = await _daemon(root, monkeypatch)
    await _agent(host, root, tmp_path, task="a@box-1")
    try:
        # What the agent writes reaches the host as a stream of its task, in pieces that make the frame.
        await host.say('{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        await host.until(lambda: host.notified(protocol.MCP_FRAME))
        assert host.notified(protocol.MCP_OPEN) == [{"task": "a@box-1", "stream": 1, "server": "pg", "lend": "d1"}]
        assert host.notified(protocol.MCP_FRAME) == [
            {"stream": 1, "text": '{"jsonrpc":"2.0","id":1,"method":"initialize"}', "end": True}
        ]

        # What the host sends, whole or in pieces, is one line on the agent's side.
        await host.tell(protocol.MCP_FRAME, {"stream": 1, "text": '{"jsonrpc":"2.0","id":1,"result":{}}', "end": True})
        big = '{"jsonrpc":"2.0","id":2,"result":"' + "x" * (protocol.MCP_CHUNK + 10) + '"}'
        for text, end in protocol.pieces(big.encode()):
            await host.tell(protocol.MCP_FRAME, {"stream": 1, "text": text, "end": end})
        await host.until(lambda: len(host.lines()) == 2)
        assert host.lines() == ['{"jsonrpc":"2.0","id":1,"result":{}}', big]

        # A frame of the agent's longer than a line crosses in pieces too.
        long = '{"jsonrpc":"2.0","id":3,"params":"' + "y" * (protocol.MCP_CHUNK + 5) + '"}'
        await host.say(long + "\n")
        await host.until(
            lambda: len(host.notified(protocol.MCP_FRAME)) >= 3 and host.notified(protocol.MCP_FRAME)[-1]["end"]
        )
        crossed = "".join(p["text"] for p in host.notified(protocol.MCP_FRAME)[1:])
        assert crossed == long and [p["end"] for p in host.notified(protocol.MCP_FRAME)[1:]] == [False, True]

        # The agent closes the server's stdin: the stream is over, the host is told, the bridge leaves clean.
        await host.say("", eof=True)
        await host.until(lambda: host.notified(protocol.MCP_CLOSE))
        assert host.notified(protocol.MCP_CLOSE) == [
            {"stream": 1, "task": "a@box-1", "reason": "the agent closed the server"}
        ]
        await host.until(lambda: host.lines()[-1:] == ["BRIDGE_EXIT 0 "])
        await host.until(lambda: host.notified(protocol.AGENT_EXIT))
    finally:
        await _wind_down(host, running)


async def test_a_stream_the_host_closes_ends_the_bridge_and_a_bridge_of_no_task_or_of_another_s_never_begins(
    tmp_path, monkeypatch
):
    root = tmp_path / "n"
    running, host = await _daemon(root, monkeypatch)
    try:
        await _agent(host, root, tmp_path, task="a@box-1")
        await host.say('{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        await host.until(lambda: host.notified(protocol.MCP_OPEN))
        await host.tell(protocol.MCP_CLOSE, {"stream": 1, "reason": "the dispatch ended"})
        await host.until(lambda: host.lines())
        assert host.lines() == ["BRIDGE_EXIT 0 "], (
            "told the stream is over, it leaves, having written nothing of its own"
        )
        # The agent outlives its server, so the task the bridges below name is still running.
        assert [p for p in host.notified(protocol.AGENT_EXIT) if p.get("channel") == 1] == []

        # Another task's agent spawns a bridge that names this task, this dispatch and this server: the daemon
        # knows whose process it is, and refuses it as that task's.
        await _agent(host, root, tmp_path, task="b@box-1", channel=2)
        await host.say('{"jsonrpc":"2.0","id":1,"method":"initialize"}\n', channel=2)
        await host.until(lambda: host.lines(2))
        (said,) = host.lines(2)
        assert said == (
            "BRIDGE_EXIT 1 raven node mcp bridge: the daemon refused the stream: "
            "the connection is a process of task 'b@box-1', not 'a@box-1'"
        )
        assert host.notified(protocol.MCP_OPEN) == [{"task": "a@box-1", "stream": 1, "server": "pg", "lend": "d1"}]

        # A bridge spawned by nobody's agent -- this test's own process -- is no task's.
        stranger = _bridge(root)
        _out, err = await asyncio.to_thread(stranger.communicate, b'{"jsonrpc":"2.0","id":1}\n', 15)
        assert stranger.returncode == 1
        assert err.strip() == (
            b"raven node mcp bridge: the daemon refused the stream: "
            b"the connection is not a process of any task on this machine"
        )

        nowhere = _bridge(tmp_path / "no-such-node")
        _out, err = await asyncio.to_thread(nowhere.communicate, b"", 15)
        assert nowhere.returncode == 1 and b"cannot reach the daemon under" in err
    finally:
        await _wind_down(host, running, channels=(1, 2))


# ---- the bridge run in this process, on the daemon's socket ----------------------------------------


class _Out:
    """What the bridge writes to the agent, as its stdout would take it."""

    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    def flush(self) -> None:
        pass

    def lines(self) -> list[bytes]:
        return self.data.splitlines()


def _carry(root: Path, stdin: asyncio.StreamReader, out: _Out, **kw: Any) -> asyncio.Task[int]:
    opts = {"task": "a@box-1", "lend": "d1", "server_name": "pg", **kw}
    return asyncio.create_task(bridge.run_mcp_async(root, stdin=stdin, out=out, **opts))


def _of_task(monkeypatch: pytest.MonkeyPatch, task: str | None) -> None:
    """Make the daemon place this process (the in-process bridge's) as ``task``'s, or, with ``None``, as no task's.

    Only the identity read is made up, not the process table: ``_end_tree``
    reads the real one to end the agent's group at teardown, and a made-up
    table that put this process under the agent would have the cancel kill
    the test itself.
    """
    real = server.Daemon.task_of

    def placed(self: server.Daemon, pid: int) -> Any:
        if pid == os.getpid():
            return self.tasks.get(task) if task is not None else None
        return real(self, pid)

    monkeypatch.setattr(server.Daemon, "task_of", placed)


async def test_run_in_this_process_the_bridge_carries_frames_both_ways_and_leaves_when_the_agent_closes_its_stdin(
    tmp_path, monkeypatch
):
    root = tmp_path / "n"
    running, host = await _daemon(root, monkeypatch)
    started = await host.ask(
        2, protocol.AGENT_LAUNCH, {"channel": 1, "task": "a@box-1", "command": "sleep 30", "cwd": str(tmp_path)}
    )
    _of_task(monkeypatch, "a@box-1")
    stdin, out = asyncio.StreamReader(), _Out()
    carrying = _carry(root, stdin, out)
    try:
        stdin.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        await host.until(lambda: host.notified(protocol.MCP_FRAME))
        assert host.notified(protocol.MCP_OPEN) == [{"task": "a@box-1", "stream": 1, "server": "pg", "lend": "d1"}]
        assert host.notified(protocol.MCP_FRAME) == [
            {"stream": 1, "text": '{"jsonrpc":"2.0","id":1,"method":"initialize"}', "end": True}
        ]

        await host.tell(protocol.MCP_FRAME, {"stream": 1, "text": '{"jsonrpc":"2.0","id":1,"result":{}}', "end": True})
        big = '{"jsonrpc":"2.0","id":2,"result":"' + "x" * (protocol.MCP_CHUNK + 10) + '"}'
        for text, end in protocol.pieces(big.encode()):
            await host.tell(protocol.MCP_FRAME, {"stream": 1, "text": text, "end": end})
        await host.until(lambda: len(out.lines()) == 2)
        assert out.lines() == [b'{"jsonrpc":"2.0","id":1,"result":{}}', big.encode()], "whole lines, however they came"

        long = '{"jsonrpc":"2.0","id":3,"params":"' + "y" * (protocol.MCP_CHUNK + 5) + '"}'
        stdin.feed_data(long.encode() + b"\n")
        await host.until(
            lambda: len(host.notified(protocol.MCP_FRAME)) >= 3 and host.notified(protocol.MCP_FRAME)[-1]["end"]
        )
        assert "".join(p["text"] for p in host.notified(protocol.MCP_FRAME)[1:]) == long

        stdin.feed_eof()
        assert await asyncio.wait_for(carrying, timeout=15) == 0
        await host.until(lambda: host.notified(protocol.MCP_CLOSE))
        assert host.notified(protocol.MCP_CLOSE) == [
            {"stream": 1, "task": "a@box-1", "reason": "the agent closed the server"}
        ]
    finally:
        if not carrying.done():
            carrying.cancel()
        await _wind_down(host, running)


async def test_the_bridge_says_why_it_never_began_and_why_it_stopped(tmp_path, monkeypatch, capsys):
    root = tmp_path / "n"
    running, host = await _daemon(root, monkeypatch)
    started = await host.ask(
        2, protocol.AGENT_LAUNCH, {"channel": 1, "task": "a@box-1", "command": "sleep 30", "cwd": str(tmp_path)}
    )
    agent = started["result"]["pid"]
    try:
        # No daemon to reach under that directory.
        assert await _carry(tmp_path / "no-such-node", asyncio.StreamReader(), _Out()) == 1
        assert "cannot reach the daemon under" in capsys.readouterr().err

        # The daemon refuses: this process is no task's, as the real process table has it.
        assert await _carry(root, asyncio.StreamReader(), _Out()) == 1
        assert (
            "the daemon refused the stream: the connection is not a process of any task on this machine"
            in capsys.readouterr().err
        )

        # The daemon refuses: this process is another task's.
        other = await host.ask(
            3, protocol.AGENT_LAUNCH, {"channel": 2, "task": "b@box-1", "command": "sleep 30", "cwd": str(tmp_path)}
        )
        _of_task(monkeypatch, "b@box-1")
        assert await _carry(root, asyncio.StreamReader(), _Out()) == 1
        assert "the daemon refused the stream: the connection is a process of task 'b@box-1', not 'a@box-1'" in (
            capsys.readouterr().err
        )

        # A frame of the agent's past the cap ends the bridge, and says so; the host hears the stream end.
        _of_task(monkeypatch, "a@box-1")
        monkeypatch.setattr(protocol, "MCP_MAX_FRAME", 64)
        stdin = asyncio.StreamReader()
        carrying = _carry(root, stdin, _Out())
        await host.until(lambda: host.notified(protocol.MCP_OPEN))
        stdin.feed_data(b'{"jsonrpc":"2.0","id":1,"params":"' + b"z" * 100 + b'"}\n')
        assert await asyncio.wait_for(carrying, timeout=15) == 1
        assert "ValueError: frame exceeds 64 bytes" in capsys.readouterr().err
        await host.until(lambda: host.notified(protocol.MCP_CLOSE))
        assert host.notified(protocol.MCP_CLOSE)[-1]["reason"] == "the bridge ended"
    finally:
        await _wind_down(host, running, channels=(1, 2))


async def test_the_bridge_reads_only_its_stream_s_frames_off_the_daemon_and_leaves_when_told_the_stream_is_over(
    tmp_path,
):
    """A stand-in daemon on the socket: what the bridge makes of lines that are not its stream's, and of a close."""
    openings: list[dict[str, Any]] = []

    async def answering(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        openings.append(json.loads(await reader.readline()))
        for line in (
            b'{"jsonrpc":"2.0","id":1,"result":{"stream":7}}\n',
            b"not json\n",
            b'["not an object"]\n',
            b'{"jsonrpc":"2.0","method":"mcp/frame","params":{"stream":2,"text":"other","end":true}}\n',
            b'{"jsonrpc":"2.0","method":"mcp/frame","params":{"stream":7,"text":5,"end":false}}\n',
            b'{"jsonrpc":"2.0","method":"something/else","params":{"stream":7}}\n',
            b'{"jsonrpc":"2.0","method":"mcp/frame","params":{"stream":7,"text":"a","end":false}}\n',
            b'{"jsonrpc":"2.0","method":"mcp/frame","params":{"stream":7,"text":"b","end":true}}\n',
            b'{"jsonrpc":"2.0","method":"mcp/close","params":{"stream":7,"reason":"the dispatch ended"}}\n',
        ):
            writer.write(line)
        await writer.drain()
        await reader.read()
        writer.close()

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.close()

    async def refusing(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b'{"jsonrpc":"2.0","id":1,"error":{"code":-32015,"message":"no host holds task"}}\n')
        await writer.drain()
        writer.close()

    out = _Out()
    for name, handler, want in (("a", answering, 0), ("b", silent, 1), ("c", refusing, 1)):
        root = tmp_path / name
        server.own_dir(root / server.RUN_DIR)
        daemon = await asyncio.start_unix_server(handler, path=str(server.socket_path(root)))
        try:
            assert await asyncio.wait_for(_carry(root, asyncio.StreamReader(), out), timeout=15) == want
        finally:
            daemon.close()
            await daemon.wait_closed()

    assert openings == [
        {"jsonrpc": "2.0", "id": 1, "method": "mcp/open", "params": {"task": "a@box-1", "lend": "d1", "server": "pg"}}
    ]
    assert out.lines() == [b"ab"], "its own stream's frame, put together; nothing of the rest"


# ---- a daemon side that goes under the stream --------------------------------------------------


class _GoneReader:
    """The bridge's read side of a daemon connection: the lines given, then the error given, else nothing more."""

    def __init__(self, lines: list[bytes], then: BaseException | None = None) -> None:
        self.lines = list(lines)
        self.then = then
        self._never = asyncio.Event()

    async def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        if self.then is not None:
            raise self.then
        await self._never.wait()
        return b""


class _GoneWriter:
    """The bridge's write side: every line kept, and a broken pipe from the ``breaks_at``-th line's drain on."""

    def __init__(self, breaks_at: int | None = None) -> None:
        self.sent: list[bytes] = []
        self.breaks_at = breaks_at
        self.closed = False

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        if self.breaks_at is not None and len(self.sent) >= self.breaks_at:
            raise BrokenPipeError(32, "Broken pipe")

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


_OPENED = b'{"jsonrpc":"2.0","id":1,"result":{"stream":4}}\n'


def _connected(monkeypatch: pytest.MonkeyPatch, reader: _GoneReader, writer: _GoneWriter) -> None:
    async def connect(path: str, *, limit: int) -> tuple[_GoneReader, _GoneWriter]:
        return reader, writer

    monkeypatch.setattr(bridge.asyncio, "open_unix_connection", connect)


def _pumps_left() -> list[str]:
    """The bridge's own tasks still pending: none, once it has returned."""
    return [
        task.get_coro().__qualname__
        for task in asyncio.all_tasks()
        if not task.done() and task.get_coro().__qualname__.startswith("run_mcp_async.<locals>.")
    ]


@pytest.mark.parametrize(
    ("then", "breaks_at", "agent_says"),
    [
        (None, 2, b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'),
        (ConnectionResetError(54, "Connection reset by peer"), None, b""),
    ],
    ids=["a broken pipe under the agent's frame", "a reset under the daemon's"],
)
async def test_a_stream_whose_daemon_side_goes_under_it_ends_as_streams_end(
    tmp_path, monkeypatch, capsys, then, breaks_at, agent_says
):
    """How a stream the host closed can end when a frame of the agent's is on its way: the daemon writes mcp/close
    and hangs up, and the frame meets the closed socket, or the read side is reset before mcp/close is read. Either
    way the stream is over: the bridge leaves as a stream ends, says nothing, and leaves no pump behind."""
    writer = _GoneWriter(breaks_at)
    _connected(monkeypatch, _GoneReader([_OPENED], then), writer)
    stdin = asyncio.StreamReader()
    stdin.feed_data(agent_says)

    rc = await asyncio.wait_for(_carry(tmp_path / "n", stdin, _Out()), timeout=15)

    assert (rc, capsys.readouterr().err) == (0, "")
    assert _pumps_left() == [] and writer.closed


async def test_a_frame_past_the_cap_is_said_and_leaves_no_pump_behind(tmp_path, monkeypatch, capsys):
    """A fault of the stream's own is still said, unlike the daemon side going: a frame past the cap ends the bridge
    with its reason, and the pump reading the daemon is over before the socket closes."""
    monkeypatch.setattr(protocol, "MCP_MAX_FRAME", 64)
    _connected(monkeypatch, _GoneReader([_OPENED]), _GoneWriter())
    stdin = asyncio.StreamReader()
    stdin.feed_data(b'{"jsonrpc":"2.0","id":1,"params":"' + b"z" * 100 + b'"}\n')

    assert await asyncio.wait_for(_carry(tmp_path / "n", stdin, _Out()), timeout=15) == 1
    assert capsys.readouterr().err == "raven node mcp bridge: ValueError: frame exceeds 64 bytes\n"
    assert _pumps_left() == []


@pytest.mark.parametrize("breaks", ["on the opening", "before the answer"])
async def test_a_daemon_that_goes_before_the_stream_opens_is_one_sentence(tmp_path, monkeypatch, capsys, breaks):
    reset = ConnectionResetError(54, "Connection reset by peer")
    if breaks == "on the opening":
        _connected(monkeypatch, _GoneReader([]), _GoneWriter(breaks_at=1))
        why = "[Errno 32] Broken pipe"
    else:
        _connected(monkeypatch, _GoneReader([], reset), _GoneWriter())
        why = "[Errno 54] Connection reset by peer"

    assert await asyncio.wait_for(_carry(tmp_path / "n", asyncio.StreamReader(), _Out()), timeout=15) == 1
    assert capsys.readouterr().err == (
        f"raven node mcp bridge: the daemon closed the connection before the stream opened: {why}\n"
    )


def test_run_mcp_takes_the_agent_s_stdin_as_a_pipe_and_keeps_its_stdout_for_frames(tmp_path, monkeypatch, capsys):
    read_end, write_end = os.pipe()
    frames = io.BytesIO()
    with os.fdopen(read_end, "rb") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=frames))
        rc = bridge.run_mcp(tmp_path / "no-such-node", task="a@box-1", lend="d1", server_name="pg")
    os.close(write_end)

    assert rc == 1 and "cannot reach the daemon under" in capsys.readouterr().err
    assert frames.getvalue() == b"", "nothing but frames goes to the agent"


async def test_a_full_astral_piece_fits_the_daemons_byte_limited_reader():
    text = chr(0x1F600) * protocol.MCP_CHUNK
    wire = bridge._line(protocol.MCP_FRAME, {"stream": 1, "text": text, "end": True})
    reader = asyncio.StreamReader(limit=server._LINE_LIMIT)
    reader.feed_data(wire)
    reader.feed_eof()
    assert json.loads(await reader.readline())["params"]["text"] == text
