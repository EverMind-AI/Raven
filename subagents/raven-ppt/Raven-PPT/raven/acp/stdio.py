"""Making stdout safe to speak a protocol on.

An ACP agent's stdout is a JSON-RPC channel, not a place to print. Every other
writer in the process shares that descriptor -- loguru, a library banner, a
traceback, LibreOffice's own chatter through a subprocess that inherited it -- and
one line from any of them is a frame the client cannot decode. What the client
sees is not "the agent logged something"; it is a protocol violation, and the
session it was half way through is gone.

Nothing here knows an ACP method name. This is the layer that makes the channel
speakable, kept apart from anything that speaks.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator, Callable, Generator
from typing import Any, BinaryIO

from raven.acp.protocol import INVALID_REQUEST, PARSE_ERROR, AcpProtocolError, decode, encode

MAX_FRAME_BYTES = 8 * 1024 * 1024
"""One ACP frame is not a line of text.

``session/prompt`` may carry embedded resources, and base64 inflates by about
4/3, so a 4 MB attachment arrives as a single frame of roughly 5.5 MB. A cap
sized for ordinary RPC traffic would reject input this protocol is specified to
accept.
"""

_READ_CHUNK = 64 * 1024


@contextlib.contextmanager
def claim_stdout() -> Generator[BinaryIO, None, None]:
    """Hand the protocol its own descriptor, and point fd 1 at stderr.

    The returned writer owns a duplicate of the original fd 1. For the duration of
    the block fd 1 *is* stderr, so anything that writes to stdout by any route --
    ``sys.stdout``, ``os.write(1, ...)``, a C extension holding the descriptor, a
    child process that inherited it -- lands in the log instead of the frame
    stream.

    Replacing ``sys.stdout`` alone would not do: it covers only writers that go
    through Python's own object, which is precisely the set that was never the
    problem. The descriptor is the shared resource, so the descriptor is what gets
    moved. This build has a specific reason to care -- the render path shells out
    to ``soffice``, which inherits fd 1 and is not quiet.

    The writer is buffered and :func:`write_frame` flushes each frame, rather than
    unbuffered. A raw ``FileIO.write`` is a single ``write(2)`` that may return
    fewer bytes than it was handed, and nothing checks the return value -- so a
    short write would be a silently truncated frame with the next frame
    concatenating onto its tail. ``BufferedWriter.flush`` retries short writes
    itself and raises rather than discarding. Flushing per frame keeps the
    property that matters: a frame left in a buffer is a client waiting forever
    for a reply that was already computed, which reads as a hang rather than as
    slowness and is the harder of the two to diagnose.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    protocol_fd = os.dup(1)
    # Guarded as its own step: ``dup2`` fails when fd 2 is closed, and ``fdopen``
    # can fail after fd 1 has already been moved. Leaving either failure unhandled
    # would leak the descriptor and, worse, hand the caller an exception while the
    # process kept running with its stdout pointed at stderr and nobody left
    # holding the original.
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
        # ``sys.stdout`` object during the block is sitting in that object's
        # buffer, since stdout is a pipe and therefore block-buffered. Restoring
        # fd 1 first would leave those bytes to be flushed at interpreter
        # shutdown, when fd 1 is the wire again.
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        os.dup2(protocol_fd, 1)
        os.close(protocol_fd)


def write_frame(writer: BinaryIO, frame: dict[str, Any]) -> None:
    """Put one frame on the wire, whole, before returning.

    The flush is the contract: it is what makes a short write the buffered layer's
    problem instead of a truncated frame, and what keeps the frame from waiting in
    a buffer for the next one to push it out.
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

    Two failures are answered rather than raised. An agent that dies on malformed
    input leaves every pending request unresolved, and a client whose promise
    never settles is worse off than one holding an error: it has nothing to show
    and nothing to retry.

    - A line longer than ``max_frame_bytes``. The oversized bytes are dropped up
      to and including their newline, so the next frame starts where a frame
      starts. Both the error and the resynchronisation are the point: without the
      drop, reading resumes in the middle of the discarded frame and every frame
      after it is garbage too.
    - A line that is not a JSON object.

    Both are reported through ``on_protocol_error`` as a JSON-RPC error frame with
    a null ``id``, because the id lived in the bytes that could not be read. EOF
    ends the iteration: that is the client closing the session, not a failure to
    report to it.

    The line buffer is this function's own rather than ``StreamReader.readuntil``'s.
    ``readuntil`` raises ``LimitOverrunError`` while the separator is still beyond
    its limit and leaves the bytes in place, so every retry raises again and the
    stream never advances; and reading fixed chunks to drain it would swallow the
    start of the following frame, which is already in the same chunk. Owning the
    buffer is what makes the recovery correct.
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
        # parses -- so a corrupted request would be accepted and acted on. The wire
        # is UTF-8 by specification, so invalid bytes are the client's bug and
        # worth telling it about.
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
    """A JSON-RPC error with a null id: there is no readable request to answer,
    and the spec's own answer for that case is ``null``."""
    return {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}


__all__ = ["MAX_FRAME_BYTES", "claim_stdout", "read_frames", "write_frame"]
