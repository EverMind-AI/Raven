"""The ACP agent's run loop: one connection, its engine, and its teardown.

The shape is deliberately flat (main repo ``raven/acp/server.py``): the client
spawns one process, speaks to it over one pipe pair, and kills it when done --
no accept loop, no connection registry. What there is instead:

* **one engine per process.** The caller's factory builds the agent loop once,
  and :func:`raven.acp.spine.build_acp` wires the turn spine onto it with the
  frame writer as its outbound face.
* **every inbound frame in its own task.** ``session/prompt`` is suspended for
  as long as the turn takes, and ``session/cancel`` has to be read *during*
  it. Handling frames inline would make cancellation unreachable -- the one
  thing the protocol requires to always work.
* **a shutdown that answers instead of cancelling.** On EOF the pending
  prompts are settled as ``cancelled`` and only then are the handler tasks
  awaited, so they return through their own code. A finite input (``echo ... |
  raven acp``, a smoke test) therefore gets its replies; cancellation stays as
  the backstop for a handler stuck somewhere else.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from typing import Any, BinaryIO

from loguru import logger

from raven.acp.methods import AcpMethods
from raven.acp.spine import build_acp
from raven.acp.stdio import read_frames, write_frame

SHUTDOWN_GRACE_S = 5.0
"""How long a handler gets to finish after the client closed stdin.

Bounded because the alternative is a process that will not exit: a handler
stuck on something other than a prompt future has nothing to resolve it.
"""

ACP_CHANNEL = "acp"
"""The delivery channel ACP turns run on. Its own channel rather than reusing
``"tui"``: session keys are prefixed with it, so sharing the name would file an
ACP client's sessions among the terminal's."""


async def serve(
    reader: asyncio.StreamReader,
    out: BinaryIO,
    *,
    agent_loop_factory: Callable[[], Any],
    channel: str = ACP_CHANNEL,
) -> None:
    """Serve ACP on one reader/writer pair until the client closes stdin.

    ``agent_loop_factory`` is a named seam so a test can drive this against a
    stub loop instead of standing up providers and a memory backend to
    exchange two frames. Everything built here is torn down on the way out.
    """

    def emit(frame: dict[str, Any]) -> None:
        write_frame(out, frame)

    agent_loop = agent_loop_factory()
    workspace = getattr(agent_loop, "workspace", None)
    scheduler, _hub, sessions, teardown = build_acp(
        agent_loop,
        emit,
        channel=channel,
        workspace=str(workspace) if workspace else None,
    )
    methods = AcpMethods(
        submit=scheduler.submit,
        sessions=sessions,
        emit=emit,
        # The engine's OWN manager: session/load reads through it, and the turn
        # that follows must see the same cache (see AcpMethods docstring).
        session_manager=getattr(agent_loop, "sessions", None),
        channel=channel,
    )
    logger.info("acp: engine ready on channel {}", channel)

    tasks: set[asyncio.Task[None]] = set()
    try:
        async for frame in read_frames(reader, emit):
            task = asyncio.create_task(_answer(methods, frame, emit))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        logger.info("acp: client closed stdin")
    finally:
        await _drain(sessions, tasks)
        try:
            await teardown()
        except Exception:
            # Teardown is best-effort; a failure in the teardown callable
            # itself must not replace a clean exit with a traceback the client
            # cannot see anyway.
            logger.exception("acp: teardown failed")


async def _answer(methods: AcpMethods, frame: dict[str, Any], emit: Any) -> None:
    """Route one frame and write its answer, if it has one.

    Logged here rather than left to the gather at shutdown: a task's exception
    retrieved there is discarded, so a write that failed mid-session (a full
    pipe, a client gone) would leave no trace. ``CancelledError`` is excluded
    because shutdown cancellation is not a fault, and re-raised because
    swallowing it would report the task as completed normally.
    """
    try:
        response = await methods.handle(frame)
        if response is not None:
            emit(response)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("acp: answering {} failed", frame.get("method"))
        raise


async def _drain(sessions: Any, tasks: set[asyncio.Task[None]]) -> None:
    """Let every in-flight handler finish, then cancel whatever is left.

    ``sessions.close()`` is what makes the wait finite without cancelling: a
    suspended ``session/prompt`` waits on a future only the event stream
    resolves, and the stream has nothing more to say once the client is gone.
    """
    sessions.close()
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
    """Route unhandled exceptions into the log file.

    Both paths are otherwise invisible in this deployment: a top-level
    exception's traceback goes to stderr (which the client surfaces) but not to
    the log file; an asyncio task's exception goes nowhere until GC. stderr is
    deliberately kept as a destination too -- a crash that is only in a log
    file is a crash nobody is told about.
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
        # Called before the loop exists; the excepthook half still covers a
        # failure during startup.
        logger.debug("acp: no running loop yet; asyncio handler not installed")


__all__ = ["ACP_CHANNEL", "SHUTDOWN_GRACE_S", "install_crash_handlers", "serve"]
