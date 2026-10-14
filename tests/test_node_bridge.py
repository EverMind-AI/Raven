"""raven.node.bridge: the stdio MCP server an agent on a machine spawns, carrying one stream to a host endpoint.

The bridge is run as the agent would run it, a process on the same machine
as a real daemon; the host's end is this test, on the daemon's socket. What
is checked is the carrying: the agent's frames reach the host in pieces and
whole, the host's reach the agent's stdout whole, and the stream ends when
either side does -- with the daemon's refusal on stderr when it never began.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

if sys.platform == "win32":
    pytest.skip("the daemon is POSIX: it locks with fcntl and listens on a unix socket", allow_module_level=True)

from raven.node import bridge, protocol, server

pytestmark = pytest.mark.slow


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


async def _wind_down(host: _Host, running: asyncio.Task[int]) -> None:
    """End the agent, hang up, and see the daemon leave idle; a daemon that does not is cancelled rather than waited on."""
    try:
        await host.ask(9, protocol.AGENT_CANCEL, {"channel": 1})
    finally:
        await host.close()
        try:
            assert await asyncio.wait_for(running, timeout=15) == 0, "nothing to do: the daemon leaves"
        except asyncio.TimeoutError:
            running.cancel()
            raise


def _bridge(root: Path, *, task: str = "a@box-1", lend: str = "d1", server_name: str = "pg") -> subprocess.Popen[bytes]:
    argv = [sys.executable, "-I", "-m", "raven.node", "--mcp", "--dir", str(root)]
    argv += ["--task", task, "--lend", lend, "--server", server_name]
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603


def _write(proc: subprocess.Popen[bytes], data: bytes) -> None:
    assert proc.stdin is not None
    proc.stdin.write(data)
    proc.stdin.flush()


async def _line(proc: subprocess.Popen[bytes]) -> bytes:
    assert proc.stdout is not None
    return await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), timeout=15)


async def test_the_bridge_carries_the_agent_s_frames_to_the_host_and_the_host_s_back_whole(tmp_path, monkeypatch):
    root = tmp_path / "n"
    running, host = await _daemon(root, monkeypatch)
    await host.ask(
        2, protocol.AGENT_LAUNCH, {"channel": 1, "task": "a@box-1", "command": "sleep 30", "cwd": str(tmp_path)}
    )
    bridge = _bridge(root)
    assert bridge.stdin is not None
    try:
        # What the agent writes reaches the host as a stream of its task, in pieces that make the frame.
        bridge.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        bridge.stdin.flush()
        await host.until(lambda: host.notified(protocol.MCP_FRAME))
        assert host.notified(protocol.MCP_OPEN) == [{"task": "a@box-1", "stream": 1, "server": "pg", "lend": "d1"}]
        assert host.notified(protocol.MCP_FRAME) == [
            {"stream": 1, "text": '{"jsonrpc":"2.0","id":1,"method":"initialize"}', "end": True}
        ]

        # What the host sends, whole or in pieces, is one line on the agent's side.
        await host.tell(protocol.MCP_FRAME, {"stream": 1, "text": '{"jsonrpc":"2.0","id":1,"result":{}}', "end": True})
        assert await _line(bridge) == b'{"jsonrpc":"2.0","id":1,"result":{}}\n'
        big = '{"jsonrpc":"2.0","id":2,"result":"' + "x" * (protocol.MCP_CHUNK + 10) + '"}'
        for text, end in protocol.pieces(big.encode()):
            await host.tell(protocol.MCP_FRAME, {"stream": 1, "text": text, "end": end})
        assert await _line(bridge) == big.encode() + b"\n"

        # A frame of the agent's longer than a line crosses in pieces too.
        # Written off the loop: the daemon runs on this loop, and a write
        # that blocks here would stop it reading what the bridge forwards.
        long = '{"jsonrpc":"2.0","id":3,"params":"' + "y" * (protocol.MCP_CHUNK + 5) + '"}'
        await asyncio.to_thread(_write, bridge, long.encode() + b"\n")
        await host.until(
            lambda: len(host.notified(protocol.MCP_FRAME)) >= 3 and host.notified(protocol.MCP_FRAME)[-1]["end"]
        )
        crossed = "".join(p["text"] for p in host.notified(protocol.MCP_FRAME)[1:])
        assert crossed == long and [p["end"] for p in host.notified(protocol.MCP_FRAME)[1:]] == [False, True]

        # The agent closes the server's stdin: the stream is over, the host is told, the bridge leaves clean.
        bridge.stdin.close()
        await host.until(lambda: host.notified(protocol.MCP_CLOSE))
        assert host.notified(protocol.MCP_CLOSE) == [
            {"stream": 1, "task": "a@box-1", "reason": "the agent closed the server"}
        ]
        assert await asyncio.to_thread(bridge.wait, 10) == 0
    finally:
        if bridge.poll() is None:
            bridge.kill()
        await _wind_down(host, running)


async def test_a_stream_the_host_closes_ends_the_bridge_and_one_the_daemon_refuses_never_begins(tmp_path, monkeypatch):
    root = tmp_path / "n"
    running, host = await _daemon(root, monkeypatch)
    await host.ask(
        2, protocol.AGENT_LAUNCH, {"channel": 1, "task": "a@box-1", "command": "sleep 30", "cwd": str(tmp_path)}
    )
    try:
        bridge = _bridge(root)
        assert bridge.stdin is not None
        bridge.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        bridge.stdin.flush()
        await host.until(lambda: host.notified(protocol.MCP_OPEN))
        await host.tell(protocol.MCP_CLOSE, {"stream": 1, "reason": "the dispatch ended"})
        assert await asyncio.to_thread(bridge.wait, 10) == 0, "told the stream is over, it leaves"
        assert await _line(bridge) == b"", "and wrote nothing of its own to the agent"

        token = server.task_record_dir(root, "a@box-1") / server.TOKEN_FILE
        token.write_text("0" * 64, encoding="utf-8")
        refused = _bridge(root)
        assert refused.stdin is not None
        # Off the loop, which the daemon answering the bridge runs on.
        _out, err = await asyncio.to_thread(refused.communicate, b"", 15)
        assert refused.returncode == 1 and b"the daemon refused the stream: the token is not task" in err

        nowhere = _bridge(tmp_path / "no-such-node")
        _out, err = await asyncio.to_thread(nowhere.communicate, b"", 15)
        assert nowhere.returncode == 1 and b"no token for task" in err
    finally:
        await _wind_down(host, running)


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


async def test_run_in_this_process_the_bridge_carries_frames_both_ways_and_leaves_when_the_agent_closes_its_stdin(
    tmp_path, monkeypatch
):
    root = tmp_path / "n"
    running, host = await _daemon(root, monkeypatch)
    await host.ask(
        2, protocol.AGENT_LAUNCH, {"channel": 1, "task": "a@box-1", "command": "sleep 30", "cwd": str(tmp_path)}
    )
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
    await host.ask(
        2, protocol.AGENT_LAUNCH, {"channel": 1, "task": "a@box-1", "command": "sleep 30", "cwd": str(tmp_path)}
    )
    try:
        # No token: no node directory of that task's.
        assert await _carry(tmp_path / "no-such-node", asyncio.StreamReader(), _Out()) == 1
        assert "no token for task a@box-1" in capsys.readouterr().err

        # A token but no daemon to reach.
        elsewhere = tmp_path / "elsewhere"
        server.task_record_dir(elsewhere, "a@box-1").mkdir(parents=True)
        (server.task_record_dir(elsewhere, "a@box-1") / server.TOKEN_FILE).write_text("tok", encoding="utf-8")
        assert await _carry(elsewhere, asyncio.StreamReader(), _Out()) == 1
        assert "cannot reach the daemon under" in capsys.readouterr().err

        # The daemon refuses: the token is not the task's.
        token = server.task_record_dir(root, "a@box-1") / server.TOKEN_FILE
        minted = token.read_text(encoding="utf-8")
        token.write_text("0" * 64, encoding="utf-8")
        assert await _carry(root, asyncio.StreamReader(), _Out()) == 1
        assert "the daemon refused the stream: the token is not task" in capsys.readouterr().err
        token.write_text(minted, encoding="utf-8")

        # A frame of the agent's past the cap ends the bridge, and says so; the host hears the stream end.
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
        await _wind_down(host, running)


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
        server.task_record_dir(root, "a@box-1").mkdir(parents=True)
        (server.task_record_dir(root, "a@box-1") / server.TOKEN_FILE).write_text("tok", encoding="utf-8")
        daemon = await asyncio.start_unix_server(handler, path=str(server.socket_path(root)))
        try:
            assert await asyncio.wait_for(_carry(root, asyncio.StreamReader(), out), timeout=15) == want
        finally:
            daemon.close()
            await daemon.wait_closed()

    assert openings == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "mcp/open",
            "params": {"task": "a@box-1", "lend": "d1", "server": "pg", "token": "tok"},
        }
    ]
    assert out.lines() == [b"ab"], "its own stream's frame, put together; nothing of the rest"


def test_run_mcp_takes_the_agent_s_stdin_as_a_pipe_and_keeps_its_stdout_for_frames(tmp_path, monkeypatch, capsys):
    read_end, write_end = os.pipe()
    frames = io.BytesIO()
    with os.fdopen(read_end, "rb") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=frames))
        rc = bridge.run_mcp(tmp_path / "no-such-node", task="a@box-1", lend="d1", server_name="pg")
    os.close(write_end)

    assert rc == 1 and "no token for task a@box-1" in capsys.readouterr().err
    assert frames.getvalue() == b"", "nothing but frames goes to the agent"
