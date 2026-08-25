"""The ACP agent's run loop: one connection, its engine, and its teardown.

The shape is deliberately flat. An editor spawns one process, speaks to it over
one pipe pair, and kills it when the window closes -- so there is no accept loop,
no connection registry, and no multi-tenant bookkeeping. What there is instead:

* **one engine per process.** ``build_rpc_stack`` builds the agent loop, the
  brokers, the subscription emitter and the turn spine, and the ACP layer is a
  translator sitting on its outbound face. The alternative -- mounting onto a
  resident gateway -- was the earlier preference and does not survive the process
  topology: an ACP agent *is* a stdio child of the editor, so there is nothing to
  mount onto unless a gateway happens to be running, and an agent that works only
  when another service is up is not an agent an editor can launch.
* **every inbound frame in its own task.** ``session/prompt`` is suspended for as
  long as the turn takes, and ``session/cancel`` has to be read *during* it.
  Handling frames inline would make cancellation unreachable, which is the one
  thing the protocol requires to always work.
* **a shutdown that answers instead of cancelling.** On EOF the pending prompts
  are settled as ``cancelled`` -- which is what the spec says a torn-down turn
  resolves as -- and only then are the handler tasks awaited. They therefore
  return through their own code, releasing their turn slots and writing their
  answers, rather than being cancelled mid-flight. It also means a finite input
  (``echo '...' | raven acp``, a scripted client, a smoke test) gets its replies:
  cancelling on EOF would answer a batch of three requests with nothing at all.
  Cancellation stays as the backstop for a handler that is stuck somewhere else.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, BinaryIO

from loguru import logger

from raven.acp.methods import AcpMethods
from raven.acp.outbound import OutboundRequests
from raven.acp.permissions import AcpPermissionBroker
from raven.acp.questions import AcpQuestions
from raven.acp.stdio import read_frames, write_frame
from raven.acp.updates import UpdateTranslator

SHUTDOWN_GRACE_S = 5.0
"""How long a handler gets to finish after the client closed stdin.

Bounded because the alternative is a process that will not exit: a handler stuck
on something other than a prompt future has nothing to resolve it. Five seconds
is the same budget ``DirectExecutor`` gives a killed process to be reaped -- long
enough for a return path, short enough that an editor closing a window does not
leave a process behind.
"""

ACP_CHANNEL = "acp"
"""The delivery channel ACP turns run on.

Its own channel rather than reusing ``"tui"``: session keys are prefixed with it,
and the session listing filters by that prefix, so sharing the name would put an
editor's sessions in the terminal's picker and vice versa. It has to be given to
``build_rpc_stack``, which passes it to *both* the spine outlet and the turn
methods -- see the note there on why one without the other delivers nothing.
"""


async def serve(reader: asyncio.StreamReader, out: BinaryIO, *, channel: str = ACP_CHANNEL) -> None:
    """Serve ACP on one reader/writer pair until the client closes stdin.

    Returns when the reader reaches EOF, which is how an editor says the window
    is gone. Everything built here is torn down on the way out, including the
    engine's own children (the browser profile, MCP subprocesses, the sub-agent
    pool), because a process that exits while holding those leaves the next
    launch to fight for a profile lock.
    """

    def emit(frame: dict[str, Any]) -> None:
        write_frame(out, frame)

    outbound = OutboundRequests(emit=emit)
    # Built before the stack, because the stack's ``send_frame`` *is* the
    # translator's sink: the hook has to be in place before the first frame can
    # arrive on it. Its broker is bound afterwards, which is why it takes one
    # late -- see ``AcpQuestions``.
    translator = UpdateTranslator(emit=emit, side_channel=lambda method, params: questions.handle(method, params))
    questions = AcpQuestions(outbound=outbound, translator=translator, emit=emit)
    permissions = AcpPermissionBroker(outbound=outbound, translator=translator)
    # Declared BEFORE the engine is built, because a policy reads the surface's
    # families at construction: set afterwards, the tools that already exist keep
    # the families they were born with and the declaration reaches nothing.
    _ask_before_external_effects()
    stack = await build_stack(translator, channel=channel, approval_responder=permissions)
    questions.set_broker(stack.question_broker)
    methods = AcpMethods(
        dispatcher=stack.dispatcher,
        translator=translator,
        emit=emit,
        agent_loop=stack.agent_loop,
        outbound=outbound,
        questions=questions,
        channel=channel,
    )
    logger.info("acp: engine ready on channel {} ({} methods)", channel, len(stack.dispatcher.methods()))

    tasks: set[asyncio.Task[None]] = set()
    try:
        async for frame in read_frames(reader, emit):
            task = asyncio.create_task(_answer(methods, frame, emit))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        logger.info("acp: client closed stdin")
    finally:
        # A question that has just been answered gets to deliver that answer to
        # the broker; then the outbound futures are failed, which is what lets a
        # handler suspended on a permission prompt reach its own cleanup.
        try:
            await questions.drain()
        except Exception:
            logger.exception("acp: draining questions failed")
        outbound.close()
        await _drain(translator, tasks)
        # Before the engine teardown, which does not touch the emitter: each
        # subscription owns a task, and one left running is reported at exit on
        # the stream the client shows.
        try:
            await methods.unsubscribe_all()
        except Exception:
            logger.exception("acp: closing subscriptions failed")
        try:
            await stack.teardown()
        except Exception:
            # Teardown is best-effort by construction (every step inside it is
            # already individually guarded); this catches a failure in the
            # teardown *callable* itself, which would otherwise replace a clean
            # exit with a traceback the client cannot see anyway.
            logger.exception("acp: teardown failed")


async def build_stack(
    translator: UpdateTranslator,
    *,
    channel: str = ACP_CHANNEL,
    approval_responder: Any = None,
) -> Any:
    """Assemble the RPC stack with the translator as its outbound sink.

    A named seam rather than an inline call, so a test can drive :func:`serve`
    against a stack it built itself instead of standing up an agent loop, a cron
    service and a memory backend to exchange two frames.
    """
    from raven.rpc.bootstrap import build_rpc_stack

    return await build_rpc_stack(translator.send_frame, channel=channel, approval_responder=approval_responder)


def _ask_before_external_effects() -> None:
    """Declare the command families this surface must ask about.

    The built-in policy asks about exactly one family, deletion -- which fits a
    terminal the reader is already watching and does not fit an agent behind an
    editor, where ``git push``, ``npm install`` and ``curl -o`` would otherwise
    run with nothing on screen. This is the single most visible difference
    between an agent somebody trusts and one they do not.

    Per process rather than per tool, and that is the fix for what used to be a
    hole here. Registering on the main loop's ``ExecTool`` reached the main loop
    only: a sub-agent builds its own tool with its own policy, so a delegated
    ``git push`` ran unannounced while the identical command asked in the main
    agent. The process IS the surface in this deployment -- one editor, one stdio
    child -- so the declaration belongs at that scope and every tool built here,
    delegated or not, inherits it.

    What a delegated command gets is a REFUSAL, not a prompt: a sub-agent's tool
    has no approval responder, and a tool that cannot ask fails closed. That is
    the deliberate half of this decision. Asking on a sub-agent's behalf means
    routing a lane's conversation id into a task that outlives its turn, which is
    its own change; until then, refusing with a reason beats acting in silence.
    """
    from raven.agent.tools.shell_policy import EXTERNAL_EFFECT_MATCHERS, set_surface_approval_families

    set_surface_approval_families(EXTERNAL_EFFECT_MATCHERS)
    logger.info("acp: {} command families will ask before running", len(EXTERNAL_EFFECT_MATCHERS))


async def _answer(methods: AcpMethods, frame: dict[str, Any], emit: Any) -> None:
    """Route one frame and write its answer, if it has one.

    The write is here rather than in the caller because the caller has already
    moved on to the next frame by the time this finishes -- that being the point
    of the task.

    Logged here rather than left to the gather at shutdown. A task's exception is
    retrieved by that gather and then discarded, so a write that failed
    mid-session -- a full pipe, a client that went away -- would leave no trace
    anywhere. ``CancelledError`` is excluded because shutdown cancellation is not
    a fault, and re-raised because swallowing it would report the task as having
    completed normally.
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


async def _drain(translator: UpdateTranslator, tasks: set[asyncio.Task[None]]) -> None:
    """Let every in-flight handler finish, then cancel whatever is left.

    ``translator.close()`` is what makes the wait finite without cancelling: a
    suspended ``session/prompt`` is waiting on a future that only the event stream
    resolves, and the stream has nothing more to say once the client is gone. It
    answers everything already waiting *and* latches, so a handler that had not
    started yet gets its answer when it opens its turn rather than waiting out the
    grace period.

    Nothing is cancelled unless that period runs out, and the wait is awaited to
    completion either way -- returning while a handler is still executing would
    have the engine torn down underneath it.
    """
    translator.close()
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

    Two paths, both of which are otherwise invisible in this deployment. A
    top-level exception's traceback goes to stderr, which an ACP client shows --
    but not to the log file, which is where the rest of the story is. An asyncio
    task's exception goes nowhere at all until the task is garbage collected, and
    then only as "Task exception was never retrieved" with no context.

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


__all__ = ["ACP_CHANNEL", "SHUTDOWN_GRACE_S", "build_stack", "install_crash_handlers", "serve"]
