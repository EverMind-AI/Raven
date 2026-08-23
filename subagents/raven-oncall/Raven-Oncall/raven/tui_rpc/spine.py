"""Spine wiring for the TUI RPC turn path: the runner (a TuiTurnRunner driving
the agent loop's native run_turn with stream=True), the outlet that maps each
spine event to its wire event (token.delta / thinking.delta / tool.*), and the
sink that fires ``message.complete`` / ``error`` after the render barrier.

The TUI runs turns through spine (submit -> lane -> run_turn -> hub -> outlet).
All of token/reasoning/tool/Text flow through the hub to the TuiOutlet, so they
share one per-outlet FIFO. spine never imports tui_rpc; tui_rpc imports spine.

Why ``message.complete`` is fired from the sink (not from a stream-close): it is
an unconditional per-turn signal — the front-end clears its turn slot on it, so a
turn that streams nothing (empty reply, tool-only) must still emit it or the UI
wedges. The sink awaits ``wait_idle`` first so it lands after the turn's last
``token.delta``; an empty turn never built a queue, so the barrier returns at
once. This is the REPL's ``result() -> wait_idle`` render barrier moved into the
sink.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from raven.agent.spine_runner import AgentTurnRunner
from raven.agent.tools.message import MessageTool
from raven.spine import (
    Deliverable,
    Origin,
    OriginPools,
    Reasoning,
    Scheduler,
    Text,
    ToolEvent,
    ToolPhase,
    TurnEnded,
    TurnFailed,
    TurnOutcome,
    TurnRequest,
    TurnStarted,
)
from raven.spine.delivery import Capabilities, DeliveryHub
from raven.spine.events import TurnEvent
from raven.spine.runner import Drain, Emit
from raven.tui_rpc.subscriptions import SubscriptionEmitter

_TURN_FAILED_CODE = -32099


def _conversation_id(req: TurnRequest) -> str:
    return req.conversation or f"{req.source.channel}:{req.source.chat_id}"


class TuiTurnRunner(AgentTurnRunner):
    """Runs a TUI turn through the agent loop's native run_turn (stream=True), so
    token/reasoning/tool/Text all flow through the hub to the TuiOutlet (one
    per-outlet FIFO — no dual path). Two TUI-specific bits the generic runner
    does not carry:

    - it passes its own ``usage_sink`` so the sink can attach the full usage
      (cost / context, richer than the three-field TurnOutcome.usage) to
      ``message.complete``; the rich usage stays TUI-internal, off the wire;
    - it fires the synthetic tool.complete when the turn replied via the
      message tool (the loop's general tool path skips the message tool), so the
      UI records that the agent acted.
    """

    def __init__(
        self,
        agent_loop: Any,
        emitter: SubscriptionEmitter,
        usages: dict[str, dict[str, Any]],
        turn_ids: dict[str, str],
        readback_texts: dict[str, str],
    ) -> None:
        super().__init__(agent_loop, stream=True)
        self._emitter = emitter
        self._usages = usages
        self._turn_ids = turn_ids
        self._readback_texts = readback_texts

    async def run(self, req: TurnRequest, emit: Emit, drain: Drain) -> TurnOutcome:
        cid = _conversation_id(req)
        # A CRON turn is not a user turn: it runs non-streaming (one reply, not a
        # token stream) and its reply text is captured for the cron fan-out, which
        # delivers a cron.delivered event to every session (the cron:<job_id>
        # conversation has no subscriber, so its hub deliverables no-op). Mirrors
        # the gateway's GatewayTurnRunner read-back path.
        if req.origin is Origin.CRON:
            text_sink: dict[str, str] = {}
            outcome = await self._loop.run_turn(req, emit, drain, stream=False, text_sink=text_sink)
            if req.conversation is not None and (text := text_sink.get("text")) is not None:
                self._readback_texts[req.conversation] = text
            return outcome
        usage_sink: dict[str, Any] = {}
        outcome = await self._loop.run_turn(
            req, emit, drain, stream=True, inline_tool_stream=True, usage_sink=usage_sink
        )

        # A synthetic tool.complete when the message tool fired (the loop
        # skips it on the general tool path), so the UI records the agent acted —
        # its reply already streamed as token deltas. Emitted before returning, so
        # it lands in the turn's event stream ahead of TurnEnded.
        message_tool = self._loop.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool.sent_in_turn:
            turn_id = self._turn_ids.get(cid, "")
            await emit(
                ToolEvent(
                    phase=ToolPhase.COMPLETE,
                    tool_call_id=f"msg-{turn_id}",
                    result_preview="(message sent via tool)",
                )
            )

        self._usages[cid] = dict(usage_sink)
        return outcome


class TuiOutlet:
    """The TUI's send surface. Maps each spine event to its wire event on the
    conversation's subscription: streamed token content via ``send_stream_chunk``
    (-> token.delta), and the discrete deliverables via ``deliver`` (Reasoning ->
    thinking.delta, ToolEvent -> tool.start / tool.complete, a non-streamed Text
    -> a token.delta). The turn's completion (``message.complete``) and failure
    (``error``) are emitted by the sink after the render barrier. Notice and
    MediaOut are eaten — the wire protocol has no event for them and the TUI shows
    no per-turn progress or tool media today (a known gap, deferred)."""

    def __init__(self, channel: str, emitter: SubscriptionEmitter) -> None:
        self.name = channel
        self.capabilities = Capabilities(streaming=True)
        self._emitter = emitter

    async def deliver(self, out: Deliverable) -> None:
        cid = out.conversation_id
        if isinstance(out, Reasoning):
            if out.content:
                await self._emitter.emit(cid, {"type": "thinking.delta", "payload": {"text": out.content}})
        elif isinstance(out, ToolEvent):
            if out.phase is ToolPhase.START:
                await self._emitter.emit(
                    cid,
                    {
                        "type": "tool.start",
                        "payload": {
                            "tool_call_id": out.tool_call_id,
                            "name": out.name,
                            "arguments": out.arguments or {},
                        },
                    },
                )
            else:
                await self._emitter.emit(
                    cid,
                    {
                        "type": "tool.complete",
                        "payload": {
                            "tool_call_id": out.tool_call_id,
                            "result_preview": out.result_preview,
                            "truncated": out.truncated,
                        },
                    },
                )
        elif isinstance(out, Text):
            # A non-streamed reply (clarification / hook short-circuit / empty
            # fallback) rides one token.delta into the same buffer the streamed
            # reply uses, so message.complete finalizes it like any other text.
            if out.content:
                await self._emitter.emit(cid, {"type": "token.delta", "payload": {"text": out.content}})
        # Notice / MediaOut: eaten (no wire event today).

    async def send_stream_chunk(self, chat_id: str, stream_id: str, delta: str, *, done: bool = False) -> None:
        if done:
            # The front-end has no stream-done event; the turn is finalized by
            # message.complete (emitted by the sink). done=True only lets the hub
            # close its stream state.
            return
        if not delta:
            return
        await self._emitter.emit(stream_id, {"type": "token.delta", "payload": {"text": delta}})

    async def emit_complete(self, conversation_id: str, turn_id: str | None, usage: dict[str, Any]) -> None:
        await self._emitter.emit(
            conversation_id,
            {"type": "message.complete", "payload": {"turn_id": turn_id, "usage": usage}},
        )

    async def emit_error(self, conversation_id: str, code: int, message: str, reason: str) -> None:
        await self._emitter.emit(
            conversation_id,
            {"type": "error", "payload": {"code": code, "message": message, "reason": reason}},
        )


def _make_tui_sink(
    hub: DeliveryHub,
    outlet: TuiOutlet,
    channel: str,
    turn_ids: dict[str, str],
    usages: dict[str, dict[str, Any]],
    on_turn_end: Callable[[str], None] | None,
) -> Callable[[TurnEvent], Awaitable[None]]:
    """Adapt the hub into the scheduler's EventSink for the TUI. Deliverables
    route through the hub; a turn's end fires message.complete / error after the
    render barrier (so they land after the last token.delta). ``on_turn_end`` is
    called at each turn exit (before message.complete) so turn.send's active-turn
    slot is cleared before the front-end is told it may submit the next turn.
    This sink is build_tui's alone — the CLI keeps its own lifecycle-dropping
    sink."""

    async def _finish(conversation_id: str) -> None:
        # close_stream clears the hub's per-stream state (so the next turn on this
        # conversation reopens cleanly); wait_idle then blocks until every queued
        # token.delta has been delivered — an empty turn never built a queue, so
        # it returns at once.
        await hub.close_stream(conversation_id)
        await hub.wait_idle(channel)

    def _drop(conversation_id: str) -> None:
        turn_ids.pop(conversation_id, None)
        usages.pop(conversation_id, None)
        if on_turn_end is not None:
            on_turn_end(conversation_id)

    async def sink(event: TurnEvent) -> None:
        if isinstance(event, TurnEnded):
            await _finish(event.conversation_id)
            turn_id = turn_ids.get(event.conversation_id)
            usage = usages.get(event.conversation_id) or {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
            _drop(event.conversation_id)
            await outlet.emit_complete(event.conversation_id, turn_id, usage)
            return
        if isinstance(event, TurnFailed):
            await _finish(event.conversation_id)
            _drop(event.conversation_id)
            # A cancelled turn's error is emitted by turn.cancel, not here, to
            # avoid a double error event.
            if not event.cancelled:
                await outlet.emit_error(event.conversation_id, _TURN_FAILED_CODE, "turn_failed", "internal")
            return
        if isinstance(event, TurnStarted):
            # message.start is emitted by turn.send (it owns the turn_id).
            return
        await hub.dispatch(event)

    return sink


def build_tui(
    agent_loop: Any,
    emitter: SubscriptionEmitter,
    *,
    channel: str = "tui",
    on_turn_end: Callable[[str], None] | None = None,
    readback_texts: dict[str, str] | None = None,
    user_pool: int = 1,
    system_pool: int = 1,
) -> tuple[Scheduler, DeliveryHub, dict[str, str], Callable[[], Awaitable[None]]]:
    """Wire the spine pieces a TUI turn flows through: a hub with the channel's
    TuiOutlet, and a Scheduler whose runner streams the agent loop and whose sink
    fires message.complete / error after the render barrier. Returns those plus
    the ``turn_ids`` map (turn.send binds conversation_id -> turn_id so the sink
    can attach it to message.complete) and a ``teardown`` the caller awaits on
    exit (stop the scheduler, then close the hub's workers). ``on_turn_end`` lets
    turn.send drop its active-turn slot at each turn exit.

    ``readback_texts`` is the cron read-back map (conversation -> reply text): the
    runner stores a CRON turn's reply there so the cron fan-out can deliver it as a
    cron.delivered event. Pass the same dict the cron callback reads; defaults to a
    private map when cron is not wired (e.g. tests)."""
    hub = DeliveryHub()
    outlet = TuiOutlet(channel, emitter)
    hub.register(outlet)
    turn_ids: dict[str, str] = {}
    usages: dict[str, dict[str, Any]] = {}
    if readback_texts is None:
        readback_texts = {}
    scheduler = Scheduler(
        TuiTurnRunner(agent_loop, emitter, usages, turn_ids, readback_texts),
        OriginPools(user=user_pool, system=system_pool),
        _make_tui_sink(hub, outlet, channel, turn_ids, usages, on_turn_end),
    )

    async def teardown() -> None:
        await scheduler.shutdown(grace=0.0)
        await hub.aclose()

    return scheduler, hub, turn_ids, teardown
