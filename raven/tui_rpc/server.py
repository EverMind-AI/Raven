"""asyncio JSON-RPC 2.0 server loop bound to two POSIX pipe FDs.

Topology (design.md §1):

    Node child writes requests  → FD 3 → Python parent reads here
    Python parent writes resp/notif → FD 4 → Node child reads here

`RpcServer` owns the read pump (one line-delimited JSON frame per iteration),
dispatches concurrently via `asyncio.create_task` so a long-running streaming
subscription doesn't block other RPC calls, and serializes writes with an
`asyncio.Lock` so concurrent dispatch tasks can't interleave bytes on the wire.

Frame size limit: 1 MiB (specs §2.5). Larger frames trigger immediate
shutdown of the connection.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher


# Per specs/tui-ipc.md §2.5
MAX_FRAME_BYTES = 1 * 1024 * 1024  # 1 MiB


class RpcServer:
    """Read JSON-RPC frames from `request_fd`, write responses to `notify_fd`.

    Args:
        request_fd: POSIX fd opened for reading (the Node→Python pipe).
        notify_fd:  POSIX fd opened for writing (the Python→Node pipe).
        dispatcher: a `Dispatcher` instance with all handlers registered.

    The server takes ownership of the FDs: they are closed on `stop()`.
    """

    def __init__(
        self,
        request_fd: int,
        notify_fd: int,
        dispatcher: "Dispatcher",
    ) -> None:
        self._request_fd = request_fd
        self._notify_fd = notify_fd
        self._dispatcher = dispatcher

        self._reader: asyncio.StreamReader | None = None
        self._write_transport: asyncio.WriteTransport | None = None
        self._write_protocol: asyncio.BaseProtocol | None = None
        self._write_lock = asyncio.Lock()
        self._pending: set[asyncio.Task] = set()
        self._stopped = asyncio.Event()
        self._started = asyncio.Event()

    @property
    def started(self) -> asyncio.Event:
        """Set once the read pump has attached to the FD; useful for tests."""
        return self._started

    # ----- write side -------------------------------------------------------

    async def send_frame(self, frame: dict) -> None:
        """Serialize and write a single JSON frame + newline to `notify_fd`.

        All writes (responses + notifications) MUST go through this method so
        the lock serializes them.
        """
        if self._write_transport is None:
            raise RuntimeError("RpcServer.send_frame called before serve_forever()")
        data = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self._write_transport.write(data)

    # ----- main loop --------------------------------------------------------

    async def serve_forever(self) -> None:
        """Run the read/dispatch/write pump until EOF or `stop()`."""
        loop = asyncio.get_running_loop()

        reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
        reader_protocol = asyncio.StreamReaderProtocol(reader)

        # P0 fix (2026-05-15): the production transport in
        # ``tui_commands.run_subprocess_with_rpc`` dups the same accepted unix
        # socket fd into ``request_fd`` and ``notify_fd``. CPython's
        # ``connect_write_pipe`` builds a ``_UnixWritePipeTransport`` whose
        # ``__init__`` registers a reader callback on the WRITE fd to detect
        # "peer closed the pipe" (read-end EOF) — this works for real pipes
        # but is fatal for a bidirectional SOCK_STREAM socket: any inbound
        # byte (e.g. ``system.hello``) makes the reader callback fire,
        # ``_close()`` runs, and every subsequent ``send_frame`` becomes a
        # silent no-op. After 5 such drops asyncio also logs ``"pipe closed
        # by peer or os.write(pipe, data) raised exception."`` — which is the
        # exact symptom seen during ``/cha`` slash autocomplete.
        #
        # Use ``connect_accepted_socket`` (full-duplex selector transport) on
        # the same fd instead. The transport's ``.write()`` works without
        # the spurious peer-close detection. We keep the pipe-based path as a
        # fallback for tests/CI that wire bare ``os.pipe()`` pairs.
        try:
            req_is_sock = stat.S_ISSOCK(os.fstat(self._request_fd).st_mode)
            notif_is_sock = stat.S_ISSOCK(os.fstat(self._notify_fd).st_mode)
        except OSError:
            req_is_sock = notif_is_sock = False

        if req_is_sock and notif_is_sock:
            # Both fds are dups of the same accepted socket. Close the read
            # dup and reclaim the write dup as a ``socket.socket`` — only one
            # handle is needed for a full-duplex transport.
            try:
                os.close(self._request_fd)
            except OSError:
                pass
            sock = socket.socket(fileno=self._notify_fd)
            sock.setblocking(False)
            transport, _ = await loop.connect_accepted_socket(lambda: reader_protocol, sock)
            self._write_transport = transport
            self._write_protocol = reader_protocol
        else:
            # Legacy / test path: bare pipes via ``os.pipe()``.
            # `os.fdopen` so the transport owns a Python file object; loop
            # will close the underlying fd when the transport closes.
            await loop.connect_read_pipe(lambda: reader_protocol, os.fdopen(self._request_fd, "rb", buffering=0))
            write_transport, write_protocol = await loop.connect_write_pipe(
                asyncio.BaseProtocol,
                os.fdopen(self._notify_fd, "wb", buffering=0),
            )
            self._write_transport = write_transport
            self._write_protocol = write_protocol

        self._reader = reader

        self._started.set()
        logger.info(
            "tui_rpc: RpcServer started (pid={}, request_fd={}, notify_fd={}, mode={})",
            os.getpid(),
            self._request_fd,
            self._notify_fd,
            "socket" if req_is_sock and notif_is_sock else "pipe",
        )

        try:
            while not self._stopped.is_set():
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError as exc:
                    # EOF — peer closed. Drain whatever partial bytes we have.
                    if exc.partial:
                        logger.warning(
                            "tui_rpc: incomplete final frame ({} bytes); dropping",
                            len(exc.partial),
                        )
                    break
                except asyncio.LimitOverrunError:
                    logger.error(
                        "tui_rpc: frame exceeds {} bytes; closing connection",
                        MAX_FRAME_BYTES,
                    )
                    break

                if len(line) > MAX_FRAME_BYTES:
                    logger.error("tui_rpc: frame {} bytes > {} cap; closing", len(line), MAX_FRAME_BYTES)
                    break

                # Spawn the dispatch as an independent task so streaming /
                # slow handlers don't block subsequent reads.
                task = asyncio.create_task(self._handle_frame(line))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
        finally:
            await self._shutdown()

    async def _handle_frame(self, raw: bytes) -> None:
        try:
            try:
                frame = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                # JSON-RPC §-32700 parse_error response (id unknown → null).
                resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": "parse_error",
                        "data": {"reason": str(exc)},
                    },
                }
                await self.send_frame(resp)
                return

            response = await self._dispatcher.dispatch(frame)

            # If the original frame omitted `id` (a notification per JSON-RPC
            # 2.0), suppress the response — but dispatcher already echoed
            # whatever it received as id, so we only suppress when id was
            # explicitly absent in the inbound frame.
            if isinstance(frame, dict) and "id" not in frame:
                return
            await self.send_frame(response)
        except Exception:
            # Last-resort guard so a single buggy handler can't kill the pump.
            logger.exception("tui_rpc: _handle_frame failed")

    async def _shutdown(self) -> None:
        # Cancel any in-flight dispatch tasks.
        for task in list(self._pending):
            if not task.done():
                task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        self._pending.clear()

        if self._write_transport is not None:
            try:
                self._write_transport.close()
            except Exception:
                logger.exception("tui_rpc: error closing write transport")
            self._write_transport = None

        self._stopped.set()
        logger.info("tui_rpc: RpcServer stopped (pid={})", os.getpid())

    async def stop(self) -> None:
        """Signal the read loop to exit and wait for cleanup."""
        self._stopped.set()
        # We can't easily interrupt `readuntil`, but closing the write side
        # plus setting `_stopped` will cause the next iteration after EOF to
        # bail. Caller typically just cancels the serve_forever task.


__all__ = ["RpcServer", "MAX_FRAME_BYTES"]
