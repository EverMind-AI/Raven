"""Spine wiring for the TUI RPC turn path: the runner (a TuiTurnRunner driving
the agent loop's native run_turn with stream=True), the outlet that maps each
spine event to its wire event (token.delta / thinking.delta / tool.*), and the
sink that fires ``message.complete`` / ``error`` after the render barrier.

The TUI runs turns through spine (submit -> lane -> run_turn -> hub -> outlet).
All of token/reasoning/tool/Text flow through the hub to the TuiOutlet, so they
share one per-outlet FIFO. spine never imports rpc; rpc imports spine.

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
from raven.agent.tools.shell import ApprovalResponder, ExecTool
from raven.rpc.subscriptions import SubscriptionEmitter
from raven.spine import (
    Deliverable,
    EpisodeStart,
    MediaOut,
    Notice,
    NoticeKind,
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
        approval_responder: ApprovalResponder | None = None,
    ) -> None:
        super().__init__(agent_loop, stream=True)
        self._emitter = emitter
        self._usages = usages
        self._turn_ids = turn_ids
        self._readback_texts = readback_texts
        self._approval_responder = approval_responder

    async def run(self, req: TurnRequest, emit: Emit, drain: Drain) -> TurnOutcome:
        cid = _conversation_id(req)
        tools = getattr(self._loop, "tools", None)
        exec_tool = tools.get("exec") if tools is not None else None
        if isinstance(exec_tool, ExecTool):
            # Approval capability is rebound for every turn. Only USER origin
            # receives the TUI responder; CRON and other background origins can
            # share this process but must still fail closed as non-interactive.
            # The IDs bind any response to this exact conversation and turn.
            exec_tool.start_approval_turn(
                self._approval_responder if req.origin is Origin.USER else None,
                conversation_id=cid,
                turn_id=self._turn_ids.get(cid, ""),
            )
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
    (``error``) are emitted by the sink after the render barrier. A Notice the
    runtime raised about the turn itself (``action_blocked``) rides ``notice``;
    a MediaOut rides ``media``. Progress and tool-hint notices are eaten -- no
    client shows per-turn progress today (a known gap, deferred)."""

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
                            "blocking": out.blocking,
                            "display": out.display,
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
                            "metadata": out.metadata,
                            "diff": out.diff,
                            # Beside the rendered diff for a client that draws its
                            # own. Absent rather than null when a call changed no
                            # file, so every payload the wire already carried keeps
                            # its shape.
                            **({"file_change": out.file_change} if out.file_change else {}),
                        },
                    },
                )
        elif isinstance(out, Text):
            # A non-streamed reply (clarification / hook short-circuit / empty
            # fallback) rides one token.delta into the same buffer the streamed
            # reply uses, so message.complete finalizes it like any other text.
            if out.content:
                await self._emitter.emit(cid, {"type": "token.delta", "payload": {"text": out.content}})
        elif isinstance(out, Notice):
            # Only the kinds that describe what the RUNTIME did to the turn go
            # on the wire. Progress and tool-hint notices exist for text-only
            # channels that cannot draw a tool row; this client draws every call
            # already, so forwarding them would narrate the same work twice.
            if out.kind is NoticeKind.ACTION_BLOCKED:
                await self._emitter.emit(
                    cid,
                    {"type": "notice", "payload": {"kind": out.kind.value, "detail": out.detail or ""}},
                )
        elif isinstance(out, EpisodeStart):
            # Boundary marker; the TUI buckets this model call's reasoning +
            # text + tools into one collapsible episode.
            await self._emitter.emit(cid, {"type": "episode.start", "payload": {"index": out.index}})
        elif isinstance(out, MediaOut):
            # Paths, not bytes: both ends of this wire are on one machine (the
            # terminal is a child process; a protocol client spawns the agent
            # itself), and a turn can produce a file large enough that base64 on
            # a line-delimited channel would stall every other event behind it.
            #
            # An empty tuple is not emitted. The contract says the list is never
            # empty, and an event that delivers nothing would still make a client
            # draw an attachment row.
            if out.media:
                await self._emitter.emit(
                    cid,
                    {
                        "type": "media",
                        "payload": {"items": [{"path": m.path, "mime": m.mime, "kind": m.kind} for m in out.media]},
                    },
                )

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

    async def emit_error(
        self,
        conversation_id: str,
        code: int,
        message: str,
        reason: str,
        detail: str = "",
        turn_id: str = "",
    ) -> None:
        """A turn's failure, tagged with the turn it belongs to when known.

        ``turn_id`` matters to any consumer that answers a *request* off this
        event. One session's subscription also carries turns the runtime
        submitted, and this lane is shared -- see ``_owns_lane`` -- so a consumer
        with no id to compare cannot tell a foreign turn's failure from its own,
        and will answer the wrong request. Empty when the caller did not know the
        turn, which a consumer must read as "not mine" rather than as "mine".
        """
        payload: dict[str, Any] = {"code": code, "message": message, "reason": reason}
        if turn_id:
            payload["turn_id"] = turn_id
        if detail:
            payload["detail"] = detail
        await self._emitter.emit(conversation_id, {"type": "error", "payload": payload})


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

    def _owns_lane(conversation_id: str, turn_id: str) -> bool:
        """Whether the ending turn is the one ``turn.send`` bound this lane to.

        A lane is serial but its slots are per-lane, so a turn the runtime
        submitted itself can end while a client's turn is still QUEUED behind it
        on the same lane. Releasing the slots there opens the -32003 guard for a
        second send and leaves the queued turn's own end with no binding to
        report against.
        """
        return bool(turn_id) and turn_ids.get(conversation_id) == turn_id

    def _drop(conversation_id: str, *, owns: bool) -> None:
        if not owns:
            return
        # usages is keyed by lane like turn_ids, so it is gated the same way: a
        # turn cancelled while queued shares this key with whichever turn is
        # actually running, and popping unconditionally would drop that turn's
        # just-written usage before its own TurnEnded reads it.
        turn_ids.pop(conversation_id, None)
        usages.pop(conversation_id, None)
        if on_turn_end is not None:
            on_turn_end(conversation_id)

    async def sink(event: TurnEvent) -> None:
        if isinstance(event, TurnEnded):
            await _finish(event.conversation_id)
            usage = usages.get(event.conversation_id) or {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
            # Read before _drop pops the register.
            _drop(event.conversation_id, owns=_owns_lane(event.conversation_id, event.turn_id))
            # The ending turn's own id, never the lane slot's current value: the
            # slot may hold a client turn that has not started yet.
            await outlet.emit_complete(event.conversation_id, event.turn_id, usage)
            return
        if isinstance(event, TurnFailed):
            await _finish(event.conversation_id)
            _drop(event.conversation_id, owns=_owns_lane(event.conversation_id, event.turn_id))
            # A cancelled turn's error is emitted by turn.cancel, not here, to
            # avoid a double error event.
            if not event.cancelled:
                await outlet.emit_error(
                    event.conversation_id,
                    _TURN_FAILED_CODE,
                    "turn_failed",
                    "internal",
                    event.error or "",
                    # The ending turn's own id, for the same reason
                    # ``emit_complete`` takes it rather than reading the lane
                    # slot: the slot may hold a client turn that has not started.
                    turn_id=event.turn_id,
                )
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
    approval_responder: ApprovalResponder | None = None,
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
    private map when cron is not wired (e.g. tests).

    ``approval_responder`` is an interactive capability, not a process-wide
    permission. The runner binds it only to USER-origin turns and explicitly
    revokes it for background origins."""
    hub = DeliveryHub()
    outlet = TuiOutlet(channel, emitter)
    hub.register(outlet)
    turn_ids: dict[str, str] = {}
    usages: dict[str, dict[str, Any]] = {}
    if readback_texts is None:
        readback_texts = {}
    scheduler = Scheduler(
        TuiTurnRunner(
            agent_loop,
            emitter,
            usages,
            turn_ids,
            readback_texts,
            approval_responder=approval_responder,
        ),
        OriginPools(user=user_pool, system=system_pool),
        _make_tui_sink(hub, outlet, channel, turn_ids, usages, on_turn_end),
    )

    async def teardown() -> None:
        await scheduler.shutdown(grace=0.0)
        await hub.aclose()

    return scheduler, hub, turn_ids, teardown
