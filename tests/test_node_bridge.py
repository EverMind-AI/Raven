"""raven.node.bridge: the stdio MCP server an agent on a machine spawns, carrying one stream to a host endpoint.

The bridge is run as the agent would run it, a process on the same machine
as a real daemon; the host's end is this test, on the daemon's socket. What
is checked is the carrying: the agent's frames reach the host in pieces and
whole, the host's reach the agent's stdout whole, and the stream ends when
either side does -- with the daemon's refusal on stderr when it never began.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

if sys.platform == "win32":
    pytest.skip("the daemon is POSIX: it locks with fcntl and listens on a unix socket", allow_module_level=True)

from raven.node import protocol, server

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
