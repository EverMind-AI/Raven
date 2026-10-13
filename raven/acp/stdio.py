"""Making stdout safe to speak a protocol on.

An ACP agent's stdout is a JSON-RPC channel, not a place to print. Every other
writer in the process shares that descriptor -- an embedded structlog, a library
banner, a traceback, a ``print`` left behind from a debug session -- and one line
from any of them is a frame the client cannot decode. What the client sees is not
"raven logged something"; it is a protocol violation, and the session it was half
way through is gone.

Nothing here knows an ACP method name. This is the layer that makes the channel
speakable, kept apart from anything that speaks.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Generator
from typing import Any, BinaryIO

from loguru import logger

from raven.acp_client.protocol import AcpProtocolError, decode, encode

# One ACP frame is not a line of text. ``session/prompt`` carries images and
# embedded resources, and base64 inflates by about 4/3, so a 4 MB screenshot
# arrives as a single frame of roughly 5.5 MB. The 1 MiB cap in
# ``rpc/server.py`` is sized for the TUI's own traffic and would reject input
# this protocol is specified to accept.
MAX_FRAME_BYTES = 8 * 1024 * 1024

_READ_CHUNK = 64 * 1024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600


@contextlib.contextmanager
def claim_stdout() -> Generator[BinaryIO, None, None]:
    """Hand the protocol its own descriptor, and point fd 1 at stderr.

    The returned writer owns a duplicate of the original fd 1. For the duration
    of the block fd 1 *is* stderr, so anything that writes to stdout by any
    route -- ``sys.stdout``, ``os.write(1, ...)``, a C extension holding the
    descriptor, an embedded logger with its own stream -- lands in the log
    instead of the frame stream.

    Replacing ``sys.stdout`` alone would not do: it covers only writers that go
    through Python's own object, which is precisely the set that was never the
    problem. The descriptor is the shared resource, so the descriptor is what
    gets moved.

    The writer is buffered and :func:`write_frame` flushes each frame, rather
    than unbuffered. A raw ``FileIO.write`` is a single ``write(2)`` that may
    return fewer bytes than it was handed, and nothing in the return value is
    checked -- so a short write would be a silently truncated frame, with the
    next frame concatenating onto its tail. ``BufferedWriter.flush`` retries
    short writes itself and raises ``BlockingIOError`` rather than discarding.
    Flushing per frame keeps the property that matters: a frame left in a buffer
    is a client waiting forever for a reply that was already computed, which
    reads as a hang rather than as slowness and is the harder of the two to
    diagnose.

    This is not hypothetical here. :func:`open_stdin`'s ``connect_read_pipe`` sets
    ``O_NONBLOCK`` on fd 0's open file description, and an interactive shell
    hands a foreground job fd 0, 1 and 2 as dups of one description -- so the
    flag lands on this descriptor too whenever somebody runs ``raven acp`` in a
    terminal rather than letting an editor spawn it.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    protocol_fd = os.dup(1)
    # Guarded as its own step: ``dup2`` fails when fd 2 is closed, which
    # ``raven acp 2>&-`` does, and ``fdopen`` can fail after fd 1 has already
    # been moved. Leaving either failure unhandled would leak the descriptor and,
    # worse, hand the caller an exception while the process kept running with its
    # stdout still pointed at stderr and nobody left holding the original.
    try:
        os.dup2(2, 1)
        writer = os.fdopen(protocol_fd, "wb", buffering=-1, closefd=False)
    except OSError:
        os.dup2(protocol_fd, 1)
        os.close(protocol_fd)
        raise
    try:
        yield writer
    finally:
        with contextlib.suppress(OSError):
            writer.flush()
        # Before the restore, not after: anything written through the
        # ``sys.stdout`` *object* during the block -- the stray ``print`` the
        # module docstring names first, or a library that attached a
        # ``StreamHandler(sys.stdout)`` after the block began -- is sitting in
        # that object's buffer, since stdout is a pipe and therefore
        # block-buffered. Restoring fd 1 first would leave those bytes to be
        # flushed at interpreter shutdown, when fd 1 is the wire again.
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        # ``protocol_fd`` is the original stdout, so restoring it to fd 1 is
        # what puts the process back the way it was found.
        os.dup2(protocol_fd, 1)
        os.close(protocol_fd)


def write_frame(writer: BinaryIO, frame: dict[str, Any]) -> None:
    """Put one frame on the wire, whole, before returning.

    Encoding is :func:`raven.acp_client.protocol.encode` rather than a second
    serialiser, so both directions cannot drift on ``ensure_ascii`` or on
    whether the newline is part of the frame.

    The flush is the contract: it is what makes a short write the buffered
    layer's problem instead of a truncated frame, and what keeps the frame from
    waiting in a buffer for the next one to push it out. On a descriptor that
    cannot take the whole frame it raises rather than dropping the tail, which
    is the outcome a caller can act on.
    """
    writer.write(encode(frame))
    writer.flush()


@contextlib.asynccontextmanager
async def open_stdin() -> AsyncIterator[asyncio.StreamReader]:
    """A reader over fd 0, released on the way out.

    The limit bounds the reader's own buffer, which is backpressure rather than
    a frame cap: framing is :func:`read_frames`'s own, precisely so an oversized
    frame can be answered instead of raising out of the transport.

    Two paths, because ``connect_read_pipe`` does not accept every stdin. It
    refuses a regular file outright -- ``ValueError: Pipe transport is for
    pipes/sockets only`` -- so ``raven acp < script.jsonl``, which is how anyone
    first tries this by hand, would die with a traceback before reading a byte.
    The fallback reads the descriptor on a daemon thread and feeds the same
    reader, which costs a thread and gives up the transport's own backpressure
    (the reader's buffer grows past its limit rather than pausing a producer that
    cannot be paused). Windows stdin also takes this path: inherited anonymous
    pipes do not support the overlapped I/O the Proactor transport requires.

    On the pipe path the transport is closed rather than left to the garbage
    collector: a dropped read transport is collected with the loop still holding
    its descriptor, and the unregister that follows fails on a descriptor that is
    already -1, raising somewhere with no caller to report it to.
    """
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    loop = asyncio.get_running_loop()
    if sys.platform == "win32":
        _spawn_stdin_feeder(reader)
        yield reader
        return
    try:
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (ValueError, OSError) as exc:
        logger.info("acp: stdin is not a pipe ({}); reading it on a thread", exc)
        _spawn_stdin_feeder(reader)
        yield reader
        return
    try:
        yield reader
    finally:
        transport.close()


def _spawn_stdin_feeder(reader: asyncio.StreamReader) -> threading.Thread:
    """Pump fd 0 into ``reader`` from a daemon thread.

    A daemon thread and not ``run_in_executor``. The read is blocking and cannot
    be cancelled, so the outstanding call outlives whoever gave up waiting for it
    -- and asyncio *waits for the default executor* when it closes the loop, which
    turns that into a process that will not exit. Measured, not predicted: a test
    over an idle pipe hung until it was killed. A daemon thread has no such hold
    on interpreter shutdown.

    The reader is fed through ``call_soon_threadsafe`` because ``StreamReader`` is
    not thread-safe: feeding it directly from here would race the loop's own
    reads of the same buffer.
    """
    loop = asyncio.get_running_loop()
    stream = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
    # ``read1`` and not ``read``: on a buffered stream ``read(n)`` blocks until it
    # has all n bytes or sees EOF, so a stream that is merely slow would deliver
    # nothing until 64 KiB had accumulated -- one frame at a time is exactly the
    # traffic pattern this has to carry. ``read1`` returns whatever one raw read
    # produced. The fallback is for a stream object that has no ``read1`` at all.
    read = _windows_stdin_read(stream) if os.name == "nt" else None
    read = read or getattr(stream, "read1", None) or stream.read

    def _pump() -> None:
        try:
            while True:
                chunk = read(_READ_CHUNK)
                if not isinstance(chunk, bytes):
                    # A text-mode stdin, which happens in an embedded interpreter
                    # with no ``buffer`` attribute to prefer.
                    chunk = str(chunk).encode("utf-8") if chunk else b""
                if not chunk:
                    loop.call_soon_threadsafe(reader.feed_eof)
                    return
                loop.call_soon_threadsafe(reader.feed_data, chunk)
        except Exception as exc:
            # A read error is EOF as far as the protocol is concerned: there is
            # nothing more coming, and the frame loop should end rather than
            # wait. Reported on the way past because a truncated session and a
            # finished one look identical from the loop. ``feed_eof`` twice is
            # harmless, which is why this needs no flag to coordinate with the
            # branch above.
            logger.warning("acp: reading stdin failed: {}", exc)
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(reader.feed_eof)

    thread = threading.Thread(target=_pump, name="acp-stdin", daemon=True)
    thread.start()
    return thread


def _windows_stdin_read(stream):
    """Read Windows stdin without a CRT lock that can block native-module imports."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    api = _windows_stdin_api(fd)
    if api is None:
        return None
    read_file, peek_pipe, handle, last_error = api

    def read(size, _stream=stream):
        while True:
            available = ctypes.c_uint32()
            if not peek_pipe(handle, None, 0, None, ctypes.byref(available), None):
                error = last_error()
                if error in (38, 109):
                    return b""
                raise OSError(error, "checking Windows stdin failed")
            if available.value:
                length = min(size, available.value)
                buffer = ctypes.create_string_buffer(length)
                count = ctypes.c_uint32()
                if not read_file(handle, buffer, length, ctypes.byref(count), None):
                    error = last_error()
                    if error in (38, 109):
                        return b""
                    raise OSError(error, "reading Windows stdin failed")
                return buffer.raw[: count.value]
            # A pending read on synchronous stdin can block native imports inspecting its handle.
            time.sleep(0.01)

    return read


def _windows_stdin_api(fd):  # pragma: no cover - native Windows declarations
    """Bind nonblocking pipe inspection and reads to the inherited stdin handle."""
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = msvcrt.get_osfhandle(fd)
    kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    kernel32.GetFileType.restype = wintypes.DWORD
    if kernel32.GetFileType(handle) != 3:
        return None
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    return kernel32.ReadFile, kernel32.PeekNamedPipe, handle, ctypes.get_last_error


async def read_frames(
    reader: asyncio.StreamReader,
    on_protocol_error: Callable[[dict[str, Any]], None],
    *,
    max_frame_bytes: int = MAX_FRAME_BYTES,
) -> AsyncIterator[dict[str, Any]]:
    """Yield one decoded frame per line, answering the client on bad input.

    Two failures are answered rather than raised. An agent that dies on
    malformed input leaves every pending request unresolved, and a client whose
    promise never settles is worse off than one holding an error: it has nothing
    to show the user and nothing to retry.

    - A line longer than ``max_frame_bytes``. The oversized bytes are dropped up
      to and including their newline, so the next frame starts where a frame
      starts. Both the error and the resynchronisation are the point: without
      the drop, reading resumes in the middle of the discarded frame and every
      frame after it is garbage too.
    - A line that is not a JSON object.

    Both are reported through ``on_protocol_error`` as a JSON-RPC error frame
    with a null ``id``, because the id lived in the bytes that could not be
    read. EOF ends the iteration: that is the client closing the session, not a
    failure to report to it.

    The line buffer is this function's own rather than
    ``StreamReader.readuntil``'s. ``readuntil`` raises ``LimitOverrunError``
    while the separator is still beyond its limit and leaves the bytes in place,
    so every retry raises again and the stream never advances; and reading fixed
    chunks to drain it would swallow the start of the following frame, which is
    already in the same chunk. Owning the buffer is what makes the recovery
    correct. Same reason ``CliAgentBackend._communicate_streaming`` keeps its
    own.
    """
    pending = b""
    dropping = False
    while chunk := await reader.read(_READ_CHUNK):
        *lines, pending = (pending + chunk).split(b"\n")
        for line in lines:
            if dropping:
                # The tail of a frame already reported. Its newline is what ends
                # the drop, and the drop is what puts the stream back in phase.
                dropping = False
                continue
            if len(line) > max_frame_bytes:
                # Complete and oversized: the whole line arrived before its
                # newline was seen, so there is nothing left to resynchronise.
                on_protocol_error(_oversized(max_frame_bytes))
                continue
            frame = _decode_or_report(line, on_protocol_error)
            if frame is not None:
                yield frame
        if dropping:
            # Still inside the frame being discarded. Its bytes are not wanted,
            # only its newline, which arrives as a line boundary -- so drop them
            # rather than letting a deliberately endless frame grow the buffer.
            pending = b""
        elif len(pending) > max_frame_bytes:
            # Oversized and still incomplete: report now rather than buffering
            # the rest of it, and skip bytes until the newline lands.
            on_protocol_error(_oversized(max_frame_bytes))
            pending = b""
            dropping = True
    # A final line with no newline is the client dying mid-write. There is no
    # whole frame there to act on, and guessing at a truncated one is how a
    # partial tool call gets executed.


def _decode_or_report(
    line: bytes,
    on_protocol_error: Callable[[dict[str, Any]], None],
) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        # Strict, not ``errors="replace"``. Replacing substitutes U+FFFD for the
        # offending bytes, and if they sat inside a JSON string the frame then
        # parses -- so a corrupted request would be accepted and acted on, with
        # the corruption reaching whatever the method does with that string. The
        # wire is UTF-8 by specification, so invalid bytes are the client's bug
        # and worth telling it about.
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        on_protocol_error(_error_frame(PARSE_ERROR, f"not UTF-8: {exc}"))
        return None
    try:
        return decode(text)
    except AcpProtocolError as exc:
        on_protocol_error(_error_frame(PARSE_ERROR, str(exc)))
        return None


def _oversized(max_frame_bytes: int) -> dict[str, Any]:
    return _error_frame(INVALID_REQUEST, f"frame exceeds {max_frame_bytes} bytes")


def _error_frame(code: int, message: str) -> dict[str, Any]:
    """A JSON-RPC error with a null id.

    ``error_response`` in the protocol module takes the id of the request being
    answered; here there is no readable request, and the spec's own answer for
    that case is ``null``.
    """
    return {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}


__all__ = [
    "MAX_FRAME_BYTES",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "claim_stdout",
    "open_stdin",
    "read_frames",
    "write_frame",
]
