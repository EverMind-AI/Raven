"""Spine wiring for the gateway daemon: build_gateway assembles the scheduler,
the delivery hub with a per-channel outbound outlet, and a teardown -- the third
assembly point, mirroring build_one_shot_spine (cli) and build_rpc_spine (rpc) at their own
surfaces. It lives with the rest of the gateway plumbing because it wires exactly
one entrance; the cross-entrance assembly root (raven/core) holds only what every
entrance shares. The gateway's host sources (cron / sentinel / heartbeat, and
channel replies) submit through it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from loguru import logger

from raven.agent.spine_runner import AgentTurnRunner
from raven.contracts.asking import SupportsDirectAsk
from raven.gateway.outlet import ChannelOutletAdapter
from raven.providers.base import bound_llm_detail, llm_error_summary, parse_llm_error
from raven.spine import OriginPools, Scheduler, session_key_for
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


def _failed_turn_reply(error: str, *, reported: bool) -> str:
    """What a channel reader is told about a failed turn.

    A model call's failure is told as its category and endpoint alone. The
    vendor's own account goes to the operator's log, not to a group chat: an
    auth body carries a masked key and an account URL, and a rejected-request
    body can quote the prompt back. ``a2a`` already answers a remote caller at
    this altitude (``TURN_FAILED_MESSAGE``).

    A runner that worded the failure itself -- ``reported`` -- is quoted,
    bounded, because that text is the runtime's own report and was written for
    a reader. Anything else is a crash whose message names hosts and paths, and
    gets the one sentence channels have always got.
    """
    parsed = parse_llm_error(error)
    if parsed is not None:
        category, provider, _detail = parsed
        return llm_error_summary(category, provider)
    if reported and error.strip():
        return bound_llm_detail(error)
    return _TURN_FAILED_REPLY


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
    never stored, so the long-running daemon does not accumulate.

    ``merged`` records, by turn id, where each person's message the turn took in
    while it ran came from; the sink pops it when the turn ends."""

    def __init__(
        self,
        agent_loop: AgentLoop,
        readback_texts: dict[str, str],
        sources: dict[str, Source],
        merged: dict[str, list[Source]] | None = None,
    ) -> None:
        super().__init__(agent_loop, stream=False)
        self._readback_texts = readback_texts
        self._sources = sources
        self._merged = {} if merged is None else merged

    def _noting_merges(self, req: TurnRequest, drain: Drain) -> Drain:
        def noted() -> list[TurnRequest]:
            taken = drain()
            people = [merged.source for merged in taken if merged.origin is Origin.USER]
            if people:
                self._merged.setdefault(req.turn_id, []).extend(people)
            return taken

        return noted

    async def run(self, req: TurnRequest, emit: Emit, drain: Drain) -> TurnOutcome:
        # Stash the turn's reply address so the sink can route a TurnFailed error
        # reply back to the originating channel (the lifecycle event carries only
        # conversation_id). Keyed by the lane's conversation id; the sink pops it
        # on TurnEnded/TurnFailed so the daemon does not accumulate.
        cid = _cid(req)
        self._sources[cid] = req.source
        drain = self._noting_merges(req, drain)
        # The same per-turn rebinding RpcTurnRunner applies, but deliberately
        # not its SUBAGENT widening: a channel session has no watched-signal to
        # gate on, and widening here would regress an abandoned-channel
        # question from an instant decline to a 600s stall. A channel user can
        # answer (gateway_commands wires a QuestionBroker onto
        # this tool, which renders clarify.request as an outbound message and
        # routes the reply back), while a CRON or otherwise background turn has
        # no reader and must decline rather than wait on nobody. Without this the
        # gateway's sub-agents saw no asker and no autofill at all.
        # Function-level on purpose: the acp client family is future shelf
        # cargo and must not be named at this module's import time
        # (binding-time debt).
        from raven.acp_client.asker import ApprovalViaAsk, AskViaTool, start_ask_turn
        from raven.acp_client.resolver import Autofill
        from raven.permissions import start_permission_turn

        tools = getattr(self._loop, "tools", None)
        ask_tool = tools.get("ask_user") if tools is not None else None
        interactive = req.origin is Origin.USER and isinstance(ask_tool, SupportsDirectAsk)
        asker = AskViaTool(ask_tool) if interactive else None
        # A channel user can answer a question, so they can answer an approval:
        # the responder rides the same broker round-trip the asker below uses.
        # Every other origin here (cron / sentinel / heartbeat) binds None by
        # decision, not omission -- those turns are unattended, the ask tier
        # refuses with a reason, and the operator picks smart or full for
        # background work that must mutate.
        start_permission_turn(
            ApprovalViaAsk(asker, cid) if asker is not None else None,
            conversation_id=cid,
            # An instance-addressed cron wake uses its conversation's policy.
            session=session_key_for(req) if req.direct_target else "",
            turn_id=req.turn_id or "",
            origin=req.origin.value,
        )
        start_ask_turn(
            asker,
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
        try:
            outcome = await self._loop.run_turn(req, emit, drain, stream=False, text_sink=text_sink)
        finally:
            # A turn that fails or is cancelled stores nothing, and the submitter
            # pops by conversation: without this, a cron job whose turn failed
            # reads the text its previous run left behind and records it as this
            # run's reply.
            if req.conversation is not None:
                self._readback_texts.pop(req.conversation, None)
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
    channels: Mapping[str, Channel] | None = None,
    ending: set[asyncio.Task[None]] | None = None,
    merged: dict[str, list[Source]] | None = None,
) -> Callable[[TurnEvent], Awaitable[None]]:
    """Adapt the delivery hub into the gateway's EventSink, with the
    lifecycle side effects the plain hub sink does not carry: on TurnEnded or
    TurnFailed fire ``on_turn_complete`` (the WakeScheduler's parked-wake
    signal), and on a non-cancelled failure deliver a user-visible error reply
    to the originating channel -- the failure's category when a model call was
    what failed. A cron turn the model gave no output for gets none: no one is
    waiting on it, and its job records the failure. A cancelled turn (/stop)
    fires the wake and sends no reply -- unless ``cut_by_reload`` says a
    generation swap did the cancelling, in which case the channel is told the
    reply was cut. A turn a user started that ended with no reply tells its
    channel so (``end_turn``): the typing indicator the user's message started
    is otherwise stopped only by a send, and no other origin starts one. A turn
    any origin opened tells the chat of each person's message merged into it
    while it ran (``merged``, filled by the runner), for the indicator that
    message started. That runs as a task held in ``ending``, not inline: the
    scheduler resolves a turn only after this sink returns, and stopping an
    indicator can be a network call.

    notify fires on every origin (cron / sentinel / heartbeat / channel) rather
    than on user turns only -- benign and slightly more correct:
    wake.on_turn_complete is a no-op unless a wake is parked, so the extra fires
    just un-park any turn, and a wake parked during a proactive turn is never
    stranded."""

    ending = set() if ending is None else ending
    merged = {} if merged is None else merged
    # The origin of each live turn, from its TurnStarted until it ends, fails or
    # is cancelled.
    origins: dict[str, Origin | None] = {}

    async def sink(event: TurnEvent) -> None:
        if isinstance(event, TurnStarted):
            origins[event.turn_id] = event.origin
            return
        if isinstance(event, (TurnEnded, TurnFailed)):
            source = sources.pop(event.conversation_id, None)
            origin = origins.pop(event.turn_id, None)
            people = merged.pop(event.turn_id, [])
            if isinstance(event, TurnFailed) and source is not None:
                if not event.cancelled:
                    # No one is waiting on a cron run, and its job records the
                    # failure.
                    if not (origin is Origin.CRON and event.no_output):
                        await hub.dispatch(
                            Text(content=_failed_turn_reply(event.error, reported=event.reported), source=source)
                        )
                elif cut_by_reload is not None and cut_by_reload():
                    # /stop is the user's own act and stays silent; a reload is
                    # not, so the reply it cut is owed at least a sentence.
                    await hub.dispatch(Text(content=_TURN_CUT_BY_RELOAD_REPLY, source=source))
            elif isinstance(event, TurnEnded) and not event.explicit_reply:
                # Only a person's message starts an indicator, the one that
                # opened this turn or one merged into it: ending a turn no person
                # wrote into would cancel the one a user turn is running in the
                # same chat.
                if origin is Origin.USER and source is not None:
                    people = [source, *people]
                for channel_name, chat_id in dict.fromkeys((person.channel, person.chat_id) for person in people):
                    task = asyncio.create_task(_end_turn((channels or {}).get(channel_name), chat_id))
                    ending.add(task)
                    task.add_done_callback(ending.discard)
            agent_loop.notify_turn_complete()
            return
        await hub.dispatch(event)

    return sink


async def _end_turn(channel: Channel | None, chat_id: str) -> None:
    """Best-effort: the turn is already over, so a channel that cannot be told
    so costs a log line, not the turn."""
    end = getattr(channel, "end_turn", None)
    if end is None:
        return
    try:
        await end(chat_id)
    except Exception as exc:
        logger.warning("gateway: {} could not end a turn with no reply: {}", getattr(channel, "name", "?"), exc)


async def _settle(tasks: set[asyncio.Task[None]], deadline: float) -> None:
    """Let the turn ends still in flight run until ``deadline`` (loop time), then
    cut them: a channel whose transport is gone costs its indicator, not the swap."""
    if not tasks:
        return
    _, pending = await asyncio.wait(set(tasks), timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if pending:
        logger.warning("gateway: {} turn end(s) still running when the grace expired; cut", len(pending))


#: Seconds ``teardown`` gives the outlets to deliver what is already queued.
#: Separate from the turn grace on purpose: a caller may allow a running turn
#: zero seconds and still owe the sentence that cutting it produced.
_DELIVERY_GRACE = 2.0


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
    not a token stream). Returns (scheduler, hub, readback_texts,
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
    ending: set[asyncio.Task[None]] = set()
    merged: dict[str, list[Source]] = {}
    # user>1 is safe now that per-turn tool state (message routing, context) is
    # turn-local: concurrent user turns no longer clobber each other's reply
    # target. system>1 lets a cron and a heartbeat/sentinel turn overlap.
    scheduler = Scheduler(
        GatewayTurnRunner(agent_loop, readback_texts, sources, merged),
        OriginPools(user=user_pool, system=system_pool),
        _make_gateway_sink(hub, agent_loop, sources, cut_by_reload, channels, ending, merged),
    )

    async def teardown() -> None:
        await scheduler.shutdown(grace=shutdown_grace)
        grace = max(shutdown_grace, _DELIVERY_GRACE)
        deadline = asyncio.get_running_loop().time() + grace
        # shutdown resolves the turns it cut, and the sink enqueues the sentence
        # each cut turn is owed while that happens. ``aclose`` cancels the outlet
        # workers mid-flight, so without a barrier here the sentence is queued
        # and then dropped -- a reload would be silent for exactly the user whose
        # reply it interrupted. Bounded: a channel whose transport is already
        # gone costs the sentence, not the swap. A turn end still in flight shares
        # that one grace rather than adding its own.
        try:
            await asyncio.wait_for(
                asyncio.gather(*(hub.wait_idle(name) for name in channels)),
                timeout=grace,
            )
        except TimeoutError:
            logger.warning("gateway: delivery still queued when the grace expired; closing anyway")
        await _settle(ending, deadline)
        await hub.aclose()

    return scheduler, hub, readback_texts, sources, teardown
