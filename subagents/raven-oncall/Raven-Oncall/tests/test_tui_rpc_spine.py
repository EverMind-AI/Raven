from dataclasses import replace

from raven.agent.tools.message import MessageTool
from raven.spine import (
    ChatType,
    MediaOut,
    Notice,
    NoticeKind,
    Origin,
    Reasoning,
    Source,
    StreamDelta,
    Text,
    ToolEvent,
    ToolPhase,
    TurnOutcome,
    TurnRequest,
    TurnRunner,
    Usage,
)
from raven.spine.delivery import Outlet, SupportsStreaming
from raven.spine.message import Media
from raven.tui_rpc.spine import (
    TuiOutlet,
    TuiTurnRunner,
    build_tui,
)


def _src(channel="tui", chat_id="c1") -> Source:
    return Source(channel=channel, chat_id=chat_id, sender_id="user", chat_type=ChatType.DM)


class FakeEmitter:
    """Records (session_key, event) — stands in for SubscriptionEmitter."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, session_key: str, event: dict) -> None:
        self.emitted.append((session_key, event))

    def types(self) -> list[str]:
        return [e["type"] for _k, e in self.emitted]


class _RunTurnLoop:
    """Fake AgentLoop whose run_turn emits scripted spine events and fills the
    caller's usage_sink — stands in for the native run_turn (stream=True). For a
    CRON turn the runner passes stream=False + text_sink; ``reply_text`` is written
    into text_sink so the read-back path can be exercised."""

    def __init__(self, events=(), usage=None, *, tools=None, reply_text=None) -> None:
        self._events = list(events)
        self._usage = usage
        self._reply_text = reply_text
        self.tools = tools if tools is not None else {}
        self.last_stream = None

    async def run_turn(
        self, req, emit, drain, *, stream, inline_tool_stream=False, usage_sink=None, text_sink=None
    ) -> TurnOutcome:
        self.last_stream = stream
        for ev in self._events:
            await emit(ev)
        if usage_sink is not None and self._usage:
            usage_sink.update(self._usage)
        if text_sink is not None and self._reply_text is not None:
            text_sink["text"] = self._reply_text
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)


def _collect():
    events: list = []

    async def emit(e):
        events.append(e)

    return events, emit


# --- protocol conformance ---


def test_pieces_satisfy_their_spine_protocols():
    assert isinstance(TuiTurnRunner(object(), FakeEmitter(), {}, {}, {}), TurnRunner)
    outlet = TuiOutlet("tui", FakeEmitter())
    assert isinstance(outlet, Outlet)
    assert isinstance(outlet, SupportsStreaming)
    assert outlet.capabilities.streaming is True


# --- TuiTurnRunner (drives run_turn stream=True; stashes rich usage) ---


async def test_runner_drives_run_turn_and_stashes_rich_usage():
    rich = {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8, "cost_usd": 0.01, "context_used": 42}
    loop = _RunTurnLoop(events=[StreamDelta(delta="he"), StreamDelta(delta="llo")], usage=rich)
    usages: dict[str, dict] = {}
    runner = TuiTurnRunner(loop, FakeEmitter(), usages, {}, {})
    req = TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1")
    events, emit = _collect()

    outcome = await runner.run(req, emit, lambda: [])

    # run_turn's events pass straight through emit (the outlet maps them to wire).
    assert [e.delta for e in events] == ["he", "llo"]
    # The full usage_sink (cost / context, richer than 3-field Usage) is stashed
    # for the sink to attach to message.complete.
    assert usages["tui:c1"] == rich
    assert outcome.explicit_reply is True


async def test_runner_emits_eve22_synthetic_tool_complete_when_message_tool_fired():
    message_tool = MessageTool()
    loop = _RunTurnLoop(tools={"message": message_tool})

    async def _run_turn(req, emit, drain, *, stream, inline_tool_stream=False, usage_sink=None):
        # the message tool replied this turn (turn-local sent flag)
        message_tool._turn.set(replace(message_tool._cur(), sent=True))
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    loop.run_turn = _run_turn
    runner = TuiTurnRunner(loop, FakeEmitter(), {}, {"tui:c1": "T7"}, {})
    req = TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1")
    events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    # A lone synthetic ToolEvent(COMPLETE) keyed by the turn id (no matching start;
    # the loop skips the message tool on its general path).
    assert len(events) == 1 and isinstance(events[0], ToolEvent)
    assert events[0].phase is ToolPhase.COMPLETE and events[0].tool_call_id == "msg-T7"


async def test_runner_no_synthetic_when_message_tool_did_not_fire():
    loop = _RunTurnLoop(tools={"message": MessageTool()})  # sent flag stays False
    runner = TuiTurnRunner(loop, FakeEmitter(), {}, {"tui:c1": "T7"}, {})
    events, emit = _collect()
    await runner.run(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1"), emit, lambda: [])
    assert events == []  # no synthetic completion


async def test_runner_cron_captures_reply_non_streaming():
    # A CRON turn runs non-streaming and its reply is read back for the cron
    # fan-out (the cron:<job_id> conversation has no subscriber, so streaming it
    # would deliver nowhere). Mirrors the gateway's GatewayTurnRunner read-back.
    loop = _RunTurnLoop(reply_text="reminder fired")
    readback: dict[str, str] = {}
    runner = TuiTurnRunner(loop, FakeEmitter(), {}, {}, readback)
    req = TurnRequest(origin=Origin.CRON, source=_src(chat_id="direct"), text="[cron]", conversation="cron:job1")
    events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    assert loop.last_stream is False  # CRON runs non-streaming
    assert readback["cron:job1"] == "reminder fired"  # reply captured for fan-out


# --- TuiOutlet.deliver: maps each spine event to its wire event ---


async def test_outlet_deliver_reasoning_to_thinking_delta():
    emitter = FakeEmitter()
    outlet = TuiOutlet("tui", emitter)
    await outlet.deliver(Reasoning(content="thinking", conversation_id="tui:c1"))
    assert emitter.emitted == [("tui:c1", {"type": "thinking.delta", "payload": {"text": "thinking"}})]


async def test_outlet_deliver_tool_event_to_tool_start_and_complete():
    emitter = FakeEmitter()
    outlet = TuiOutlet("tui", emitter)
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.START, tool_call_id="t1", name="shell", arguments={"cmd": "ls"}, conversation_id="tui:c1"
        )
    )
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE, tool_call_id="t1", result_preview="ok", truncated=False, conversation_id="tui:c1"
        )
    )
    assert emitter.emitted == [
        (
            "tui:c1",
            {"type": "tool.start", "payload": {"tool_call_id": "t1", "name": "shell", "arguments": {"cmd": "ls"}}},
        ),
        (
            "tui:c1",
            {"type": "tool.complete", "payload": {"tool_call_id": "t1", "result_preview": "ok", "truncated": False}},
        ),
    ]


async def test_outlet_deliver_text_to_token_delta():
    # A non-streamed reply (clarification / hook short-circuit) rides one
    # token.delta — previously dropped on the TUI (the earlier dual-path runner ignored the
    # direct path's return value).
    emitter = FakeEmitter()
    outlet = TuiOutlet("tui", emitter)
    await outlet.deliver(Text(content="please clarify", conversation_id="tui:c1"))
    assert emitter.emitted == [("tui:c1", {"type": "token.delta", "payload": {"text": "please clarify"}})]


async def test_outlet_deliver_eats_notice_and_media():
    emitter = FakeEmitter()
    outlet = TuiOutlet("tui", emitter)
    await outlet.deliver(Notice(kind=NoticeKind.PROGRESS, detail="working", conversation_id="tui:c1"))
    await outlet.deliver(
        MediaOut(media=(Media(path="/tmp/x.png", mime="image/png", kind="image"),), conversation_id="tui:c1")
    )
    assert emitter.emitted == []  # no wire event for either today


async def test_outlet_emits_token_delta_on_a_chunk():
    emitter = FakeEmitter()
    outlet = TuiOutlet("tui", emitter)
    await outlet.send_stream_chunk("c1", "tui:c1", "hi", done=False)
    assert emitter.emitted == [("tui:c1", {"type": "token.delta", "payload": {"text": "hi"}})]


async def test_outlet_done_chunk_is_a_noop():
    emitter = FakeEmitter()
    outlet = TuiOutlet("tui", emitter)
    await outlet.send_stream_chunk("c1", "tui:c1", "", done=True)
    assert emitter.emitted == []  # front-end has no stream-done event; complete is the sink's


async def test_outlet_eats_empty_delta():
    emitter = FakeEmitter()
    outlet = TuiOutlet("tui", emitter)
    await outlet.send_stream_chunk("c1", "tui:c1", "", done=False)
    assert emitter.emitted == []


async def test_outlet_emit_complete_and_error_shapes():
    emitter = FakeEmitter()
    outlet = TuiOutlet("tui", emitter)
    await outlet.emit_complete("tui:c1", "t1", {"total_tokens": 7})
    await outlet.emit_error("tui:c1", -32099, "turn_failed", "internal")
    assert emitter.emitted == [
        ("tui:c1", {"type": "message.complete", "payload": {"turn_id": "t1", "usage": {"total_tokens": 7}}}),
        ("tui:c1", {"type": "error", "payload": {"code": -32099, "message": "turn_failed", "reason": "internal"}}),
    ]


# --- build_tui: real Scheduler + DeliveryHub + TuiOutlet, only the edges faked ---
# (faking the spine path would make the ordering / deadlock tests pass trivially;
#  message.complete-after-token and the empty-turn finalize only hold on the real
#  async path through the hub's per-outlet queue + wait_idle barrier.)


async def test_build_tui_defaults_to_single_slot_pools():
    scheduler, _hub, _turn_ids, teardown = build_tui(_RunTurnLoop(events=[]), FakeEmitter())
    try:
        assert scheduler._pools._user._value == 1
        assert scheduler._pools._system._value == 1
    finally:
        await teardown()


async def test_build_tui_honors_configured_pool_sizes():
    scheduler, _hub, _turn_ids, teardown = build_tui(_RunTurnLoop(events=[]), FakeEmitter(), user_pool=6, system_pool=4)
    try:
        assert scheduler._pools._user._value == 6
        assert scheduler._pools._system._value == 4
    finally:
        await teardown()


async def test_streaming_turn_emits_token_deltas_then_message_complete():
    emitter = FakeEmitter()
    loop = _RunTurnLoop(
        events=[StreamDelta(delta="a"), StreamDelta(delta="b")],
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )
    scheduler, hub, turn_ids, teardown = build_tui(loop, emitter)
    try:
        turn_ids["tui:c1"] = "t1"  # turn.send binds this; emulate here
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1"))
        await handle.result()
    finally:
        await teardown()

    # message.complete lands AFTER both token.delta events (wait_idle barrier).
    assert emitter.types() == ["token.delta", "token.delta", "message.complete"]
    last_key, last = emitter.emitted[-1]
    assert last_key == "tui:c1"
    assert last["payload"] == {
        "turn_id": "t1",
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


async def test_interleaved_events_keep_emit_order_through_one_queue():
    # Folding reasoning/tool into the hub (no dual StreamAdapter path) means
    # token/reasoning/tool share one per-outlet FIFO, so the wire order matches
    # the emit order — the cross-type ordering the earlier dual path could not promise.
    emitter = FakeEmitter()
    loop = _RunTurnLoop(
        events=[
            Reasoning(content="thinking"),
            ToolEvent(phase=ToolPhase.START, tool_call_id="t1", name="shell", arguments={}),
            StreamDelta(delta="answer"),
            ToolEvent(phase=ToolPhase.COMPLETE, tool_call_id="t1", result_preview="ok", truncated=False),
        ]
    )
    scheduler, hub, turn_ids, teardown = build_tui(loop, emitter)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1"))
        await handle.result()
    finally:
        await teardown()

    assert emitter.types() == ["thinking.delta", "tool.start", "token.delta", "tool.complete", "message.complete"]


async def test_non_streamed_text_reaches_the_wire_as_a_token_delta():
    # A clarification / hook short-circuit reply (a Text, not a stream) now renders
    # on the TUI — the earlier runner dropped it (it ignored the direct path's return value).
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[Text(content="which file?")])
    scheduler, hub, turn_ids, teardown = build_tui(loop, emitter)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1"))
        await handle.result()
    finally:
        await teardown()

    assert emitter.types() == ["token.delta", "message.complete"]
    assert emitter.emitted[0][1]["payload"]["text"] == "which file?"


async def test_empty_stream_turn_still_emits_message_complete():
    # Deadlock regression: a turn that streams nothing must STILL finalize, or the
    # front-end's turn slot never clears and the next turn is rejected forever.
    emitter = FakeEmitter()
    scheduler, hub, turn_ids, teardown = build_tui(_RunTurnLoop(events=[]), emitter)
    try:
        turn_ids["tui:c1"] = "t9"
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1"))
        await handle.result()
    finally:
        await teardown()

    assert emitter.types() == ["message.complete"]  # no token.delta, but still finalized
    assert emitter.emitted[-1][1]["payload"]["turn_id"] == "t9"


async def test_cron_turn_deliverables_key_to_dead_conversation_not_user_session():
    # No-double-delivery: a CRON turn's spine deliverables key to its
    # cron:<job_id> conversation (no user subscriber -> no-op in the real
    # emitter), so a user session never sees a stray token.delta/message.complete.
    # The only delivery to a user session is the wrapper's cron.delivered fan-out
    # (tested separately). The reply is read back for that fan-out.
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[Text(content="reminder")], reply_text="reminder")
    readback: dict[str, str] = {}
    scheduler, hub, turn_ids, teardown = build_tui(loop, emitter, readback_texts=readback)
    try:
        handle = scheduler.submit(
            TurnRequest(
                origin=Origin.CRON,
                source=_src(chat_id="direct"),
                text="[cron]",
                conversation="cron:job1",
            )
        )
        await handle.result()
        await hub.wait_idle("tui")
    finally:
        await teardown()

    # Every spine emit keys to the dead cron:<job_id>; none leak onto a user key.
    assert emitter.emitted and all(key == "cron:job1" for key, _ev in emitter.emitted)
    assert readback["cron:job1"] == "reminder"  # captured for the fan-out


async def test_failed_turn_emits_error():
    class _BoomLoop:
        async def run_turn(self, req, emit, drain, *, stream, inline_tool_stream=False, usage_sink=None) -> TurnOutcome:
            raise RuntimeError("boom")

    emitter = FakeEmitter()
    scheduler, hub, turn_ids, teardown = build_tui(_BoomLoop(), emitter)
    try:
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1"))
        await handle.result()
    finally:
        await teardown()

    assert emitter.types() == ["error"]
    assert emitter.emitted[-1][1]["payload"]["reason"] == "internal"


async def test_cancelled_turn_does_not_emit_error():
    # A cancelled turn's error is turn.cancel's to emit; the sink must stay silent
    # so the client does not get two error frames.
    import asyncio

    started = asyncio.Event()

    class _HangLoop:
        async def run_turn(self, req, emit, drain, *, stream, inline_tool_stream=False, usage_sink=None) -> TurnOutcome:
            started.set()
            await asyncio.sleep(3600)
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    emitter = FakeEmitter()
    scheduler, hub, turn_ids, teardown = build_tui(_HangLoop(), emitter)
    try:
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1"))
        await started.wait()
        handle.cancel()
        await handle.result()
    finally:
        await teardown()

    assert emitter.emitted == []  # no error from the sink on cancellation
