"""``raven acp``: the ACP agent's process shell.

An editor spawns this command and speaks newline-delimited JSON-RPC to its stdin
and stdout. The command owns that channel and the frame loop, and answers no ACP
method at all: what it exists for is the part that has to be right before any
method can work, which is that the only bytes on stdout are frames, and that
input the agent cannot read produces an answer rather than a dead agent.

The methods land on top of this. Nothing here needs to change to add them.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from typing import Any

import typer
from loguru import logger

from raven.acp.stdio import INVALID_REQUEST, MAX_FRAME_BYTES, claim_stdout, read_frames, write_frame
from raven.agent.acp import protocol
from raven.cli._log_file import redirect_loguru_to_file

acp_app = typer.Typer(name="acp", help="Serve Raven as an ACP agent over stdio.")


@acp_app.callback(invoke_without_command=True)
def acp(ctx: typer.Context) -> None:
    """Serve the Agent Client Protocol on stdin/stdout."""
    if ctx.invoked_subcommand is not None:
        return
    asyncio.run(_serve())


async def _serve() -> None:
    """Own the stdio channel, then read frames until the client closes.

    loguru goes to a file, but fd 2 is deliberately left alone: an ACP client
    surfaces its agent's stderr, and taking that away would make a crash
    invisible from the side that can actually report it. What must not happen is
    a write reaching fd 1, and ``claim_stdout`` is what prevents that -- a stray
    ``print`` lands on stderr, where it is noise in a log rather than a frame the
    client cannot decode.
    """
    log_path = redirect_loguru_to_file("acp.log", retention=3, terminal_level="WARNING")
    with claim_stdout() as out:
        logger.info("acp: serving on stdio, logs at {}", log_path)

        def report(frame: dict[str, Any]) -> None:
            write_frame(out, frame)

        async with _open_stdin() as reader:
            async for frame in read_frames(reader, report):
                answer = _answer(frame)
                if answer is not None:
                    write_frame(out, answer)
        logger.info("acp: client closed stdin, exiting")


@contextlib.asynccontextmanager
async def _open_stdin() -> AsyncIterator[asyncio.StreamReader]:
    """A reader over fd 0, closed on the way out.

    The limit bounds the reader's own buffer, which is backpressure rather than
    a frame cap: framing is :func:`raven.acp.stdio.read_frames`'s own, precisely
    so an oversized frame can be answered instead of raising out of the
    transport.

    The transport is closed rather than left to the garbage collector. A dropped
    read transport is collected with the loop still holding its descriptor, and
    the unregister that follows fails on a descriptor that is already -1 -- an
    error raised somewhere with no caller to report it to.
    """
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    try:
        yield reader
    finally:
        transport.close()


def _answer(frame: dict[str, Any]) -> dict[str, Any] | None:
    """What to send back for one inbound frame, or ``None`` to stay silent.

    Three shapes, and the distinction between them is the whole of it:

    - a request (has ``id`` and ``method``) gets ``-32601``. An unimplemented
      method answered is not the same failure as one left unanswered: the first
      lets a client report an incompatible agent, the second leaves its promise
      pending for the life of the session.
    - a notification (``method``, no ``id``) gets silence, which is what the
      spec has for it. Answering one would be a frame the client has nowhere to
      route.
    - a response (``id``, no ``method``) gets silence too. It answers a request
      this agent never sent, so there is nothing to correlate it with and
      nothing to say about it.
    """
    if "method" not in frame:
        return None
    request_id = frame.get("id")
    if request_id is None:
        return None
    method = frame.get("method")
    if not isinstance(method, str):
        return protocol.error_response(request_id, INVALID_REQUEST, "method must be a string")
    return protocol.error_response(request_id, protocol.METHOD_NOT_FOUND, f"{method} is not implemented yet")


__all__ = ["acp_app"]
