"""Spine wiring for the gateway daemon: build_gateway assembles the scheduler,
the delivery hub with a per-channel outbound outlet, and a teardown -- the third
assembly point, mirroring build_one_shot_spine (cli) and build_rpc_spine (rpc) at their own
surfaces. It lives with the rest of the gateway plumbing because it wires exactly
one entrance; the cross-entrance assembly root (raven/core) holds only what every
entrance shares. The gateway's host sources (cron / sentinel / heartbeat, and
channel replies) submit through it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from raven.agent.spine_runner import AgentTurnRunner
from raven.agent.tools.ask_user import AskUserTool
from raven.gateway.outlet import ChannelOutletAdapter
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
_TURN_CUT_BY_RELOAD_REPLY = "A runtime reload cut this reply short; please send your message again."


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
        cid = _cid(req)
        self._sources[cid] = req.source
        # The same per-turn rebinding and origin gate RpcTurnRunner applies: a
        # channel user can answer (gateway_commands wires a QuestionBroker onto
        # this tool, which renders clarify.request as an outbound message and
        # routes the reply back), while a CRON or otherwise background turn has
        # no reader and must decline rather than wait on nobody. Without this the
        # gateway's sub-agents saw no asker and no autofill at all.
        # Function-level on purpose: the acp client family is future shelf
        # cargo and must not be named at this module's import time
        # (binding-time debt).
        from raven.agent.acp_client.asker import AskViaTool, start_ask_turn
        from raven.agent.acp_client.resolver import Autofill

        tools = getattr(self._loop, "tools", None)
        ask_tool = tools.get("ask_user") if tools is not None else None
        interactive = req.origin is Origin.USER and isinstance(ask_tool, AskUserTool)
        start_ask_turn(
            AskViaTool(ask_tool) if interactive else None,
            Autofill(
                self._loop,
                emit=emit,
                conversation_id=cid,
                config=self._loop.subagent_questions_config,
            )
            if interactive
            else None,
            conversation_id=cid,
        )
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
    cut_by_reload: Callable[[], bool] | None = None,
) -> Callable[[TurnEvent], Awaitable[None]]:
    """Adapt the delivery hub into the gateway's EventSink, with the two
    lifecycle side effects the plain hub sink does not carry: on TurnEnded or
    TurnFailed fire ``on_turn_complete`` (the WakeScheduler's parked-wake
    signal), and on a non-cancelled failure deliver a user-visible error reply
    to the originating channel. A cancelled turn (/stop) fires the wake and
    sends no reply -- unless ``cut_by_reload`` says a generation swap did the
    cancelling, in which case the channel is told the reply was cut.

    notify fires on every origin (cron / sentinel / heartbeat / channel) rather
    than on user turns only -- benign and slightly more correct:
    wake.on_turn_complete is a no-op unless a wake is parked, so the extra fires
    just un-park any turn, and a wake parked during a proactive turn is never
    stranded."""

    async def sink(event: TurnEvent) -> None:
        if isinstance(event, TurnStarted):
            return
        if isinstance(event, (TurnEnded, TurnFailed)):
            source = sources.pop(event.conversation_id, None)
            if isinstance(event, TurnFailed) and source is not None:
                if not event.cancelled:
                    await hub.dispatch(Text(content=_TURN_FAILED_REPLY, source=source))
                elif cut_by_reload is not None and cut_by_reload():
                    # /stop is the user's own act and stays silent; a reload is
                    # not, so the reply it cut is owed at least a sentence.
                    await hub.dispatch(Text(content=_TURN_CUT_BY_RELOAD_REPLY, source=source))
            agent_loop.notify_turn_complete()
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
    shutdown_grace: float = 0.0,
    cut_by_reload: Callable[[], bool] | None = None,
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
        _make_gateway_sink(hub, agent_loop, sources, cut_by_reload),
    )

    async def teardown() -> None:
        await scheduler.shutdown(grace=shutdown_grace)
        await hub.aclose()

    return scheduler, hub, readback_texts, sources, teardown
