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
from dataclasses import dataclass
from typing import Any

from loguru import logger

SendFrame = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class RpcStack:
    dispatcher: Any
    emitter: Any
    agent_loop: Any
    build_error: Any
    teardown: Callable[[], Awaitable[None]]


async def build_rpc_stack(send_frame: SendFrame) -> RpcStack:
    """Assemble dispatcher + engine wired to ``send_frame``.

    Must run inside the event loop that will serve requests (cron start and
    the spine scheduler bind to the running loop).
    """
    from raven.cli.tui_commands import (
        _build_cron_callback_spine,
        _build_tui_agent_loop,
    )
    from raven.tui_rpc.approval_broker import ApprovalBroker
    from raven.tui_rpc.confirm_broker import ConfirmBroker
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.errors import RpcError
    from raven.tui_rpc.methods import (
        register_aligned_methods_except_system,
    )
    from raven.tui_rpc.methods import (
        turn as turn_module,
    )
    from raven.tui_rpc.methods.system import (
        system_hello,
        system_ping,
        system_upgrade,
        system_version,
    )
    from raven.tui_rpc.question_broker import QuestionBroker
    from raven.tui_rpc.spine import build_tui, make_dag_progress_sink
    from raven.tui_rpc.subscriptions import SubscriptionEmitter

    dispatcher = Dispatcher()
    emitter = SubscriptionEmitter(send_frame=send_frame)
    confirm_broker = ConfirmBroker(send_frame=send_frame)
    approval_broker = ApprovalBroker(send_frame=send_frame)
    question_broker = QuestionBroker(send_frame=send_frame)

    agent_loop = None
    build_error: RpcError | None = None
    try:
        agent_loop = _build_tui_agent_loop()
    except RpcError as e:
        build_error = e

    if agent_loop is not None:
        if (ask_tool := agent_loop.tools.get("ask_user")) is not None and hasattr(ask_tool, "set_broker"):
            ask_tool.set_broker(question_broker)
        agent_loop.set_deep_research_broker(question_broker)
        # The TUI path wires this too. Without it a DAG run streams nothing
        # while it works, which reads as a hang rather than as progress.
        agent_loop.set_dag_progress_sink(make_dag_progress_sink(emitter))

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
    turn_teardown = None
    if agent_loop is not None:
        from types import SimpleNamespace

        from raven.cli._cron_handler import make_on_cron_job

        cron_readback: dict[str, str] = {}
        turn_scheduler, turn_hub, turn_ids, turn_teardown = build_tui(
            agent_loop,
            emitter,
            on_turn_end=turn_module.clear_active,
            readback_texts=cron_readback,
            approval_responder=approval_broker,
        )
        agent_loop.subagents.set_submit(turn_scheduler.submit)
        if agent_loop.cron_service is not None:
            base_on_cron = make_on_cron_job(
                agent_loop,
                turn_hub,
                submit=turn_scheduler.submit,
                readback_texts=cron_readback,
                channel_manager=SimpleNamespace(enabled_channels=["tui"]),
                default_channel="tui",
            )
            agent_loop.cron_service.on_job = _build_cron_callback_spine(base_on_cron, emitter)
            await agent_loop.cron_service.start()

    dispatcher.register("system.hello", system_hello)
    dispatcher.register("system.ping", system_ping)
    dispatcher.register("system.version", system_version)
    dispatcher.register("system.upgrade", system_upgrade)
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
    )

    if agent_loop is not None and agent_loop.backend is not None:

        async def _start_backend() -> None:
            try:
                await agent_loop.backend.start()
            except Exception:
                logger.exception("serve: memory backend start failed; continuing with degraded memory path")

        asyncio.create_task(_start_backend())

    async def teardown() -> None:
        confirm_broker.cancel_all()
        approval_broker.cancel_all()
        if agent_loop is not None and agent_loop.cron_service is not None:
            try:
                agent_loop.cron_service.stop()
            except Exception:
                pass
        if turn_teardown is not None:
            try:
                await turn_teardown()
            except Exception:
                pass
        if agent_loop is not None and agent_loop.backend is not None:
            try:
                await agent_loop.backend.stop()
            except Exception:
                logger.exception("serve: memory backend stop failed; continuing shutdown")

    return RpcStack(
        dispatcher=dispatcher,
        emitter=emitter,
        agent_loop=agent_loop,
        build_error=build_error,
        teardown=teardown,
    )


__all__ = ["RpcStack", "SendFrame", "build_rpc_stack"]
