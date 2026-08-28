"""The process a sub-agent spawns to reach a server the host holds.

It knows no MCP and holds no credential: one argument, a unix socket path, and
two byte pumps. Everything about which servers exist, how they authenticate and
when they die stays on the host's side of that socket, whose 0600 mode is the
whole of the boundary.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from typing import BinaryIO

# Above the 8 MiB the ACP frame reader allows, because an MCP tool result can
# carry an image and this hop is not the place to decide that is too big. Still
# bounded: an endless frame from a rogue server would otherwise grow the buffer
# until the process dies with no indication of why.
BRIDGE_MAX_FRAME_BYTES = 32 * 1024 * 1024

_READ_CHUNK = 64 * 1024


async def iter_raw_frames(
    reader: asyncio.StreamReader, *, max_frame_bytes: int = BRIDGE_MAX_FRAME_BYTES
) -> AsyncIterator[bytes]:
    """Yield one newline-delimited frame at a time, without decoding it.

    The buffer is this function's own rather than ``StreamReader.readline``'s:
    that one caps a line at its ``limit`` and raises while the newline is still
    beyond it, so a frame carrying an image never arrives. Decoding is left to
    the two real parties -- a bridge that parsed frames would also have to
    decide what to do with a malformed one, and the only correct answer is to
    hand it to the peer that asked.
    """
    pending = bytearray()
    while chunk := await reader.read(_READ_CHUNK):
        pending.extend(chunk)
        while (idx := pending.find(b"\n")) != -1:
            line, pending = bytes(pending[:idx]), bytearray(pending[idx + 1 :])
            # Checked here as well as below because a whole oversized frame
            # usually arrives with its newline in the same chunk, which leaves
            # the buffer empty by the time the tail check runs.
            if len(line) > max_frame_bytes:
                raise ValueError(f"frame exceeds {max_frame_bytes} bytes")
            if line.strip():
                yield line
        if len(pending) > max_frame_bytes:
            raise ValueError(f"frame exceeds {max_frame_bytes} bytes with no newline in sight")
    # A final line with no newline is the peer dying mid-write. There is no whole
    # frame there, and a truncated one is worse than none.


async def _pump(reader: asyncio.StreamReader, write: "BinaryIO | asyncio.StreamWriter") -> None:
    async for frame in iter_raw_frames(reader):
        if isinstance(write, asyncio.StreamWriter):
            write.write(frame + b"\n")
            await write.drain()
        else:
            write.write(frame + b"\n")
            write.flush()


async def run_bridge(socket_path: str, stdout: BinaryIO) -> int:
    """Pump frames between this process's stdio and ``socket_path``.

    stdin is attached with ``connect_read_pipe`` rather than read in a worker
    thread: a thread blocked on stdin cannot be cancelled, and ``asyncio.run``
    waits for the default executor on the way out -- which turns an upstream
    death into the sub-agent hanging until its own timeout instead of seeing the
    connection drop.
    """
    loop = asyncio.get_running_loop()
    try:
        sock_reader, sock_writer = await asyncio.open_unix_connection(socket_path)
    except OSError as exc:
        print(f"raven mcp bridge: cannot reach {socket_path}: {exc}", file=sys.stderr, flush=True)
        return 1

    stdin_reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin_reader), sys.stdin)

    tasks = [
        asyncio.create_task(_pump(stdin_reader, sock_writer)),
        asyncio.create_task(_pump(sock_reader, stdout)),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        if (exc := task.exception()) is not None:
            print(f"raven mcp bridge: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 1
    return 0
