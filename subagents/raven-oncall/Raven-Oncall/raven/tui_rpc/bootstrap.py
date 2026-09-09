"""Assemble this install's RPC stack over an arbitrary frame sink.

``cli/tui_commands.py`` has always built this -- dispatcher, brokers, agent loop,
spine -- but built it *into* a socket server, latched to a handshake and torn down
with the TUI subprocess. ``raven acp`` needs the same engine with none of that:
its transport is stdio, its client speaks the Agent Client Protocol rather than
this dispatcher's own wire, and it has no handshake to wait for.

So the assembly moves here and takes the sink as an argument. The TUI path is
untouched -- lifting it wholesale would mean proving a rewrite of the surface the
product is used through, to serve a second caller -- and the duplication is
deliberate and small: this function is the subset that a caller with its own
transport needs, and the parts it leaves out are exactly the parts that belong to
a socket.

What it leaves out, and why each is a decision rather than an omission:

* **Cron, and the on-call watcher with it.** An on-call loop is built on waking
  up later, and ACP has no way for an agent to start a turn: only the client's
  ``session/prompt`` does. Without a scheduler in this process a campaign submits
  its first job, arms a wake nothing will ever fire, and stops -- silently,
  looking for all the world like it is still running.

  So the schedule lives here. The store is this install's own
  (``<state root>/cron/jobs.json``), not the host raven's, so nothing is
  contended: what a second claimant on it would be is a ``wake_shell``, and this
  hosting does not start one. A fired job is submitted as a turn on the session
  that scheduled it, which is what puts the round on that session's own update
  stream -- ``updates.py`` says this outright, that a session's stream also
  carries turns the runtime submitted and their endings must not answer a
  client's prompt.
* **No handshake latch.** ACP's own ``initialize`` is the handshake, and it is
  answered by the protocol layer above this.
* **No memory backend start.** The caller owns process lifecycle; ``teardown``
  here stops only what this built.

The whole umbrella is registered even though the ACP layer dispatches five
methods, for the reason the TUI path already gives: a handler added later must
not need a second registration site to be reachable.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

SendFrame = Callable[[dict], Any]


@dataclass
class RpcStack:
    """What a transport needs to answer requests, and to stop cleanly.

    Field-for-field what mainline's ``raven/rpc/bootstrap.py`` returns, so the
    copied ``raven/acp/**`` reads it without an edit. ``build_error`` is carried
    rather than raised: a bad provider config must surface as a typed error on
    the first turn, not as a server that would not start, which is the same
    contract ``turn.send`` has always had here.
    """

    dispatcher: Any
    emitter: Any
    agent_loop: Any
    build_error: Any
    teardown: Callable[[], Awaitable[None]]
    question_broker: Any = None
    direct_targets: dict[str, dict[str, str]] = field(default_factory=dict)
    deliverables: Any = None


def _make_on_wake(submit: Callable[[Any], Any], channel: str) -> Callable[[Any], Awaitable[None]]:
    """Run a fired job as a turn on the session that scheduled it.

    The session is not looked up: it is spelled in the job. A cron job records the
    ``(channel, to)`` of the turn that created it, ``acp/methods.py`` gives every
    prompt the pair that rebuilds its own session key, and ``<channel>:<chat_id>``
    IS that key -- so the job carries its own addressee and no registry has to be
    kept in step with one.

    Submitted on that key as the conversation, rather than on a lane of the job's
    own, because the point is the session's update stream: an ACP client learns
    what an agent is doing from ``session/update`` on a session it subscribed to,
    and a turn delivered anywhere else is a round that happened where nobody is
    listening. The prompt in flight, if there is one, is unaffected --
    ``updates.py`` defers a runtime turn's ending rather than letting it answer.

    A job from some other surface (one written before this install served ACP,
    say) names a channel this stack has no outlet for. Nothing is delivered then,
    which is the same outcome as today and better than guessing at a session.
    """
    from raven.spine import ChatType, Origin, Source, TurnRequest

    async def on_wake(job: Any) -> None:
        payload = getattr(job, "payload", None)
        job_channel = (getattr(payload, "channel", None) or channel).strip()
        chat_id = (getattr(payload, "to", None) or "default").strip()
        conversation = f"{job_channel}:{chat_id}"
        req = TurnRequest(
            origin=Origin.CRON,
            source=Source(
                channel=job_channel,
                chat_id=chat_id,
                sender_id="cron",
                chat_type=ChatType.DM,
            ),
            # The wake's own words. A wake turn starts from an empty history, so
            # the message is the whole of what it gets, and wrapping it would hand
            # the loop an instruction written for a different reader.
            text=getattr(payload, "message", "") or "",
            conversation=conversation,
        )
        logger.info("acp: wake '{}' running on {}", getattr(job, "name", "?"), conversation)
        await submit(req).result()

    return on_wake


async def build_rpc_stack(
    send_frame: SendFrame,
    *,
    channel: str = "tui",
    approval_responder: Any = None,
) -> RpcStack:
    """Build dispatcher + engine wired to ``send_frame``.

    Must run inside the event loop that will serve requests: the spine scheduler
    binds to the running loop.

    ``channel`` names the delivery channel this stack's turns run on and it must
    reach *both* collaborators -- ``build_tui`` registers its outlet under this
    name and the turn methods stamp it on every request they submit. Passing it
    to one and not the other drops every reply silently, which ``spine/turn.py``
    states outright.

    ``approval_responder`` replaces the shell-approval transport. The broker built
    here emits ``approval.request`` on this same sink, which is right for a client
    that implements that method and useless for one that does not -- an ACP client
    speaks ``session/request_permission`` instead. Only the transport is replaced;
    classification and scope stay where they are.
    """
    from raven.cli.tui_commands import _build_tui_agent_loop
    from raven.tui_rpc.confirm_broker import ConfirmBroker
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.errors import RpcError
    from raven.tui_rpc.methods import register_aligned_methods_except_system
    from raven.tui_rpc.methods import turn as turn_module
    from raven.tui_rpc.methods.system import system_hello, system_ping, system_version
    from raven.tui_rpc.question_broker import QuestionBroker
    from raven.tui_rpc.approval_broker import ApprovalBroker
    from raven.tui_rpc.spine import build_tui
    from raven.tui_rpc.subscriptions import SubscriptionEmitter

    dispatcher = Dispatcher()
    emitter = SubscriptionEmitter(send_frame=send_frame)
    confirm_broker = ConfirmBroker(send_frame=send_frame)
    question_broker = QuestionBroker(send_frame=send_frame)
    # The caller's responder wins when it has one; otherwise this stack's own
    # broker keeps the existing behaviour for a client that speaks approval.*.
    approval_broker = approval_responder or ApprovalBroker(send_frame=send_frame)

    agent_loop = None
    build_error: RpcError | None = None
    try:
        agent_loop = _build_tui_agent_loop()
    except RpcError as exc:
        build_error = exc
        logger.warning("acp: engine build failed, surfacing on first turn: {}", exc)

    # Late-bound now that the tool registry exists. ops_ask_owner takes one for
    # the reason the TUI path gives: during a campaign it is the only route to the
    # owner, and over ACP that route is a real one -- the client can answer.
    if agent_loop is not None:
        for name in ("ask_user", "ops_ask_owner"):
            tool = agent_loop.tools.get(name)
            if tool is not None and hasattr(tool, "set_broker"):
                tool.set_broker(question_broker)
        agent_loop.set_deep_research_broker(question_broker)

    def _agent_loop_factory():
        if agent_loop is not None:
            return agent_loop
        if build_error is not None:
            raise build_error
        return None

    turn_scheduler = None
    turn_ids: dict[str, str] = {}
    turn_teardown: Callable[[], Awaitable[None]] | None = None
    watcher_task: asyncio.Task | None = None
    if agent_loop is not None:
        turn_scheduler, _hub, turn_ids, turn_teardown = build_tui(
            agent_loop,
            emitter,
            channel=channel,
            on_turn_end=turn_module.clear_active,
            approval_responder=approval_broker,
        )
        # A sub-agent's result comes back as a turn of its own, and it has to run
        # on this spine -- the only one whose hub knows this channel's outlet.
        agent_loop.subagents.set_submit(turn_scheduler.submit)

        if agent_loop.cron_service is not None:
            # The loop's service is partitioned to the TUI's channel, which is
            # right for the socket and refuses every job this stack schedules:
            # "channel 'acp' is outside this runner's partition", logged once and
            # then silence, with the wake sitting in the store unclaimed. The
            # partition exists so two runners on one store do not both fire a job;
            # this process is the only claimant of its own store, and widening it
            # by exactly the channel served here keeps that true.
            allowed = agent_loop.cron_service.allowed_channels
            if allowed is not None:
                agent_loop.cron_service.allowed_channels = set(allowed) | {channel}
            agent_loop.cron_service.on_job = _make_on_wake(turn_scheduler.submit, channel)
            await agent_loop.cron_service.start()
            # An on-call wake is armed with an ETA the loop guessed. The watcher
            # pulls it forward the moment the job it was guessing about reaches a
            # terminal state, which is the difference between noticing in seconds
            # and noticing at the end of a ninety-minute timer.
            from raven.ops.event_watcher import OpsEventWatcher

            watcher_task = asyncio.create_task(OpsEventWatcher(agent_loop.cron_service).run())

    dispatcher.register("system.hello", system_hello)
    dispatcher.register("system.ping", system_ping)
    dispatcher.register("system.version", system_version)
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

    async def teardown() -> None:
        """Stop only what this built, in the order the TUI path stops it."""
        if watcher_task is not None:
            watcher_task.cancel()
        if agent_loop is not None and agent_loop.cron_service is not None:
            with contextlib.suppress(Exception):
                agent_loop.cron_service.stop()
        if turn_teardown is not None:
            try:
                await turn_teardown()
            except Exception:  # noqa: BLE001 -- teardown must not mask the exit
                logger.exception("acp: turn spine teardown failed")

    return RpcStack(
        dispatcher=dispatcher,
        emitter=emitter,
        agent_loop=agent_loop,
        build_error=build_error,
        teardown=teardown,
        question_broker=question_broker,
    )


__all__ = ["RpcStack", "build_rpc_stack"]
