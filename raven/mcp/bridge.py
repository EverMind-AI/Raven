"""The process a sub-agent spawns to reach a server the host holds.

It knows no MCP and holds no upstream credential: one endpoint argument and
two byte pumps. Unix sockets use file permissions; loopback TCP endpoints use
a per-endpoint token. Server definitions and credentials stay on the host.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
from collections.abc import AsyncIterator
from typing import BinaryIO

from raven.mcp.transport import endpoint_label, open_endpoint
from raven.mcp.transport import unix_sockets_available as unix_sockets_available

# Above the 8 MiB the ACP frame reader allows, because an MCP tool result can
# carry an image and this hop is not the place to decide that is too big. Still
# bounded: an endless frame from a rogue server would otherwise grow the buffer
# until the process dies with no indication of why.
BRIDGE_MAX_FRAME_BYTES = 32 * 1024 * 1024

_READ_CHUNK = 64 * 1024
_CLOSE_TIMEOUT_S = 1.0


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


class _StdinTransport(asyncio.ReadTransport):
    """Pass StreamReader backpressure and shutdown to the Windows stdin worker."""

    def __init__(self) -> None:
        super().__init__()
        self.ready = threading.Event()
        self.ready.set()
        self._stopped = threading.Event()
        self._reading = True

    def is_reading(self) -> bool:
        return self._reading and not self.is_closing()

    def pause_reading(self) -> None:
        self._reading = False
        self.ready.clear()

    def resume_reading(self) -> None:
        if not self.is_closing():
            self._reading = True
            self.ready.set()

    def is_closing(self) -> bool:
        return self._stopped.is_set()

    def close(self) -> None:
        self._stopped.set()
        self.ready.set()


@contextlib.asynccontextmanager
async def _stdin_reader() -> AsyncIterator[asyncio.StreamReader]:
    """Read inherited stdin without keeping the process alive after a disconnect."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    if sys.platform != "win32":
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        try:
            yield reader
        finally:
            transport.close()
        return

    # Inherited Windows pipes are not overlapped handles. A daemon reading the
    # raw descriptor neither holds a buffered-stdio lock nor delays exit while
    # stdin stays open after the endpoint dies, as a default-executor job would.
    fd = sys.stdin.fileno()
    transport = _StdinTransport()
    reader.set_transport(transport)

    def deliver(chunk: bytes) -> None:
        if not transport.is_closing():
            reader.feed_data(chunk)
            if transport.is_reading():
                transport.ready.set()

    def feed() -> None:
        try:
            while True:
                # Acknowledge delivery before reading again: queued callbacks
                # would otherwise bypass the reader's buffer limit.
                transport.ready.wait()
                transport.ready.clear()
                if transport.is_closing() or not (chunk := os.read(fd, _READ_CHUNK)):
                    break
                loop.call_soon_threadsafe(deliver, chunk)
        except (OSError, RuntimeError):
            pass
        finally:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(reader.feed_eof)

    threading.Thread(target=feed, name="mcp-stdin", daemon=True).start()
    try:
        yield reader
    finally:
        transport.close()


async def run_bridge(socket_path: str, stdout: BinaryIO) -> int:
    """Pump frames between stdio and a Unix or authenticated loopback endpoint."""
    try:
        sock_reader, sock_writer = await open_endpoint(socket_path)
    except OSError as exc:
        print(f"raven mcp bridge: cannot reach {endpoint_label(socket_path)}: {exc}", file=sys.stderr, flush=True)
        return 1

    tasks: list[asyncio.Task] = []
    try:
        async with _stdin_reader() as stdin_reader:
            tasks = [
                asyncio.create_task(_pump(stdin_reader, sock_writer)),
                asyncio.create_task(_pump(sock_reader, stdout)),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        return 0
    except (OSError, ValueError) as exc:
        print(f"raven mcp bridge: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        for task in tasks:
            task.cancel()
        sock_writer.close()
        # A transport whose buffer drains inside that close has already run
        # connection_lost and dropped its loop, and the selector loop's
        # _force_close does not stop there -- aborting it afterwards reaches
        # call_soon on None. Only a close that did not finish needs the force.
        drained = False
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(OSError, TimeoutError):
                async with asyncio.timeout(_CLOSE_TIMEOUT_S):
                    await sock_writer.wait_closed()
                drained = True
        finally:
            if not drained:
                sock_writer.transport.abort()
