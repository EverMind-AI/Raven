"""Reusable RPC stack assembly for a transport that owns its own frame sink.

The engine half of ``tui_commands._run_rpc_server_until_done``: an AgentLoop,
the three prompt brokers, a SubscriptionEmitter, the spine scheduler and the
method registrations, minus any transport. The caller supplies ``send_frame``,
so a transport that is not a socket (a stdio protocol, a WebSocket) gets the
same engine without reimplementing the wiring.

Why the socket path does not call this, though its shape came from there:
``RpcServer`` is constructed FROM the dispatcher and only then can provide
``send_frame``, while this function creates the dispatcher itself. Inverting
that is a change to the startup path every terminal session depends on, and it
buys nothing until a second socket transport exists. So the duplication is
deliberate and bounded, and the reason is written here rather than left for
somebody to rediscover.

What a caller does NOT get here, and why -- so that anyone reconciling this file
with a fuller assembly elsewhere has the list rather than a diff to interpret:
a DAG progress sink, a sub-agent delivery sink and an MCP event sink (nothing on
this side sets them), the direct-chat target map (no direct chat here), and
per-connection scoping of the approval and question brokers (one process serves
one client here, so a broadcast IS the one client; scoping matters only where
several sockets share a dispatcher).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

SendFrame = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class RpcStack:
    """Everything a transport needs back from the assembly.

    ``build_error`` is latched rather than raised: a bad provider config must
    still let the client connect and be told what is wrong, which is why the
    factory closure below re-raises it on first use instead.
    """

    dispatcher: Any
    emitter: Any
    agent_loop: Any
    build_error: Any
    teardown: Callable[[], Awaitable[None]]
    # The ask_user / deep-research broker. A caller that answers questions its
    # own way (a protocol with an elicitation method of its own) needs the handle
    # to rebind or wrap it.
    question_broker: Any = None


async def build_rpc_stack(
    send_frame: SendFrame,
    *,
    channel: str = "tui",
    approval_responder: Any = None,
) -> RpcStack:
    """Assemble dispatcher + engine wired to ``send_frame``.

    Must run inside the event loop that will serve requests: cron start and the
    spine scheduler both bind to the running loop.

    ``channel`` names the delivery channel this stack's turns run on, and it has
    to reach BOTH collaborators. ``build_tui`` registers its outlet under this
    name and ``register_turn_methods`` stamps it on every turn it submits as
    ``source.channel``; the hub routes a deliverable by that name, so passing it
    to one and not the other loses every turn with no error anywhere. It is its
    own channel per surface and not a shared name because session keys are
    prefixed with it and the session listing filters on that prefix -- sharing
    would put one surface's sessions in another's picker.

    ``approval_responder`` replaces the transport shell approvals are asked
    over. The broker built here emits ``approval.request`` on the same
    ``send_frame``, which is right for a client that implements that method and
    useless for one that does not. Only the transport is replaced: the
    classification, the one-command scope and the absence of any always-allow
    state all stay where they are. The local broker is still constructed either
    way, because ``approval.respond`` is registered from it and a caller
    overriding the responder is not necessarily removing that method.
    """
    from raven.cli.tui_commands import (
        _build_cron_callback_spine,
        _build_tui_agent_loop,
        _fanout_cron_missed,
    )
    from raven.rpc.approval_broker import ApprovalBroker
    from raven.rpc.confirm_broker import ConfirmBroker
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.errors import RpcError
    from raven.rpc.methods import register_aligned_methods_except_system
    from raven.rpc.methods import turn as turn_module
    from raven.rpc.methods.system import register_system_methods
    from raven.rpc.question_broker import QuestionBroker
    from raven.rpc.spine import build_tui
    from raven.rpc.subscriptions import SubscriptionEmitter

    dispatcher = Dispatcher()
    emitter = SubscriptionEmitter(send_frame=send_frame)
    # Three brokers rather than one: a shell approval is not a conversational
    # confirmation. It binds one exact command to one turn, has dual deadlines,
    # and always fails closed when the transport disappears.
    confirm_broker = ConfirmBroker(send_frame=send_frame)
    approval_broker = ApprovalBroker(send_frame=send_frame)
    question_broker = QuestionBroker(send_frame=send_frame)

    # Eager, so a bad provider config surfaces at connect rather than on the
    # first turn; latched, so the client still gets to connect and be told.
    build_error: RpcError | None = None
    agent_loop = None
    try:
        agent_loop = _build_tui_agent_loop()
    except RpcError as e:
        build_error = e

    # Late-bound now that the tool registry exists. deep_research goes through
    # the loop rather than the tool, so a tool built later by a mid-session
    # enable inherits the broker too.
    if agent_loop is not None:
        if (ask_tool := agent_loop.tools.get("ask_user")) is not None and hasattr(ask_tool, "set_broker"):
            ask_tool.set_broker(question_broker)
        agent_loop.set_deep_research_broker(question_broker)

    def _agent_loop_factory():
        if agent_loop is not None:
            return agent_loop
        if build_error is not None:
            raise build_error
        return None

    turn_scheduler = None
    turn_ids: dict[str, str] = {}
    turn_teardown = None
    if agent_loop is not None:
        from raven.cli._cron_handler import make_on_cron_job

        # The spine is built before cron is wired: a reminder submits a CRON turn
        # through this scheduler, captured non-streaming and read back so the
        # wrapper can fan it out as a cron.delivered event.
        cron_readback: dict[str, str] = {}
        turn_scheduler, _turn_hub, turn_ids, turn_teardown = build_tui(
            agent_loop,
            emitter,
            channel=channel,
            on_turn_end=turn_module.clear_active,
            readback_texts=cron_readback,
            approval_responder=approval_responder or approval_broker,
        )
        agent_loop.subagents.set_submit(turn_scheduler.submit)
        if agent_loop.cron_service is not None:
            base_on_cron = make_on_cron_job(
                submit=turn_scheduler.submit,
                readback_texts=cron_readback,
                default_channel=channel,
                cron_service=agent_loop.cron_service,
            )
            agent_loop.cron_service.on_job = _build_cron_callback_spine(base_on_cron, emitter)
            # on_job must be wired before start(), or an immediately-firing job
            # has no callback.
            await agent_loop.cron_service.start()
            # start() dropped past-due one-shot reminders on this runner's
            # partition. A transport that reaches the runtime through here rather
            # than through tui_commands would otherwise collect the drops and
            # never tell anyone.
            if agent_loop.cron_service.last_startup_drops:
                await _fanout_cron_missed(emitter, drops=agent_loop.cron_service.last_startup_drops)

    register_system_methods(dispatcher)
    register_aligned_methods_except_system(
        dispatcher,
        emitter=emitter,
        agent_loop_factory=_agent_loop_factory,
        approval_broker=approval_broker,
        confirm_broker=confirm_broker,
        question_broker=question_broker,
        scheduler=turn_scheduler,
        turn_ids=turn_ids,
        build_error=build_error,
        default_channel=channel,
    )

    backend_start: asyncio.Task[None] | None = None
    if agent_loop is not None and agent_loop.backend is not None:
        # Backgrounded because it may spawn a server and take tens of seconds;
        # a first render must not wait on the memory path.
        async def _start_backend() -> None:
            try:
                await agent_loop.backend.start()
            except Exception:
                logger.exception("rpc: memory backend start failed; continuing with degraded memory path")

        # Held, not fire-and-forget: teardown has to settle it before it stops
        # the backend, or a client that connects and closes while EverOS is
        # still starting leaves the service running behind a stack that has
        # already reported itself closed -- holding the embedded index lock the
        # next process needs.
        backend_start = asyncio.create_task(_start_backend())

    async def teardown() -> None:
        # Pending UI waits are released before the transport goes away.
        # Cancelling an approval is denial, which keeps a disconnect fail-closed;
        # an ordinary confirm keeps its configured default.
        confirm_broker.cancel_all()
        approval_broker.cancel_all()
        # The third broker on the same transport. Without this an ask_user
        # outside an active Spine turn survives the disconnect and stays alive
        # until the broker's own default timeout, which is ten minutes.
        question_broker.cancel_all()
        if agent_loop is not None and agent_loop.cron_service is not None:
            try:
                agent_loop.cron_service.stop()
            except Exception:
                logger.exception("rpc: cron stop failed; continuing shutdown")
        if turn_teardown is not None:
            try:
                await turn_teardown()
            except Exception:
                logger.exception("rpc: turn spine teardown failed; continuing shutdown")
        # Drain before stop, and both before returning: the drain flushes what a
        # turn wrote and has not persisted, and the stop releases the embedded
        # index lock the next process needs. Stopping without draining loses the
        # last turn's memory writes silently.
        if backend_start is not None and not backend_start.done():
            # Cancelled rather than awaited: start may take tens of seconds and
            # teardown is on the exit path. ``stop`` is contractually safe after
            # a failed start (MemoryBackend.stop cleans up partial-init state),
            # so cancel-then-stop releases what a half-finished start created --
            # while running the two concurrently would have stop racing it.
            backend_start.cancel()
            try:
                await backend_start
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("rpc: memory backend start failed during teardown")
        if agent_loop is not None and agent_loop.backend is not None:
            try:
                await agent_loop.drain_backend_stores()
                await agent_loop.backend.stop()
            except Exception:
                logger.exception("rpc: memory backend stop failed; continuing shutdown")

    return RpcStack(
        dispatcher=dispatcher,
        emitter=emitter,
        agent_loop=agent_loop,
        build_error=build_error,
        teardown=teardown,
        question_broker=question_broker,
    )


__all__ = ["RpcStack", "SendFrame", "build_rpc_stack"]
