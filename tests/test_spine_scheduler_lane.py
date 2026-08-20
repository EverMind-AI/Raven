import asyncio

from raven.spine import (
    ChatType,
    Notice,
    NoticeKind,
    Origin,
    Source,
    Text,
    TurnEnded,
    TurnFailed,
    TurnOutcome,
    TurnRequest,
    TurnStarted,
    Usage,
)
from raven.spine.scheduler import Lane, OriginPools


def _req(text: str = "hi") -> TurnRequest:
    src = Source(channel="t", chat_id="c", sender_id="u", chat_type=ChatType.DM)
    return TurnRequest(origin=Origin.USER, source=src, text=text)


def _collector():
    events: list = []

    async def sink(event) -> None:
        events.append(event)

    return events, sink


def _lane(runner) -> Lane:
    return Lane(runner=runner, pools=OriginPools(user=1, system=1), sink=_collector()[1], conversation_id="c")


# --- runners (fakes; the real runner is the agent loop) ---


class SuccessRunner:
    def __init__(self, outcome: TurnOutcome | None = None):
        self.outcome = outcome or TurnOutcome(usage=Usage(1, 2, 3), explicit_reply=True)

    async def run(self, req, emit, drain) -> TurnOutcome:
        await emit(Text(content="reply"))
        return self.outcome


class OrderRunner:
    def __init__(self):
        self.order: list[str] = []
        self.live = 0
        self.max_live = 0

    async def run(self, req, emit, drain) -> TurnOutcome:
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        self.order.append(req.text)
        await asyncio.sleep(0)
        self.live -= 1
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False)


class HangingRunner:
    def __init__(self):
        self.started = asyncio.Event()

    async def run(self, req, emit, drain) -> TurnOutcome:
        self.started.set()
        await asyncio.Event().wait()  # hang until cancelled
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False)


class FailingRunner:
    async def run(self, req, emit, drain) -> TurnOutcome:
        raise ValueError("boom")


class RecordingHangRunner:
    def __init__(self):
        self.ran: list[str] = []
        self.first_started = asyncio.Event()

    async def run(self, req, emit, drain) -> TurnOutcome:
        self.ran.append(req.text)
        if len(self.ran) == 1:
            self.first_started.set()
            await asyncio.Event().wait()  # the first turn hangs until cancelled
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False)


class LifecycleEmittingRunner:
    def __init__(self):
        self.rejected: bool | None = None

    async def run(self, req, emit, drain) -> TurnOutcome:
        try:
            await emit(TurnStarted())  # a runner must not emit lifecycle
            self.rejected = False
        except TypeError:
            self.rejected = True
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False)


class StampProbeRunner:
    def __init__(self, source=None):
        self._source = source

    async def run(self, req, emit, drain) -> TurnOutcome:
        await emit(Text(content="x", source=self._source))
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False)


# --- tests ---


async def test_lane_runs_fifo_one_at_a_time():
    runner = OrderRunner()
    events, sink = _collector()
    lane = Lane(runner=runner, pools=OriginPools(user=5, system=5), sink=sink, conversation_id="c")
    futs = [lane.submit(_req(t)) for t in ("a", "b", "c")]
    await asyncio.gather(*futs)
    assert runner.order == ["a", "b", "c"]  # FIFO
    assert runner.max_live == 1  # single running turn even with a 5-slot pool


async def test_success_emits_started_then_deliverables_then_ended():
    runner = SuccessRunner(TurnOutcome(usage=Usage(5, 7, 12), explicit_reply=True))
    events, sink = _collector()
    lane = Lane(runner=runner, pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    await lane.submit(_req())
    assert isinstance(events[0], TurnStarted)
    assert isinstance(events[1], Text)
    assert isinstance(events[-1], TurnEnded)
    assert events[-1].usage == Usage(5, 7, 12)
    assert events[-1].explicit_reply is True


async def test_emit_guard_rejects_lifecycle_events_at_runtime():
    # The only enforcement of "a runner cannot emit lifecycle" without a static
    # checker: the emit closure must reject it at runtime.
    runner = LifecycleEmittingRunner()
    events, sink = _collector()
    lane = Lane(runner=runner, pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    await lane.submit(_req())
    assert runner.rejected is True


async def test_emit_stamps_source_when_absent_and_preserves_explicit():
    src = Source(channel="t", chat_id="c", sender_id="u", chat_type=ChatType.DM)
    other = Source(channel="x", chat_id="y", sender_id="z", chat_type=ChatType.GROUP)

    events1, sink1 = _collector()
    lane1 = Lane(
        runner=StampProbeRunner(source=None), pools=OriginPools(user=1, system=1), sink=sink1, conversation_id="c"
    )
    await lane1.submit(TurnRequest(origin=Origin.USER, source=src, text="x"))
    text1 = next(e for e in events1 if isinstance(e, Text))
    assert text1.source == src  # stamped with the request's source

    events2, sink2 = _collector()
    lane2 = Lane(
        runner=StampProbeRunner(source=other), pools=OriginPools(user=1, system=1), sink=sink2, conversation_id="c"
    )
    await lane2.submit(TurnRequest(origin=Origin.USER, source=src, text="x"))
    text2 = next(e for e in events2 if isinstance(e, Text))
    assert text2.source == other  # explicit source is not overwritten


async def test_emit_stamps_source_on_a_non_text_deliverable():
    # A Notice (like ToolEvent/Reasoning) carries no source from the runner;
    # emit stamps the turn's source so the hub can route it.
    src = Source(channel="tg", chat_id="1", sender_id="u", chat_type=ChatType.DM)
    events, sink = _collector()

    class R:
        async def run(self, req, emit, drain) -> TurnOutcome:
            await emit(Notice(kind=NoticeKind.PROGRESS))  # no source
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False)

    lane = Lane(runner=R(), pools=OriginPools(user=1, system=1), sink=sink, conversation_id="tg:1")
    await lane.submit(TurnRequest(origin=Origin.USER, source=src, text="x"))
    notice = next(e for e in events if isinstance(e, Notice))
    assert notice.source == src  # stamped with the turn's source


async def test_emit_stamps_conversation_id_and_preserves_explicit():
    src = Source(channel="tg", chat_id="1", sender_id="u", chat_type=ChatType.DM)

    class EmitCid:
        def __init__(self, cid):
            self._cid = cid

        async def run(self, req, emit, drain) -> TurnOutcome:
            await emit(Text(content="x", conversation_id=self._cid))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False)

    events1, sink1 = _collector()
    lane1 = Lane(runner=EmitCid(None), pools=OriginPools(user=1, system=1), sink=sink1, conversation_id="tg:1")
    await lane1.submit(TurnRequest(origin=Origin.USER, source=src, text="x"))
    text1 = next(e for e in events1 if isinstance(e, Text))
    assert text1.conversation_id == "tg:1"  # stamped with the lane key

    events2, sink2 = _collector()
    lane2 = Lane(runner=EmitCid("explicit"), pools=OriginPools(user=1, system=1), sink=sink2, conversation_id="tg:1")
    await lane2.submit(TurnRequest(origin=Origin.USER, source=src, text="x"))
    text2 = next(e for e in events2 if isinstance(e, Text))
    assert text2.conversation_id == "explicit"  # explicit conversation_id not overwritten


async def test_worker_stamps_conversation_id_on_lifecycle_events():
    # Lifecycle events carry no source (never routed) but do carry conversation_id
    # — the key the hub correlates a stream by, e.g. to close it on TurnFailed.
    runner = SuccessRunner(TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False))
    events, sink = _collector()
    lane = Lane(runner=runner, pools=OriginPools(user=1, system=1), sink=sink, conversation_id="tg:7")
    await lane.submit(_req())
    started = next(e for e in events if isinstance(e, TurnStarted))
    ended = next(e for e in events if isinstance(e, TurnEnded))
    assert started.conversation_id == "tg:7"
    assert ended.conversation_id == "tg:7"


async def test_turn_failed_carries_conversation_id():
    runner = FailingRunner()
    events, sink = _collector()
    lane = Lane(runner=runner, pools=OriginPools(user=1, system=1), sink=sink, conversation_id="tg:9")
    await lane.submit(_req())
    failed = next(e for e in events if isinstance(e, TurnFailed))
    assert failed.conversation_id == "tg:9"


async def test_run_exception_yields_turn_failed_and_resolves_future():
    runner = FailingRunner()
    events, sink = _collector()
    lane = Lane(runner=runner, pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    result = await lane.submit(_req())
    assert result is None
    failed = next(e for e in events if isinstance(e, TurnFailed))
    assert failed.cancelled is False
    assert "boom" in failed.error


async def test_cancel_resolves_future_with_cancelled_terminal():
    runner = HangingRunner()
    events, sink = _collector()
    lane = Lane(runner=runner, pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    fut = lane.submit(_req())
    await runner.started.wait()  # the turn is genuinely running
    lane.cancel()
    result = await asyncio.wait_for(fut, timeout=1.0)  # must resolve, not hang
    assert result is None
    assert any(isinstance(e, TurnFailed) and e.cancelled for e in events)


async def test_cancel_after_completion_does_not_double_resolve():
    # The dangerous twin of cancel-resolves: a cancel arriving after the turn
    # already completed must not resolve the future a second time
    # (set_result twice -> InvalidStateError). cancel only touches run_task,
    # which is already done (a no-op), never the future.
    runner = SuccessRunner()
    events, sink = _collector()
    lane = Lane(runner=runner, pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    fut = lane.submit(_req())
    result = await fut  # turn completes, worker resolves exactly once
    lane.cancel()  # late cancel: run_task is None/done -> no-op, future untouched
    await asyncio.sleep(0)
    assert isinstance(result, TurnOutcome)  # succeeded -> resolved with its outcome
    assert fut.done() and fut.exception() is None


async def test_cancel_drains_queue_resolving_pending_as_cancelled():
    # cancel stops the running turn AND drains the queue: every queued turn's
    # future resolves (as cancelled) and none of them runs. The dropped count
    # feeds the "Stopped N" report.
    runner = RecordingHangRunner()
    events, sink = _collector()
    lane = Lane(runner=runner, pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    f1 = lane.submit(_req("a"))  # runs, hangs
    f2 = lane.submit(_req("b"))  # queued
    f3 = lane.submit(_req("c"))  # queued
    await runner.first_started.wait()
    stopped = lane.cancel()
    assert stopped == 3  # 1 running + 2 queued
    for fut in (f1, f2, f3):
        assert await asyncio.wait_for(fut, timeout=1.0) is None
    assert runner.ran == ["a"]  # the queued turns never ran


async def test_cancel_during_setup_window_stops_the_turn():
    # The setup window (after a turn leaves the queue, before its body runs) is
    # only observable with an awaiting sink. A cancel landing there must stop the
    # turn, not let it run to completion. (Earlier no-await fakes hid this race.)
    ran: list[str] = []
    at_started = asyncio.Event()
    release = asyncio.Event()

    async def sink(event) -> None:
        if isinstance(event, TurnStarted):
            at_started.set()
            await release.wait()

    class R:
        async def run(self, req, emit, drain) -> TurnOutcome:
            ran.append(req.text)
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False)

    lane = Lane(runner=R(), pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    fut = lane.submit(_req("X"))
    await at_started.wait()  # worker suspended mid-setup
    stopped = lane.cancel()
    release.set()
    assert await asyncio.wait_for(fut, timeout=1.0) is None
    assert stopped == 1  # cancel saw the in-flight turn
    assert ran == []  # and the turn never ran


async def test_cancel_before_turnstarted_emits_no_lifecycle():
    # A turn cancelled before it acquires the pool (hence before TurnStarted)
    # emits no lifecycle at all — no orphan TurnFailed — and still resolves,
    # consistent with a drained queued turn.
    ran: list[str] = []
    events, sink = _collector()
    pools = OriginPools(user=1, system=1)
    await pools.for_origin(Origin.USER).acquire()  # exhaust the user pool

    class R:
        async def run(self, req, emit, drain) -> TurnOutcome:
            ran.append(req.text)
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False)

    lane = Lane(runner=R(), pools=pools, sink=sink, conversation_id="c")
    fut = lane.submit(_req("X"))
    await asyncio.sleep(0.02)  # let the worker reach and block on pool acquire
    stopped = lane.cancel()
    assert await asyncio.wait_for(fut, timeout=1.0) is None
    assert stopped == 1
    assert ran == []
    assert events == []  # no TurnStarted, so no orphan TurnFailed either


async def test_worker_exits_when_queue_drains():
    runner = SuccessRunner()
    events, sink = _collector()
    lane = Lane(runner=runner, pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    await lane.submit(_req())
    await asyncio.sleep(0)  # let the worker observe the empty queue and exit
    assert lane._worker is None or lane._worker.done()


async def test_cancel_is_scoped_to_its_lane():
    r1, r2 = HangingRunner(), SuccessRunner()
    _, sink1 = _collector()
    _, sink2 = _collector()
    lane1 = Lane(runner=r1, pools=OriginPools(user=1, system=1), sink=sink1, conversation_id="c")
    lane2 = Lane(runner=r2, pools=OriginPools(user=1, system=1), sink=sink2, conversation_id="c")
    fut1 = lane1.submit(_req())
    fut2 = lane2.submit(_req())
    await r1.started.wait()
    lane1.cancel()  # cancelling lane1 must not disturb lane2
    await asyncio.wait_for(fut1, timeout=1.0)
    assert isinstance(await asyncio.wait_for(fut2, timeout=1.0), TurnOutcome)  # lane2 ran fine


async def test_worker_self_cancellation_drains_the_payload_leaving_no_zombie():
    # If the worker task itself is cancelled (process shutdown) while a turn
    # hangs, the worker cascades to the payload AND awaits its cleanup: the future
    # resolves (not a hang) and the payload's own TurnFailed(cancelled) is emitted
    # (its finally ran) — proving it is not a zombie still producing events.
    runner = HangingRunner()
    events, sink = _collector()
    lane = Lane(runner=runner, pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    fut = lane.submit(_req())
    await runner.started.wait()
    run_task = lane._run_task
    lane._worker.cancel()  # process-shutdown-style cancel of the worker itself
    result = await asyncio.wait_for(fut, timeout=1.0)
    assert result is None
    assert run_task.cancelled() or run_task.cancelling() > 0
    assert any(isinstance(e, TurnFailed) and e.cancelled for e in events)  # payload finally ran


async def test_scheduler_cancel_conversation_stops_running_and_returns_count():
    # /stop: cancel_conversation finds the lane and cancels its running
    # turn (+ drains its queue), returning the count; unknown conversation -> 0.
    from raven.spine.scheduler import Scheduler

    runner = HangingRunner()
    _events, sink = _collector()
    sched = Scheduler(runner, OriginPools(user=1, system=1), sink)
    handle = sched.submit(_req())  # no conversation -> cid = "t:c" (channel:chat_id)
    await runner.started.wait()

    assert sched.cancel_conversation("t:c") == 1  # running turn cancelled
    assert sched.cancel_conversation("nope") == 0  # unknown conversation -> no-op

    try:
        await handle.result()
    except (asyncio.CancelledError, Exception):
        pass
    await sched.shutdown(grace=0.0)


async def test_scheduler_has_inflight_tracks_running_turn():
    # has_inflight: True while a turn runs on the conversation's lane, False for
    # an idle / unknown conversation — the inbound gate reads it to decide
    # whether to inject a mid-turn message (BusyPolicy.INJECT) vs open a turn.
    from raven.spine.scheduler import Scheduler

    runner = HangingRunner()
    _events, sink = _collector()
    sched = Scheduler(runner, OriginPools(user=1, system=1), sink)
    assert sched.has_inflight("t:c") is False  # no lane yet
    handle = sched.submit(_req())  # no conversation -> cid = "t:c"
    await runner.started.wait()
    assert sched.has_inflight("t:c") is True  # turn running on the lane
    assert sched.has_inflight("nope") is False  # unknown conversation

    sched.cancel_conversation("t:c")
    try:
        await handle.result()
    except (asyncio.CancelledError, Exception):
        pass
    await sched.shutdown(grace=0.0)
