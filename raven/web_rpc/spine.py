"""Spine wiring for the web-app RPC channel.

The web channel streams a turn exactly like the TUI does — token.delta /
thinking.delta / tool.start / tool.complete from the runner, message.complete /
error from the sink after the render barrier. That machinery is already
channel-parameterized in :mod:`raven.tui_rpc.spine`, so ``build_web`` reuses it
with ``channel="web"``; the only genuinely web-specific piece is the WebSocket
transport (:mod:`raven.web_rpc.server`).

spine never imports web_rpc; web_rpc imports spine + tui_rpc.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from raven.spine import Scheduler
from raven.spine.delivery import DeliveryHub
from raven.tui_rpc.spine import TuiOutlet, build_tui
from raven.tui_rpc.subscriptions import SubscriptionEmitter

# The web outlet is byte-for-byte the TUI outlet (same wire events, only the
# channel name differs). Alias it so call sites read as "web" and a future
# web-only divergence has a named seam to grow from.
WebOutlet = TuiOutlet


def build_web(
    agent_loop: Any,
    emitter: SubscriptionEmitter,
    *,
    channel: str = "web",
    on_turn_end: Callable[[str], None] | None = None,
    readback_texts: dict[str, str] | None = None,
    user_pool: int = 4,
    system_pool: int = 2,
) -> tuple[Scheduler, DeliveryHub, dict[str, str], Callable[[], Awaitable[None]]]:
    """Assemble the web channel's spine: a streaming ``Scheduler`` + ``DeliveryHub``
    whose outlet maps spine events to the TUI wire protocol on ``emitter``, keyed
    by the ``web`` channel. Returns ``(scheduler, hub, turn_ids, teardown)`` — the
    same shape as ``build_tui`` / ``build_gateway``.

    This is a distinct spine from the gateway's own (``build_gateway``): the
    gateway runner is non-streaming (proactive replies are one Text), while the
    web channel wants token streaming, so it gets its own streaming runner. Both
    drive the same ``agent_loop`` (concurrency-safe: per-turn tool state is
    turn-local). See ui-webui/docs/plans/2026-07-23-p1-gateway-web-channel.md.
    """
    return build_tui(
        agent_loop,
        emitter,
        channel=channel,
        on_turn_end=on_turn_end,
        readback_texts=readback_texts,
        user_pool=user_pool,
        system_pool=system_pool,
    )
