"""The MCP bridge: what an agent on this machine spawns to reach a server the host holds.

``python -I -m raven.node --mcp --dir D --task T --lend L --server S`` is one
stdio MCP server as the agent sees it. It connects to the daemon's socket under
``D``, opens a stream for task ``T`` with the token the daemon left beside the
task's record (:data:`raven.node.server.TOKEN_FILE`), and from then on carries
frames both ways: what the agent writes goes to the daemon as ``mcp/frame``
pieces, what comes back as such pieces is put together and written to stdout.
It parses no MCP and holds no credential, and it ends when either side does.
``S`` and ``L`` name the server and the host's dispatch, for the host to pair
the stream with the endpoint it opened on its own side; neither is a secret.
The host authorizes that pair against the daemon-authenticated task, not the
names alone. Frames use UTF-8 JSON so a Unicode piece fits the daemon's byte limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, BinaryIO

from raven.mcp.bridge import iter_raw_frames
from raven.node import protocol, server

#: The daemon's lines are pieces of at most ``MCP_CHUNK`` characters and a little JSON around them.
_LINE_LIMIT = 2 * 1024 * 1024


def _line(method: str, params: dict[str, Any]) -> bytes:
    return (
        json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False).encode("utf-8") + b"\n"
    )


async def run_mcp_async(
    root: Path, *, task: str, lend: str, server_name: str, stdin: asyncio.StreamReader, out: BinaryIO
) -> int:
    """Carry one stream between ``stdin``/``out`` and the daemon under ``root``; 0 when it ended as streams end."""
    try:
        token = (server.task_record_dir(root, task) / server.TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"raven node mcp bridge: no token for task {task}: {exc}", file=sys.stderr, flush=True)
        return 1
    sock = server.socket_path(root)
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock), limit=_LINE_LIMIT)
    except OSError as exc:
        print(f"raven node mcp bridge: cannot reach the daemon under {root}: {exc}", file=sys.stderr, flush=True)
        return 1
    try:
        opening = {"task": task, "lend": lend, "server": server_name, "token": token}
        writer.write(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": protocol.MCP_OPEN, "params": opening}).encode("utf-8")
            + b"\n"
        )
        await writer.drain()
        try:
            answer = json.loads(await reader.readline() or b"null")
        except ValueError:
            answer = None
        result = answer.get("result") if isinstance(answer, dict) else None
        stream = result.get("stream") if isinstance(result, dict) else None
        if not isinstance(stream, int):
            error = answer.get("error") if isinstance(answer, dict) else None
            said = error.get("message") if isinstance(error, dict) else "no answer"
            print(f"raven node mcp bridge: the daemon refused the stream: {said}", file=sys.stderr, flush=True)
            return 1

        async def inward() -> None:
            # Every frame the agent writes, cut to the lines the daemon reads;
            # the end of its stdin ends the stream, as a server's would end.
            async for frame in iter_raw_frames(stdin, max_frame_bytes=protocol.MCP_MAX_FRAME):
                for text, end in protocol.pieces(frame):
                    writer.write(_line(protocol.MCP_FRAME, {"stream": stream, "text": text, "end": end}))
                    await writer.drain()
            writer.write(_line(protocol.MCP_CLOSE, {"stream": stream, "reason": "the agent closed the server"}))
            await writer.drain()

        async def outward() -> None:
            parts: list[str] = []
            while line := await reader.readline():
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                params = frame.get("params") if isinstance(frame, dict) else None
                if not isinstance(params, dict) or params.get("stream") != stream:
                    continue
                if frame.get("method") == protocol.MCP_FRAME:
                    text = params.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                    if params.get("end", True):
                        out.write("".join(parts).encode("utf-8") + b"\n")
                        out.flush()
                        parts = []
                elif frame.get("method") == protocol.MCP_CLOSE:
                    return

        pumps = [asyncio.create_task(inward()), asyncio.create_task(outward())]
        done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for pump in pending:
            pump.cancel()
        for pump in done:
            if (exc := pump.exception()) is not None:
                print(f"raven node mcp bridge: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                return 1
        return 0
    finally:
        with contextlib.suppress(OSError, ConnectionError):
            writer.close()
            await writer.wait_closed()


def run_mcp(root: Path, *, task: str, lend: str, server_name: str) -> int:
    """Carry one stream between this process's stdio and the daemon under ``root``."""
    out = sys.stdout.buffer
    # stdout is the agent's MCP transport. Anything that prints by accident
    # goes to stderr instead of corrupting a frame.
    sys.stdout = sys.stderr

    async def main() -> int:
        loop = asyncio.get_running_loop()
        stdin = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)
        return await run_mcp_async(root, task=task, lend=lend, server_name=server_name, stdin=stdin, out=out)

    return asyncio.run(main())


__all__ = ["run_mcp", "run_mcp_async"]
