"""Reusable RPC stack assembly for headless transports (`raven serve`).

Extracted shape of the wiring in ``tui_commands._run_rpc_server_until_done``:
AgentLoop + brokers + SubscriptionEmitter + spine scheduler + method
registration, minus any transport. The caller supplies a ``send_frame``
sink (the TUI hands it a single socket writer; ``raven serve`` hands it a
WebSocket broadcast), so the same engine assembly serves both transports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from raven.rpc import LOCAL_CHANNEL

SendFrame = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class RpcStack:
    dispatcher: Any
    emitter: Any
    agent_loop: Any
    build_error: Any
    teardown: Callable[[], Awaitable[None]]
    # The direct-chat map turn.send writes into and the outlet reads back. A
    # host that mounts this stack beside its own spines needs it to build a
    # second outlet over the same emitter (see raven/cli/_gateway_page.py).
    direct_targets: dict[str, dict[str, str]] = field(default_factory=dict)
    # The page's ask_user / deep-research broker. A host with a question
    # surface of its own (the gateway's IM channels) needs the handle to build
    # a per-conversation routing shim over both (see RoutingQuestionBroker).
    question_broker: Any = None
    deliverables: Any = None


async def build_rpc_stack(
    send_frame: SendFrame,
    *,
    agent_loop: Any = None,
    channel: str = "tui",
    approval_responder: Any = None,
) -> RpcStack:
    """Assemble dispatcher + engine wired to ``send_frame``.

    Must run inside the event loop that will serve requests (cron start and
    the spine scheduler bind to the running loop).

    ``agent_loop`` mounts this stack over an engine somebody else owns (the
    gateway hosting the page in its own process) instead of building one here.
    The host keeps every process-lifecycle responsibility: it started cron and
    the memory backend and will stop them, it owns ``subagents.set_submit``
    (a subagent's result turn must run on the host spine, whose hubs can route
    any channel's delivery -- this stack's hub only knows the page's), and this
    stack's teardown then stops only what it built (its brokers and its turn
    spine). The page-facing sinks and brokers are applied either way; a host
    with a question surface of its own then re-binds ask_user / deep-research
    through a per-conversation routing shim over this stack's broker (exposed
    as ``RpcStack.question_broker``) and its own, so the page answers its own
    sessions' questions without swallowing the host's -- see
    ``RoutingQuestionBroker`` and the gateway's page mount.

    ``channel`` names the delivery channel this stack's turns run on, and it must
    reach *both* collaborators or nothing is delivered at all: ``build_rpc_spine``
    registers its outlet under this name, and ``register_turn_methods`` stamps it
    on every turn it submits as ``source.channel``. ``spine/turn.py`` states the
    consequence outright -- the default channel MUST match the channel the outlet
    was registered under, or the reply is dropped -- so passing it to one and not
    the other loses every turn silently. Defaulted to ``"tui"``, which is what
    both sides already used, so existing callers are unchanged.

    ``approval_responder`` replaces the shell-approval transport for this stack's
    user turns. The broker built here emits ``approval.request`` on the same
    ``send_frame``, which is right for a client that implements that method and
    useless for one that does not -- an ACP client speaks
    ``session/request_permission`` instead. Only the transport is replaced:
    classification, the one-command scope and the absence of any always-allow
    state all stay where they are. ``None`` keeps this stack's own broker, so
    existing callers are unchanged.
    """
    from raven.cli.tui_commands import (
        _build_agent_loop,
        _build_cron_callback_spine,
        _fanout_cron_missed,
    )
    from raven.rpc.approval_broker import ApprovalBroker
    from raven.rpc.confirm_broker import ConfirmBroker
    from raven.rpc.connection import conversation_scoped
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.errors import RpcError
    from raven.rpc.methods import (
        register_aligned_methods_except_system,
    )
    from raven.rpc.methods import (
        turn as turn_module,
    )
    from raven.rpc.methods.system import register_system_methods
    from raven.rpc.question_broker import QuestionBroker
    from raven.rpc.spine import build_rpc_spine, make_dag_progress_sink
    from raven.rpc.subscriptions import SubscriptionEmitter

    dispatcher = Dispatcher()
    emitter = SubscriptionEmitter(send_frame=send_frame)
    # ``confirm.request`` now names the conversation the dispatch was asked for
    # (slash.exec threads its session_id down; see cli_dispatch), so the same
    # scoping as the question broker applies: a destructive command's yes/no
    # sheet reaches the surface that conversation speaks through instead of every
    # terminal attached here. A dispatch that names no conversation -- a bare
    # cli.dispatch -- still broadcasts, which is what it did before.
    confirm_broker = ConfirmBroker(send_frame=conversation_scoped(send_frame))
    # approval.request / approval.closed both carry conversation_id, so the same
    # scoping as the question broker below keeps a protected-command overlay on
    # the surface that sent the turn instead of every attached terminal.
    approval_broker = ApprovalBroker(send_frame=conversation_scoped(send_frame))
    # A question is not a stream: ``clarify.request`` carries no subscription_id
    # for a client to filter on, so a broadcast one opens the sheet on every
    # surface attached to this transport. Scoped to the connection that sent the
    # turn instead, with the broadcast kept as the fallback for a conversation
    # no connection owns (a cron or IM turn) -- see connection.conversation_scoped.
    question_broker = QuestionBroker(send_frame=conversation_scoped(send_frame))

    owns_loop = agent_loop is None
    build_error: RpcError | None = None
    if owns_loop:
        try:
            agent_loop = _build_agent_loop()
        except RpcError as e:
            build_error = e

    if agent_loop is not None:
        if (ask_tool := agent_loop.tools.get("ask_user")) is not None and hasattr(ask_tool, "set_broker"):
            ask_tool.set_broker(question_broker)
        agent_loop.set_deep_research_broker(question_broker)
        # The TUI path wires this too. Without it a DAG run streams nothing
        # while it works, which reads as a hang rather than as progress.
        agent_loop.set_dag_progress_sink(make_dag_progress_sink(emitter))
        # The seam a delegated result re-enters its conversation at. Same
        # emitter, same routing; without it the announce's reply arrives as an
        # assistant turn nobody visibly asked.
        agent_loop.subagents.set_delivery_sink(emitter.emit)

        # Backfill the acp capability snapshots at startup: an acp row's
        # statefulness comes from its snapshot, and before this the only writers
        # were the UI Test button and a probe=true listing -- so a fresh install
        # reported every acp agent stateless and the instance picker hid them.
        # Skipped on the acp channel, where this stack is itself a subagent
        # child: verifying there would cascade (each child verifying its own
        # row launching another child), and the host's own stack does it.
        if channel != "acp":
            from raven.agent.subagent.probe import schedule_snapshot_verification

            schedule_snapshot_verification(agent_loop.subagents)

        # Per-server MCP events, broadcast rather than conversation-scoped: a
        # server connecting is not part of anybody's turn. Clients already listen
        # for these three, and an OAuth connect is not completable without them --
        # the authorization URL would only ever reach the gateway host's own
        # browser, which for `raven serve` is not where the user is.
        async def _mcp_event(method: str, params: dict) -> None:
            await send_frame({"jsonrpc": "2.0", "method": method, "params": params})

        agent_loop.set_mcp_event_sink(_mcp_event)

    def _agent_loop_factory():
        if agent_loop is not None:
            return agent_loop
        if build_error is not None:
            raise build_error
        return None

    turn_scheduler = None
    turn_ids: dict[str, str] = {}
    # Owned here, not by the spine, because two collaborators need the same map:
    # turn.send binds a turn's addressee into it and the spine's outlet/sink read
    # it back to tag that turn's events (see build_rpc_spine).
    direct_targets: dict[str, dict[str, str]] = {}
    turn_teardown = None
    if agent_loop is not None:
        from raven.cli._cron_handler import make_on_cron_job

        cron_readback: dict[str, str] = {}
        turn_scheduler, _turn_hub, turn_ids, turn_teardown = build_rpc_spine(
            agent_loop,
            emitter,
            channel=channel,
            on_turn_end=turn_module.clear_active,
            direct_targets=direct_targets,
            readback_texts=cron_readback,
            # The caller's transport when it brought one. The locally built
            # broker is still constructed either way: ``approval.respond`` is
            # registered from it below, and a caller that overrides the responder
            # is not necessarily removing that method.
            approval_responder=approval_responder or approval_broker,
        )
        if owns_loop:
            agent_loop.subagents.set_submit(turn_scheduler.submit)
        if owns_loop and agent_loop.cron_service is not None:
            base_on_cron = make_on_cron_job(
                submit=turn_scheduler.submit,
                readback_texts=cron_readback,
                default_channel=LOCAL_CHANNEL,
                cron_service=agent_loop.cron_service,
            )
            agent_loop.cron_service.on_job = _build_cron_callback_spine(
                base_on_cron, emitter, direct_targets=direct_targets
            )
            await agent_loop.cron_service.start()
            # start() dropped past-due one-shot reminders on this runner's
            # partition. The served page reaches the runtime through here rather
            # than through tui_commands, so without this the drops are collected
            # and never told to anyone.
            if agent_loop.cron_service.last_startup_drops:
                await _fanout_cron_missed(emitter, drops=agent_loop.cron_service.last_startup_drops)

    # The sink is what makes an explicit version check visible to tabs other than
    # the one that asked: two windows on one gateway, one settings button, and
    # both banners update.
    register_system_methods(dispatcher, send_frame=send_frame)
    register_aligned_methods_except_system(
        dispatcher,
        emitter=emitter,
        agent_loop_factory=_agent_loop_factory,
        approval_broker=approval_broker,
        confirm_broker=confirm_broker,
        question_broker=question_broker,
        scheduler=turn_scheduler,
        turn_ids=turn_ids,
        direct_targets=direct_targets,
        build_error=build_error,
        send_frame=send_frame,
        default_channel=channel,
    )

    if owns_loop and agent_loop is not None and agent_loop.backend is not None:

        async def _start_backend() -> None:
            try:
                await agent_loop.backend.start()
            except Exception:
                logger.exception("serve: memory backend start failed; continuing with degraded memory path")

        asyncio.create_task(_start_backend())

    async def teardown() -> None:
        confirm_broker.cancel_all()
        approval_broker.cancel_all()
        if owns_loop and agent_loop is not None and agent_loop.cron_service is not None:
            try:
                agent_loop.cron_service.stop()
            except Exception:
                pass
        if turn_teardown is not None:
            try:
                await turn_teardown()
            except Exception:
                pass
        # A mounted stack stops here: the engine, its backend, the browser and
        # the ACP pool are the host process's to close, not this stack's.
        if not owns_loop:
            return
        if agent_loop is not None and agent_loop.backend is not None:
            try:
                # Drain before stop, the same order the CLI hosts follow:
                # stopping the backend closes the HTTP client the queued
                # writes still need, and this stack owns turns too.
                await agent_loop.drain_backend_stores()
                await agent_loop.backend.stop()
            except Exception:
                logger.exception("serve: memory backend stop failed; continuing shutdown")
        # Same order every host follows: drain, cancel, then close. Cancelling
        # after the pool closed would report each in-flight turn as a connection
        # failure instead of as the stop it is.
        try:
            from raven.agent.acp.client import begin_drain

            begin_drain()
            if agent_loop is not None:
                await agent_loop.subagents.cancel_all()
        except Exception:
            logger.exception("serve: cancelling in-flight sub-agents failed; continuing shutdown")
        # Chromium is a child process too, and a persistent-profile one: leaving
        # it running holds the profile lock the next launch needs.
        try:
            from raven.browser import get_browser

            await get_browser().close()
        except Exception:
            logger.exception("serve: browser close failed; continuing shutdown")
        # ACP agents are launched with start_new_session, so they do not get the
        # terminal's signals and outlive this process unless the pool is closed.
        try:
            from raven.agent.acp.pool import close_pool

            await close_pool()
        except Exception:
            logger.exception("serve: acp pool close failed; continuing shutdown")

    return RpcStack(
        dispatcher=dispatcher,
        emitter=emitter,
        agent_loop=agent_loop,
        build_error=build_error,
        teardown=teardown,
        direct_targets=direct_targets,
        question_broker=question_broker,
        deliverables=getattr(agent_loop, "_deliverables", None),
    )


__all__ = ["RpcStack", "SendFrame", "build_rpc_stack"]
