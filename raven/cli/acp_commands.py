"""``raven acp``: the ACP agent's process shell.

An editor spawns this command and speaks newline-delimited JSON-RPC to its stdin
and stdout. What lives here is only the process's own business -- claiming fd 1
for the protocol, sending the logs somewhere else, opening stdin as a stream, and
making a crash visible -- because that part has to be right before any method can
work: the only bytes on stdout must be frames.

The protocol itself is :mod:`raven.acp.server`, which this hands the channel to.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from collections.abc import AsyncIterator

import typer
from loguru import logger

from raven.acp.server import install_crash_handlers, serve
from raven.acp.stdio import MAX_FRAME_BYTES, claim_stdout
from raven.cli._log_file import redirect_loguru_to_file

_THREAD_CHUNK = 64 * 1024

acp_app = typer.Typer(name="acp", help="Serve Raven as an ACP agent over stdio.")


@acp_app.callback(invoke_without_command=True)
def acp(ctx: typer.Context) -> None:
    """Serve the Agent Client Protocol on stdin/stdout."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        asyncio.run(_serve())
    except Exception as exc:
        # Kept away from Typer's own handler, which renders a rich traceback with
        # ``show_locals`` on -- measured at 228 lines of stderr, with the value of
        # every local in every frame. stderr is the stream an ACP client displays,
        # and those frames hold config objects and request payloads. The full
        # traceback is in the log file, where the sink is configured not to
        # annotate it with values.
        logger.exception("acp: exiting on an unhandled failure")
        typer.echo(f"raven acp failed: {exc}", err=True)
        raise typer.Exit(code=1) from None


async def _serve() -> None:
    """Own the stdio channel, then serve the protocol until the client closes it.

    loguru goes to a file, but fd 2 is deliberately left alone: an ACP client
    surfaces its agent's stderr, and taking that away would make a crash
    invisible from the side that can actually report it. What must not happen is
    a write reaching fd 1, and ``claim_stdout`` is what prevents that -- a stray
    ``print`` lands on stderr, where it is noise in a log rather than a frame the
    client cannot decode.
    """
    log_path = redirect_loguru_to_file("acp.log", retention=3, terminal_level="WARNING")
    install_crash_handlers()
    with claim_stdout() as out:
        logger.info("acp: serving on stdio, logs at {}", log_path)
        async with _open_stdin() as reader:
            await serve(reader, out)
        logger.info("acp: exiting")


@contextlib.asynccontextmanager
async def _open_stdin() -> AsyncIterator[asyncio.StreamReader]:
    """A reader over fd 0, released on the way out.

    The limit bounds the reader's own buffer, which is backpressure rather than
    a frame cap: framing is :func:`raven.acp.stdio.read_frames`'s own, precisely
    so an oversized frame can be answered instead of raising out of the
    transport.

    Two paths, because ``connect_read_pipe`` does not accept every stdin. It
    refuses a regular file outright -- ``ValueError: Pipe transport is for
    pipes/sockets only`` -- so ``raven acp < script.jsonl``, which is how anyone
    first tries this by hand, would die with a traceback before reading a byte.
    The fallback reads the descriptor on a daemon thread and feeds the same
    reader, which costs a thread and gives up the transport's own backpressure
    (the reader's buffer grows past its limit rather than pausing a producer that
    cannot be paused) -- acceptable for the case that reaches it, which is a file
    of bounded size. An editor gets a pipe and never takes this branch.

    On the pipe path the transport is closed rather than left to the garbage
    collector: a dropped read transport is collected with the loop still holding
    its descriptor, and the unregister that follows fails on a descriptor that is
    already -1, raising somewhere with no caller to report it to.
    """
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    loop = asyncio.get_running_loop()
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
    read = getattr(stream, "read1", None) or stream.read

    def _pump() -> None:
        try:
            while True:
                chunk = read(_THREAD_CHUNK)
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


__all__ = ["acp_app"]
