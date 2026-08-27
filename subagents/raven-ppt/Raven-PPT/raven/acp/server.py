"""The ACP agent's run loop: one connection, its sessions, and its teardown.

The shape is deliberately flat. A client spawns one process, speaks to it over
one pipe pair, and closes stdin when it is done -- so there is no accept loop, no
connection registry, no multi-tenant bookkeeping. What there is instead:

* **Every inbound frame in its own task.** ``session/prompt`` is suspended for as
  long as the turn takes -- for a deck that is tens of minutes -- and
  ``session/cancel`` has to be read *during* it. Handling frames inline would make
  cancellation unreachable, which is the one thing the protocol requires to always
  work.
* **A shutdown that answers instead of cancelling.** On EOF the sessions are
  released, which settles every pending prompt as ``cancelled`` -- what the spec
  says a torn-down turn resolves as -- and only then are the handler tasks
  awaited. They therefore return through their own code, releasing their turn
  slots and writing their answers, rather than being cancelled mid-flight. It also
  means a finite input (a scripted client, a smoke test piping three frames) gets
  its replies. Cancellation stays as the backstop for a handler stuck somewhere
  else.
* **No engine until a session asks for one.** ``session/new`` builds it, because
  that is the call the client bounds with ``readyTimeoutMs`` -- and because the
  workspace it needs is the session's.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, BinaryIO

from loguru import logger

from raven.acp.engine import build_engine_factory
from raven.acp.methods import AcpMethods
from raven.acp.outbound import OutboundRequests
from raven.acp.questions import AcpQuestions
from raven.acp.session import SessionTable
from raven.acp.stdio import read_frames, write_frame

SHUTDOWN_GRACE_S = 30.0
"""How long a handler gets to finish after the client closed stdin.

Bounded because the alternative is a process that will not exit: a handler stuck
on something other than a prompt future has nothing to resolve it. Longer than a
generic RPC grace because a cancelled deck turn unwinds through a render that may
already be running, and killing it mid-write leaves a truncated pptx behind.
"""

ACP_CHANNEL = "acp"
"""The delivery channel ACP turns run on.

Its own channel rather than reusing ``"cli"``: the hub routes a deliverable by its
source channel to the outlet registered under that name, and session keys are
prefixed with it -- so sharing the name would mix an ACP session into the
launcher's own store.
"""


async def serve(
    reader: asyncio.StreamReader,
    out: BinaryIO,
    *,
    jobs_root: Path,
    config: Any,
    channel: str = ACP_CHANNEL,
    engine_factory: Any = None,
) -> None:
    """Serve ACP on one reader/writer pair until the client closes stdin.

    Returns when the reader reaches EOF, which is how a client says it is gone.
    Everything built here is torn down on the way out, including each session's
    engine and its children (MCP subprocesses, the memory backend's HTTP client),
    because a process that exits while holding those leaves the next launch to
    fight for them.

    ``engine_factory`` is a named seam, not a knob: it lets a test drive this
    whole loop over an in-memory pipe against a scripted engine, instead of
    standing up a provider and a memory backend to exchange four frames. Left
    unset in production, where it is built from ``config``.
    """

    def emit(frame: dict[str, Any]) -> None:
        write_frame(out, frame)

    sessions = SessionTable()
    outbound = OutboundRequests(emit)
    questions = AcpQuestions(outbound=outbound, sessions=sessions, emit=emit)
    methods = AcpMethods(
        emit=emit,
        sessions=sessions,
        engine_factory=engine_factory or build_engine_factory(config, emit, sessions, questions),
        jobs_root=jobs_root,
        channel=channel,
        outbound=outbound,
        questions=questions,
    )
    logger.info("acp: ready on channel {}; jobs under {}", channel, jobs_root)

    tasks: set[asyncio.Task[None]] = set()
    try:
        async for frame in read_frames(reader, emit):
            task = asyncio.create_task(_answer(methods, frame, emit))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        logger.info("acp: client closed stdin")
    finally:
        # Four steps, in this order, and the order is the whole of it.
        #
        # 1. Settle every pending prompt. That is what lets a handler suspended on
        #    a turn's future reach its own cleanup instead of waiting out the grace
        #    period -- and it must not tear the engine down, because the code the
        #    handler returns through still reads it.
        # 2. Let the handlers finish. Draining before this would leave a session
        #    created by a handler that had not returned yet -- ``session/new`` is
        #    awaiting its engine build when EOF arrives -- registered with nothing
        #    left to release it, so its MCP subprocesses would outlive the process.
        # 3. Release the sessions, which is where the engines are torn down.
        # 0. Fail every request still waiting on the client, before anything
        #    else. A handler suspended on a question would otherwise wait out the
        #    ask's own deadline inside step 2, long past the grace period, and the
        #    tool call it belongs to would never be told the client had gone.
        outbound.close()
        await questions.drain()
        sessions.settle_all()
        await _drain(tasks)
        try:
            await sessions.aclose()
        except Exception:
            logger.exception("acp: releasing sessions failed")


async def _answer(methods: AcpMethods, frame: dict[str, Any], emit: Any) -> None:
    """Route one frame and write its answer, if it has one.

    The write is here rather than in the caller because the caller has already
    moved on to the next frame by the time this finishes -- that being the point of
    the task.

    Logged here rather than left to the gather at shutdown: a task's exception is
    retrieved by that gather and then discarded, so a write that failed mid-session
    -- a full pipe, a client that went away -- would leave no trace anywhere.
    ``CancelledError`` is excluded because shutdown cancellation is not a fault.
    """
    try:
        response = await methods.handle(frame)
        if response is not None:
            emit(response)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("acp: answering {} failed", frame.get("method"))


async def _drain(tasks: set[asyncio.Task[None]]) -> None:
    """Let every in-flight handler finish, then cancel whatever is left.

    Nothing is cancelled unless the grace period runs out, and the wait is awaited
    to completion either way -- returning while a handler is still executing would
    have its engine torn down underneath it.
    """
    pending = [task for task in tuple(tasks) if not task.done()]
    if not pending:
        return
    _, still_running = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_S)
    if still_running:
        logger.warning("acp: {} handler(s) did not finish in {}s; cancelling", len(still_running), SHUTDOWN_GRACE_S)
        for task in still_running:
            task.cancel()
        await asyncio.gather(*still_running, return_exceptions=True)


def install_crash_handlers() -> None:
    """Route unhandled exceptions into the log.

    Two paths, both otherwise invisible in this deployment. A top-level
    exception's traceback goes to stderr, which an ACP client shows -- but not to
    the log file, which is where the rest of the story is. An asyncio task's
    exception goes nowhere at all until the task is garbage collected, and then
    only as "Task exception was never retrieved" with no context.

    stderr is deliberately left as a destination as well: the client surfaces it,
    and a crash that is only in a log file is a crash nobody is told about.
    """
    previous = sys.excepthook

    def hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        logger.opt(exception=(exc_type, exc, tb)).error("acp: unhandled exception")
        previous(exc_type, exc, tb)

    sys.excepthook = hook

    def on_loop_error(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        message = context.get("message") or "asyncio error"
        exception = context.get("exception")
        if exception is not None:
            logger.opt(exception=exception).error("acp: {}", message)
        else:
            logger.error("acp: {} ({})", message, {k: v for k, v in context.items() if k != "message"})

    try:
        asyncio.get_running_loop().set_exception_handler(on_loop_error)
    except RuntimeError:
        # Called before the loop exists. The excepthook half is still installed,
        # which is the half that covers a failure during startup.
        logger.debug("acp: no running loop yet; asyncio handler not installed")


__all__ = ["ACP_CHANNEL", "SHUTDOWN_GRACE_S", "install_crash_handlers", "serve"]
