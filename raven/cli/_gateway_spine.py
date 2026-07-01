"""Spine wiring for the gateway daemon: build_gateway assembles the scheduler,
the delivery hub with a per-channel outbound outlet, and a teardown — the third
assembly point, mirroring build_repl / build_tui. The gateway's host sources
(cron / sentinel / heartbeat, and channel replies) submit through it.

spine never imports cli; cli imports spine.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from raven.agent.spine_runner import AgentTurnRunner
from raven.channels.outlet import ChannelOutletAdapter
from raven.spine import OriginPools, Scheduler
from raven.spine.delivery import DeliveryHub
from raven.spine.events import Text, TurnEnded, TurnFailed, TurnStarted
from raven.spine.message import Source
from raven.spine.turn import Origin

if TYPE_CHECKING:
    from raven.agent.loop import AgentLoop
    from raven.channels.contract import Channel
    from raven.spine.events import TurnEvent
    from raven.spine.runner import Drain, Emit, TurnOutcome
    from raven.spine.turn import TurnRequest

_TURN_FAILED_REPLY = "Sorry, I encountered an error."


def _cid(req: TurnRequest) -> str:
    return req.conversation or f"{req.source.channel}:{req.source.chat_id}"


# Origins whose submitter reads the turn's reply back to feed its own side effect
# — only cron, which fills a system event from its reply. A delivery-only origin
# (a channel user reply, or heartbeat: its reply rides emit -> hub -> outlet and
# nothing reads it back) is not stored — storing it would only leak (no one pops
# it). The store is gated on this set.
_READBACK_ORIGINS = frozenset({Origin.CRON})


class GatewayTurnRunner(AgentTurnRunner):
    """The gateway's runner: the non-streaming agent loop, plus a per-conversation
    capture of the reply text for read-back origins (cron). The gateway hosts a
    mix of turns (cron / heartbeat / channel users) on one runner, so — unlike the
    TUI runner, which pops every turn because its turns are homogeneous — it stores
    only the read-back origins' text, keyed by conversation; the submitter pops it
    after ``result()``. A delivery-only turn (a channel user or heartbeat reply) is
    never stored, so the long-running daemon does not accumulate."""

    def __init__(
        self,
        agent_loop: AgentLoop,
        readback_texts: dict[str, str],
        sources: dict[str, Source],
    ) -> None:
        super().__init__(agent_loop, stream=False)
        self._readback_texts = readback_texts
        self._sources = sources

    async def run(self, req: TurnRequest, emit: Emit, drain: Drain) -> TurnOutcome:
        # Stash the turn's reply address so the sink can route a TurnFailed error
        # reply back to the originating channel (the lifecycle event carries only
        # conversation_id). Keyed by the lane's conversation id; the sink pops it
        # on TurnEnded/TurnFailed so the daemon does not accumulate.
        self._sources[_cid(req)] = req.source
        if req.origin not in _READBACK_ORIGINS:
            return await self._loop.run_turn(req, emit, drain, stream=False)
        text_sink: dict[str, str] = {}
        outcome = await self._loop.run_turn(req, emit, drain, stream=False, text_sink=text_sink)
        # Stored before returning: the worker resolves result() only after run()
        # returns, so the submitter's read is ordered after this write.
        if req.conversation is not None and (text := text_sink.get("text")) is not None:
            self._readback_texts[req.conversation] = text
        return outcome


def _make_gateway_sink(
    hub: DeliveryHub,
    agent_loop: AgentLoop,
    sources: dict[str, Source],
) -> Callable[[TurnEvent], Awaitable[None]]:
    """Adapt the hub into the gateway's EventSink, restoring the two lifecycle
    side effects the bus drainer's ``_dispatch`` had (which the plain hub sink
    drops): on every turn end fire ``on_turn_complete`` (the WakeScheduler's
    parked-wake signal), and on a non-cancelled failure deliver a user-visible
    error reply to the originating channel. A cancelled turn (/stop) fires the
    wake but sends no reply — mirroring the bus path (CancelledError re-raises
    without the "Sorry" message) and build_tui's cancelled-gated emit_error.

    notify fires on every origin (cron / sentinel / heartbeat / channel), a
    superset of the bus drainer's user-turn-only _dispatch — benign and slightly
    more correct: wake.on_turn_complete is a no-op unless a wake is parked, so the
    extra fires just un-park any turn (the bus path could strand a wake parked
    during a proactive turn)."""

    async def sink(event: TurnEvent) -> None:
        if isinstance(event, TurnStarted):
            return
        if isinstance(event, (TurnEnded, TurnFailed)):
            source = sources.pop(event.conversation_id, None)
            if isinstance(event, TurnFailed) and not event.cancelled and source is not None:
                await hub.dispatch(Text(content=_TURN_FAILED_REPLY, source=source))
            agent_loop._notify_turn_complete()
            return
        await hub.dispatch(event)

    return sink


def build_gateway(
    agent_loop: AgentLoop,
    channels: Mapping[str, Channel],
    *,
    user_pool: int = 4,
    system_pool: int = 2,
    send_max_retries: int = 3,
) -> tuple[Scheduler, DeliveryHub, dict[str, str], dict[str, Source], Callable[[], Awaitable[None]]]:
    """Wire the gateway's spine pieces: a hub with a ChannelOutletAdapter per
    channel (so a reply reaches its target channel), and a Scheduler whose runner
    is the agent loop's non-streaming run_turn (proactive replies are one Text,
    not a token stream — canon Q2-D). Returns (scheduler, hub, readback_texts,
    sources, teardown); teardown stops the scheduler then closes the hub's outlet
    workers. ``sources`` maps a live turn's conversation id to its real inbound
    Source — the ask_user question outbound reuses it to reach the exact (topic-
    correct) chat rather than reconstructing an address from the conversation id.

    ``readback_texts`` maps a read-back origin's conversation to its reply text:
    cron reads its own turn's reply back (to fill a system event) through it — a
    submitter pops its conversation after result().

    Register every channel the gateway may deliver to: a reply whose source
    channel has no registered outlet is dropped by the hub (a warning, not an
    error)."""
    hub = DeliveryHub(send_max_retries=send_max_retries)
    for channel in channels.values():
        hub.register(ChannelOutletAdapter(channel))
    readback_texts: dict[str, str] = {}
    sources: dict[str, Source] = {}
    # user>1 is safe now that per-turn tool state (message routing, context) is
    # turn-local: concurrent user turns no longer clobber each other's reply
    # target. system>1 lets a cron and a heartbeat/sentinel turn overlap.
    scheduler = Scheduler(
        GatewayTurnRunner(agent_loop, readback_texts, sources),
        OriginPools(user=user_pool, system=system_pool),
        _make_gateway_sink(hub, agent_loop, sources),
    )

    async def teardown() -> None:
        await scheduler.shutdown(grace=0.0)
        await hub.aclose()

    return scheduler, hub, readback_texts, sources, teardown
