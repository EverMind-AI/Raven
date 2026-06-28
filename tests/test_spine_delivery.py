import asyncio
import dataclasses

import pytest
from loguru import logger

from raven.spine import (
    ChatType,
    Media,
    MediaOut,
    Notice,
    NoticeKind,
    Reasoning,
    Source,
    StreamDelta,
    Text,
    ToolEvent,
    ToolPhase,
)
from raven.spine import delivery as delivery_mod
from raven.spine.delivery import Capabilities, DeliveryHub, Outlet, SupportsStreaming


def test_capabilities_default_to_all_off():
    caps = Capabilities()
    assert caps.interactive_login is False
    assert caps.streaming is False


def test_capabilities_is_frozen():
    caps = Capabilities(streaming=True)
    assert caps.streaming is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.streaming = False


def test_supports_streaming_is_a_runtime_checkable_structural_protocol():
    class WithStreaming:
        async def send_stream_chunk(self, chat_id, stream_id, delta, *, done=False):
            pass

    class WithoutStreaming:
        pass

    assert isinstance(WithStreaming(), SupportsStreaming)
    assert not isinstance(WithoutStreaming(), SupportsStreaming)


def _src(channel: str) -> Source:
    return Source(channel=channel, chat_id="c", sender_id="u", chat_type=ChatType.DM)


class FakeOutlet:
    """Records what it delivers; raises on the first ``fail_times`` calls to
    exercise retry. A normal return is the eat / success path."""

    def __init__(self, name: str, *, fail_times: int = 0) -> None:
        self.name = name
        self.capabilities = Capabilities()
        self.received: list = []
        self._fail_times = fail_times
        self.calls = 0

    async def deliver(self, out) -> None:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transport down")
        self.received.append(out)


def test_fake_outlet_satisfies_the_outlet_protocol():
    assert isinstance(FakeOutlet("tg"), Outlet)
    assert not isinstance(object(), Outlet)


async def _settle(predicate, *, tries: int = 2000) -> None:
    # Delivery is asynchronous (the per-outlet worker), so wait for the worker to
    # act by yielding the loop until the condition holds.
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never reached")


@pytest.fixture
async def hub():
    h = DeliveryHub()
    yield h
    await h.aclose()  # cancel resident outlet workers so none leak past the test


class GatedOutlet:
    """Blocks in deliver until its gate is set — for testing per-outlet isolation."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.capabilities = Capabilities()
        self.received: list = []
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def deliver(self, out) -> None:
        self.entered.set()
        await self.gate.wait()
        self.received.append(out)


_DELIVERABLES = [
    Text(content="hi", source=_src("tg")),
    MediaOut(media=(Media(path="/tmp/a.jpg", mime="image/jpeg", kind="image"),), source=_src("tg")),
    ToolEvent(phase=ToolPhase.START, tool_call_id="t1", name="grep", source=_src("tg")),
    Reasoning(content="r", source=_src("tg")),
    Notice(kind=NoticeKind.PROGRESS, source=_src("tg")),
]


@pytest.mark.parametrize("out", _DELIVERABLES, ids=lambda o: type(o).__name__)
async def test_dispatch_routes_every_deliverable_to_its_channel_outlet(hub, out):
    outlet = FakeOutlet("tg")
    hub.register(outlet)
    await hub.dispatch(out)
    await _settle(lambda: outlet.received == [out])
    assert outlet.calls == 1  # eat / success path does not retry


async def test_dispatch_keeps_same_channel_order(hub):
    outlet = FakeOutlet("tg")
    hub.register(outlet)
    for n in ("a", "b", "c"):
        await hub.dispatch(Text(content=n, source=_src("tg")))
    await _settle(lambda: len(outlet.received) == 3)
    assert [m.content for m in outlet.received] == ["a", "b", "c"]  # per-outlet FIFO


async def test_per_outlet_serial_holds_across_a_retry(hub, monkeypatch):
    monkeypatch.setattr(delivery_mod, "_RETRY_BASE_DELAY", 0)

    class FailsFirstEvent:
        name = "tg"
        capabilities = Capabilities()

        def __init__(self) -> None:
            self.received: list = []
            self._fail_left = 2  # the first event's first two attempts raise

        async def deliver(self, out) -> None:
            if out.content == "a" and self._fail_left > 0:
                self._fail_left -= 1
                raise RuntimeError("transport down")
            self.received.append(out)

    outlet = FailsFirstEvent()
    hub.register(outlet)
    await hub.dispatch(Text(content="a", source=_src("tg")))
    await hub.dispatch(Text(content="b", source=_src("tg")))
    await _settle(lambda: len(outlet.received) == 2)
    assert [m.content for m in outlet.received] == ["a", "b"]  # b waits behind a's retries


async def test_send_max_retries_is_configurable(monkeypatch):
    monkeypatch.setattr(delivery_mod, "_RETRY_BASE_DELAY", 0)

    class AlwaysFails:
        name = "tg"
        capabilities = Capabilities()

        def __init__(self) -> None:
            self.attempts = 0

        async def deliver(self, out) -> None:
            self.attempts += 1
            raise RuntimeError("transport down")

    hub = DeliveryHub(send_max_retries=1)
    outlet = AlwaysFails()
    hub.register(outlet)
    try:
        await hub.dispatch(Text(content="a", source=_src("tg")))
        await hub.wait_idle("tg")
        assert outlet.attempts == 2  # 1 initial + 1 retry, then dropped
    finally:
        await hub.aclose()


async def test_dispatch_routes_by_source_channel_across_outlets(hub):
    tg, wx = FakeOutlet("tg"), FakeOutlet("wx")
    hub.register(tg)
    hub.register(wx)
    await hub.dispatch(Text(content="a", source=_src("tg")))
    await hub.dispatch(Text(content="b", source=_src("wx")))
    await _settle(lambda: tg.received and wx.received)
    assert [m.content for m in tg.received] == ["a"]
    assert [m.content for m in wx.received] == ["b"]


async def test_a_slow_outlet_does_not_block_another(hub):
    slow = GatedOutlet("slow")
    fast = FakeOutlet("fast")
    hub.register(slow)
    hub.register(fast)
    await hub.dispatch(Text(content="s", source=_src("slow")))  # slow blocks in deliver
    await hub.dispatch(Text(content="f", source=_src("fast")))
    await _settle(lambda: fast.received and fast.received[0].content == "f")
    assert not slow.received  # cross-outlet: fast delivered while slow is stuck
    slow.gate.set()
    await _settle(lambda: slow.received and slow.received[0].content == "s")


async def test_per_outlet_backpressure_isolates_channels(hub, monkeypatch):
    monkeypatch.setattr(delivery_mod, "_OUTLET_QUEUE_MAXSIZE", 1)
    slow = GatedOutlet("slow")
    fast = FakeOutlet("fast")
    hub.register(slow)
    hub.register(fast)
    await hub.dispatch(Text(content="s1", source=_src("slow")))  # worker takes it, blocks in deliver
    await slow.entered.wait()
    await hub.dispatch(Text(content="s2", source=_src("slow")))  # fills the maxsize-1 queue
    blocked = asyncio.ensure_future(hub.dispatch(Text(content="s3", source=_src("slow"))))
    await asyncio.sleep(0)
    assert not blocked.done()  # queue full -> this channel's sender is backpressured
    await hub.dispatch(Text(content="f", source=_src("fast")))  # other channel unaffected
    await _settle(lambda: fast.received and fast.received[0].content == "f")
    slow.gate.set()
    await blocked  # released once the slow worker drains


async def test_a_dead_worker_self_heals_on_next_enqueue(hub):
    outlet = FakeOutlet("tg")
    hub.register(outlet)
    await hub.dispatch(Text(content="a", source=_src("tg")))
    await _settle(lambda: len(outlet.received) == 1)
    dead = hub._workers["tg"]
    dead.cancel()  # simulate the resident worker dying
    await asyncio.gather(dead, return_exceptions=True)
    await hub.dispatch(Text(content="b", source=_src("tg")))  # done-check restarts it
    await _settle(lambda: len(outlet.received) == 2)
    assert [m.content for m in outlet.received] == ["a", "b"]
    assert hub._workers["tg"] is not dead  # a fresh worker took over


async def test_a_raising_deliver_is_retried_then_succeeds(hub, monkeypatch):
    monkeypatch.setattr(delivery_mod, "_RETRY_BASE_DELAY", 0)
    outlet = FakeOutlet("tg", fail_times=2)
    hub.register(outlet)
    out = Text(content="hi", source=_src("tg"))
    await hub.dispatch(out)
    await _settle(lambda: outlet.received == [out])
    assert outlet.calls == 3  # two failures, third sticks


async def test_exhausted_retries_log_an_error_and_drop(hub, monkeypatch):
    monkeypatch.setattr(delivery_mod, "_RETRY_BASE_DELAY", 0)
    outlet = FakeOutlet("tg", fail_times=99)
    hub.register(outlet)
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="ERROR", format="{message}")
    try:
        await hub.dispatch(Text(content="hi", source=_src("tg")))  # delivered async, then dropped
        await _settle(lambda: any("delivery failed" in line for line in lines))
    finally:
        logger.remove(sink_id)
    assert outlet.calls == delivery_mod._SEND_MAX_RETRIES + 1  # initial + retries
    assert not outlet.received  # dropped after exhaustion
    err = next(line for line in lines if "delivery failed" in line)
    assert "tg" in err and "Text" in err and "transport down" in err  # channel + event + reason


async def test_retry_backoff_doubles_from_the_base_delay(hub, monkeypatch):
    real_sleep = asyncio.sleep  # keep a real yield; the fake replaces asyncio.sleep globally
    delays: list[float] = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(delivery_mod.asyncio, "sleep", fake_sleep)
    outlet = FakeOutlet("tg", fail_times=99)
    hub.register(outlet)
    await hub.dispatch(Text(content="hi", source=_src("tg")))
    for _ in range(50):
        if len(delays) == 3:
            break
        await real_sleep(0)
    base = delivery_mod._RETRY_BASE_DELAY
    assert delays == [base, base * 2, base * 4]  # one sleep per retry, exponential
    assert outlet.calls == delivery_mod._SEND_MAX_RETRIES + 1  # no sleep after the final attempt


async def test_an_unreachable_channel_warns_and_drops(hub):
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="WARNING", format="{message}")
    try:
        await hub.dispatch(Text(content="hi", source=_src("ghost")))  # no outlet registered
    finally:
        logger.remove(sink_id)
    warn = next(line for line in lines if "no outlet" in line)
    assert "ghost" in warn


async def test_post_routes_like_dispatch(hub):
    outlet = FakeOutlet("tg")
    hub.register(outlet)
    out = Text(content="menu", source=_src("tg"))
    await hub.post(out)
    await _settle(lambda: outlet.received == [out])


async def test_a_sourceless_deliverable_fails_loud(hub):
    hub.register(FakeOutlet("tg"))
    with pytest.raises(ValueError, match="no source"):
        await hub.dispatch(Text(content="hi"))


class FakeStreamingOutlet:
    """A streaming-capable outlet: deliver() consumes the stream instead of
    delivering Text, send_stream_chunk records each chunk."""

    def __init__(self, name: str = "tg") -> None:
        self.name = name
        self.capabilities = Capabilities(streaming=True)
        self.received: list = []
        self.chunks: list[tuple] = []

    async def deliver(self, out) -> None:
        self.received.append(out)

    async def send_stream_chunk(self, chat_id, stream_id, delta, *, done=False) -> None:
        self.chunks.append((chat_id, stream_id, delta, done))


async def test_streaming_outlet_gets_chunks_then_a_done_close(hub):
    outlet = FakeStreamingOutlet("tg")
    hub.register(outlet)
    src = _src("tg")  # chat_id == "c"
    await hub.dispatch(StreamDelta(delta="he", source=src, conversation_id="tg:c"))
    await hub.dispatch(StreamDelta(delta="llo", source=src, conversation_id="tg:c"))
    await hub.close_stream("tg:c")  # marker is queued after the deltas
    await hub.wait_idle("tg")
    assert outlet.chunks == [
        ("c", "tg:c", "he", False),
        ("c", "tg:c", "llo", False),
        ("c", "tg:c", "", True),  # close lands after every delta (S1: done never precedes a chunk)
    ]


async def test_close_stream_is_a_noop_for_an_unopened_conversation(hub):
    outlet = FakeStreamingOutlet("tg")
    hub.register(outlet)
    await hub.close_stream("tg:never")  # no StreamDelta ever -> no channel recorded -> no-op
    assert outlet.chunks == []


async def test_non_streaming_outlet_eats_stream_delta(hub):
    outlet = FakeOutlet("tg")  # not SupportsStreaming
    hub.register(outlet)
    await hub.dispatch(StreamDelta(delta="x", source=_src("tg"), conversation_id="tg:c"))
    await hub.close_stream("tg:c")
    await hub.wait_idle("tg")
    assert outlet.received == []  # eaten, not delivered; close-marker no-ops (stream never opened)


async def test_a_stream_reopens_cleanly_for_the_next_turn(hub):
    # The same conversation streams across sequential turns; each close must reset
    # the open-stream state so the next turn opens a fresh stream (not stack onto
    # the closed one) — both the worker's table and the routing entry clear.
    outlet = FakeStreamingOutlet("tg")
    hub.register(outlet)
    src = _src("tg")  # chat_id == "c"
    for delta in ("a", "b"):
        await hub.dispatch(StreamDelta(delta=delta, source=src, conversation_id="tg:c"))
        await hub.close_stream("tg:c")
        await hub.wait_idle("tg")
    assert outlet.chunks == [
        ("c", "tg:c", "a", False), ("c", "tg:c", "", True),   # turn 1
        ("c", "tg:c", "b", False), ("c", "tg:c", "", True),   # turn 2, reopened cleanly
    ]
    assert "tg:c" not in hub._open_streams  # close left no open-stream state behind
    assert "tg:c" not in hub._stream_channel  # nor a routing entry


async def test_aclose_cancels_workers(hub):
    outlet = FakeOutlet("tg")
    hub.register(outlet)
    await hub.dispatch(Text(content="a", source=_src("tg")))
    await _settle(lambda: len(outlet.received) == 1)  # worker is now back blocking on get
    worker = hub._workers["tg"]
    await hub.aclose()
    assert worker.cancelled()  # in-flight/idle worker is cancelled, not awaited to finish
    assert hub._workers == {}


async def test_drain_drops_queued_events_and_counts_them(hub):
    slow = GatedOutlet("slow")
    hub.register(slow)
    await hub.dispatch(Text(content="s1", source=_src("slow")))  # worker takes it, blocks
    await slow.entered.wait()
    await hub.dispatch(Text(content="s2", source=_src("slow")))  # stays queued
    await hub.dispatch(Text(content="s3", source=_src("slow")))  # stays queued
    assert hub.drain() == 2  # the two still-queued events dropped; the in-flight one is not
    assert hub.drain() == 0  # idempotent once empty


async def test_wait_idle_returns_at_once_when_nothing_queued(hub):
    hub.register(FakeOutlet("tg"))
    await hub.wait_idle("tg")  # registered but never dispatched -> no queue -> idle
    await hub.wait_idle("ghost")  # no outlet, no queue -> idle


async def test_wait_idle_blocks_until_in_flight_delivery_completes(hub):
    slow = GatedOutlet("slow")
    hub.register(slow)
    await hub.dispatch(Text(content="s", source=_src("slow")))
    await slow.entered.wait()  # worker dequeued and is blocked in deliver (not task_done)
    waiter = asyncio.ensure_future(hub.wait_idle("slow"))
    await asyncio.sleep(0)
    assert not waiter.done()  # an in-flight item keeps the channel non-idle
    slow.gate.set()
    await waiter  # delivery completes -> task_done -> idle


async def test_wait_idle_returns_only_after_all_queued_delivered(hub):
    outlet = FakeOutlet("tg")
    hub.register(outlet)
    for n in ("a", "b", "c"):
        await hub.dispatch(Text(content=n, source=_src("tg")))
    await hub.wait_idle("tg")
    assert [m.content for m in outlet.received] == ["a", "b", "c"]  # all rendered before wait returns


async def test_wait_idle_returns_even_when_delivery_is_dropped(hub, monkeypatch):
    monkeypatch.setattr(delivery_mod, "_RETRY_BASE_DELAY", 0)
    outlet = FakeOutlet("tg", fail_times=99)  # always raises -> retries exhausted -> dropped
    hub.register(outlet)
    await hub.dispatch(Text(content="x", source=_src("tg")))
    await hub.wait_idle("tg")  # task_done() in finally -> join() does not hang on the dropped item
    assert outlet.calls == delivery_mod._SEND_MAX_RETRIES + 1


async def test_drain_keeps_wait_idle_consistent(hub):
    slow = GatedOutlet("slow")
    hub.register(slow)
    await hub.dispatch(Text(content="s1", source=_src("slow")))  # becomes in-flight
    await slow.entered.wait()
    await hub.dispatch(Text(content="s2", source=_src("slow")))  # stays queued
    assert hub.drain() == 1  # s2 dropped (with task_done)
    slow.gate.set()  # s1 completes -> task_done
    await hub.wait_idle("slow")  # both the in-flight and the drained item accounted -> returns
