"""Spine wiring for the web-app RPC channel.

The web channel streams a turn exactly like the TUI does — token.delta /
thinking.delta / tool.start / tool.complete from the runner, message.complete /
error from the sink after the render barrier. That machinery is already
channel-parameterized in :mod:`raven.rpc.spine`, so ``build_web`` reuses it
with ``channel="web"``; the only genuinely web-specific piece is the WebSocket
transport (:mod:`raven.web_rpc.server`).

spine never imports web_rpc; web_rpc imports spine + rpc.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from raven.rpc.spine import RpcOutlet, build_rpc_spine
from raven.rpc.subscriptions import SubscriptionEmitter
from raven.spine import Scheduler
from raven.spine.delivery import DeliveryHub

# The web outlet is byte-for-byte the TUI outlet (same wire events, only the
# channel name differs). Alias it so call sites read as "web" and a future
# web-only divergence has a named seam to grow from.
WebOutlet = RpcOutlet


def build_web(
    agent_loop: Any,
    emitter: SubscriptionEmitter,
    *,
    channel: str = "web",
    on_turn_end: Callable[[str], None] | None = None,
    readback_texts: dict[str, str] | None = None,
    direct_targets: dict[str, dict[str, str]] | None = None,
    user_pool: int = 4,
    system_pool: int = 2,
) -> tuple[Scheduler, DeliveryHub, dict[str, str], Callable[[], Awaitable[None]]]:
    """Assemble the web channel's spine: a streaming ``Scheduler`` + ``DeliveryHub``
    whose outlet maps spine events to the TUI wire protocol on ``emitter``, keyed
    by the ``web`` channel. Returns ``(scheduler, hub, turn_ids, teardown)`` — the
    same shape as ``build_rpc_spine`` / ``build_gateway``.

    This is a distinct spine from the gateway's own (``build_gateway``): the
    gateway runner is non-streaming (proactive replies are one Text), while the
    web channel wants token streaming, so it gets its own streaming runner. Both
    drive the same ``agent_loop`` (concurrency-safe: per-turn tool state is
    turn-local).

    ``direct_targets`` has to be the *same object* the caller hands
    ``register_turn_methods``: ``turn.send`` writes the addressee into it and the
    outlet reads it back to tag that lane's events. Two separate dicts is not a
    degraded version of one -- it is silence, since nothing would ever be tagged
    and no client could tell a sub-agent's reply from the main agent's.
    """
    return build_rpc_spine(
        agent_loop,
        emitter,
        channel=channel,
        on_turn_end=on_turn_end,
        readback_texts=readback_texts,
        direct_targets=direct_targets,
        user_pool=user_pool,
        system_pool=system_pool,
    )
