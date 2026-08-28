"""Making stdout safe to speak a protocol on.

An ACP agent's stdout is a JSON-RPC channel, not a place to print. Every other
writer in the process shares that descriptor -- a library banner, a traceback, a
``print`` left behind from a debug session -- and one line from any of them is a
frame the client cannot decode: a protocol violation, and the session it was
halfway through is gone.

Nothing here knows an ACP method name. This is the layer that makes the channel
speakable, kept apart from anything that speaks. Ported from the main raven
repo's ``raven/acp/stdio.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator, Callable, Generator
from typing import Any, BinaryIO

from raven.acp.protocol import AcpProtocolError, decode, encode

# One ACP frame is not a line of text: ``session/prompt`` may carry embedded
# resources, and base64 inflates by about 4/3. Sized well past raven's own
# traffic so the cap only ever cuts a frame that is genuinely malformed.
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
    descriptor -- lands in the log instead of the frame stream. Replacing
    ``sys.stdout`` alone covers only writers that go through Python's own
    object, which is precisely the set that was never the problem.

    The writer is buffered and :func:`write_frame` flushes each frame: a raw
    ``FileIO.write`` is a single ``write(2)`` that may return short with nothing
    checking the return value -- a silently truncated frame, with the next frame
    concatenating onto its tail. ``BufferedWriter.flush`` retries short writes
    itself.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    protocol_fd = os.dup(1)
    # Guarded as its own step: ``dup2`` fails when fd 2 is closed (``raven acp
    # 2>&-``), and ``fdopen`` can fail after fd 1 has already been moved.
    # Either failure unhandled would leak the descriptor and leave the process
    # running with stdout pointed at stderr and nobody holding the original.
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
        # Before the restore, not after: bytes written through the ``sys.stdout``
        # object during the block sit in its buffer (a pipe is block-buffered),
        # and restoring fd 1 first would leave them to flush at interpreter
        # shutdown, when fd 1 is the wire again.
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        os.dup2(protocol_fd, 1)
        os.close(protocol_fd)


def write_frame(writer: BinaryIO, frame: dict[str, Any]) -> None:
    """Put one frame on the wire, whole, before returning.

    Encoding is :func:`raven.acp.protocol.encode` rather than a second
    serialiser, so both directions cannot drift on ``ensure_ascii`` or on
    whether the newline is part of the frame. The flush is the contract: a
    frame left in a buffer is a client waiting forever for a reply that was
    already computed.
    """
    writer.write(encode(frame))
    writer.flush()


async def read_frames(
    reader: asyncio.StreamReader,
    on_protocol_error: Callable[[dict[str, Any]], None],
    *,
    max_frame_bytes: int = MAX_FRAME_BYTES,
) -> AsyncIterator[dict[str, Any]]:
    """Yield one decoded frame per line, answering the client on bad input.

    Two failures are answered rather than raised -- an agent that dies on
    malformed input leaves every pending request unresolved:

    - a line longer than ``max_frame_bytes``: the oversized bytes are dropped up
      to and including their newline, so the next frame starts where a frame
      starts. Without the drop, reading resumes mid-frame and every frame after
      it is garbage too.
    - a line that is not a JSON object.

    Both are reported through ``on_protocol_error`` as a JSON-RPC error frame
    with a null ``id``, because the id lived in the bytes that could not be
    read. EOF ends the iteration: the client closing the session, not a failure.

    The line buffer is this function's own rather than ``readuntil``'s:
    ``readuntil`` raises ``LimitOverrunError`` while the separator is beyond its
    limit and leaves the bytes in place, so every retry raises again and the
    stream never advances. Owning the buffer is what makes the recovery correct.
    """
    pending = b""
    dropping = False
    while chunk := await reader.read(_READ_CHUNK):
        *lines, pending = (pending + chunk).split(b"\n")
        for line in lines:
            if dropping:
                # The tail of a frame already reported; its newline ends the
                # drop, and the drop puts the stream back in phase.
                dropping = False
                continue
            if len(line) > max_frame_bytes:
                on_protocol_error(_oversized(max_frame_bytes))
                continue
            frame = _decode_or_report(line, on_protocol_error)
            if frame is not None:
                yield frame
        if dropping:
            pending = b""
        elif len(pending) > max_frame_bytes:
            # Oversized and still incomplete: report now rather than buffering
            # the rest, and skip bytes until the newline lands.
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
        # Strict, not ``errors="replace"``: substituting U+FFFD inside a JSON
        # string can make a corrupted frame parse, so it would be accepted and
        # acted on. The wire is UTF-8 by specification.
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
    # A null id: the spec's own answer when the request's id lived in bytes
    # that could not be read.
    return {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}


__all__ = ["MAX_FRAME_BYTES", "PARSE_ERROR", "INVALID_REQUEST", "claim_stdout", "read_frames", "write_frame"]
