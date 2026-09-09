"""Assemble a dispatcher + engine for a caller that owns its own transport.

Written for ``raven acp``, which speaks a protocol on stdio rather than serving
the TUI socket: it needs the same stack ``cli/tui_commands.py`` builds around
``RpcServer``, minus the socket and the handshake latch. The two are kept apart
rather than factored together because the TUI path also owns process lifecycle
(the child, the handshake deadline, first-render sequencing) that has no meaning
here -- see ``raven.acp.server.build_stack``, the only caller.

Only the five methods the ACP layer dispatches have to work: ``turn.send``,
``turn.subscribe``, ``turn.unsubscribe``, ``turn.cancel`` and ``session.resume``.
The umbrella is registered whole anyway, for the reason the TUI path states -- a
handler added to ``tui_rpc/methods/__init__.py`` later should not need a second
registration site here to be reachable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

SendFrame = Callable[[dict], Any]


@dataclass
class RpcStack:
    """What a caller needs off the assembled stack, and nothing more.

    Mirrors mainline's shape so ``raven/acp/**`` needs no edit here: it reads
    ``dispatcher``, ``agent_loop``, ``question_broker`` and ``teardown``.
    """

    dispatcher: Any
    emitter: Any
    agent_loop: Any
    build_error: Any
    teardown: Callable[[], Awaitable[None]]
    direct_targets: dict[str, dict[str, str]] = field(default_factory=dict)
    question_broker: Any = None


async def build_rpc_stack(
    send_frame: SendFrame,
    *,
    agent_loop: Any = None,
    channel: str = "acp",
    approval_responder: Any = None,
) -> RpcStack:
    """Build the stack with ``send_frame`` as its outbound notification sink.

    Must run inside the event loop that will serve requests: the spine's
    scheduler binds to the running loop.
    """
    from types import SimpleNamespace

    from raven.cli.tui_commands import _build_tui_agent_loop
    from raven.tui_rpc.confirm_broker import ConfirmBroker
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.errors import RpcError
    from raven.tui_rpc.methods import register_aligned_methods_except_system
    from raven.tui_rpc.methods import turn as turn_module
    from raven.tui_rpc.methods.system import system_hello, system_ping, system_version
    from raven.tui_rpc.question_broker import QuestionBroker
    from raven.tui_rpc.spine import build_tui
    from raven.tui_rpc.subscriptions import SubscriptionEmitter

    dispatcher = Dispatcher()
    emitter = SubscriptionEmitter(send_frame=send_frame)
    confirm_broker = ConfirmBroker(send_frame=send_frame)
    question_broker = QuestionBroker(send_frame=send_frame)

    build_error: RpcError | None = None
    if agent_loop is None:
        try:
            agent_loop = _build_tui_agent_loop()
        except RpcError as exc:
            build_error = exc

    # Late-bound for the reason the TUI path gives: the broker exists before the
    # loop does, and the tools that ask mid-turn are built with the loop.
    if agent_loop is not None:
        if (ask_tool := agent_loop.tools.get("ask_user")) is not None and hasattr(ask_tool, "set_broker"):
            ask_tool.set_broker(question_broker)
        agent_loop.set_deep_research_broker(question_broker)
        if approval_responder is not None and hasattr(agent_loop, "set_approval_responder"):
            agent_loop.set_approval_responder(approval_responder)

    turn_scheduler = None
    turn_ids: dict[str, str] = {}
    turn_teardown = None
    if agent_loop is not None:
        # One AgentLoop owns mutable tool and turn state. Keep its user lane
        # serial until ACP constructs one loop per session; process-level
        # concurrency belongs to the caller in the meantime.
        turn_scheduler, _turn_hub, turn_ids, turn_teardown = build_tui(
            agent_loop,
            emitter,
            on_turn_end=turn_module.clear_active,
        )
        # A sub-agent's result turn is submitted back onto this scheduler; without
        # it a delegated reply is produced and never delivered.
        agent_loop.subagents.set_submit(turn_scheduler.submit)

    # Cron is deliberately NOT started. This process lives as long as one
    # editor's connection: a reminder that fires here would deliver into a
    # channel nobody is reading, and starting the service would also make this
    # process fight the host raven for the same cron store.
    dispatcher.register("system.hello", system_hello)
    dispatcher.register("system.ping", system_ping)
    dispatcher.register("system.version", system_version)
    register_aligned_methods_except_system(
        dispatcher,
        emitter=emitter,
        agent_loop_factory=lambda: _factory(agent_loop, build_error),
        confirm_broker=confirm_broker,
        question_broker=question_broker,
        scheduler=turn_scheduler,
        turn_ids=turn_ids,
        build_error=build_error,
    )

    async def teardown() -> None:
        """Stop what this built, in the order that makes each stop reachable."""
        if turn_teardown is not None:
            try:
                res = turn_teardown()
                if hasattr(res, "__await__"):
                    await res
            except Exception as exc:  # noqa: BLE001 - teardown must not raise past here
                logger.warning("acp: turn spine teardown failed: {}", exc)
        for name, closer in (("confirm", confirm_broker), ("question", question_broker)):
            cancel = getattr(closer, "cancel_all", None)
            if cancel is None:
                continue
            try:
                res = cancel()
                if hasattr(res, "__await__"):
                    await res
            except Exception as exc:  # noqa: BLE001
                logger.warning("acp: {} broker teardown failed: {}", name, exc)
        if agent_loop is not None:
            for attr in ("aclose", "close", "shutdown"):
                fn = getattr(agent_loop, attr, None)
                if fn is None:
                    continue
                try:
                    res = fn()
                    if hasattr(res, "__await__"):
                        await res
                except Exception as exc:  # noqa: BLE001
                    logger.warning("acp: agent loop {} failed: {}", attr, exc)
                break

    _ = SimpleNamespace  # imported for parity with the TUI path's cron wiring
    return RpcStack(
        dispatcher=dispatcher,
        emitter=emitter,
        agent_loop=agent_loop,
        build_error=build_error,
        teardown=teardown,
        question_broker=question_broker,
    )


def _factory(agent_loop: Any, build_error: Any) -> Any:
    if agent_loop is not None:
        return agent_loop
    if build_error is not None:
        raise build_error
    return None


__all__ = ["RpcStack", "build_rpc_stack"]
