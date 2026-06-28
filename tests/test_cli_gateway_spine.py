import asyncio

import pytest

from raven.cli._gateway_spine import build_gateway
from raven.spine import (
    ChatType,
    MediaOut,
    Origin,
    Source,
    Text,
    TurnOutcome,
    TurnRequest,
    Usage,
)
from raven.spine.message import Media


def _src(channel="telegram", chat_id="c1") -> Source:
    return Source(channel=channel, chat_id=chat_id, sender_id="user", chat_type=ChatType.DM)


def _req(text="ping", *, channel="telegram", chat_id="c1", conversation="cron:1") -> TurnRequest:
    return TurnRequest(
        origin=Origin.CRON,
        source=_src(channel, chat_id),
        text=text,
        conversation=conversation,
    )


class _FakeChannel:
    def __init__(self, name="telegram") -> None:
        self.name = name
        self.sent: list[tuple[str, str, list[str] | None]] = []

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        self.sent.append((chat_id, content, media))


class _ReplyAgent:
    """run_turn emits scripted spine events (the gateway runner wires stream=False
    -> proactive replies are Text / MediaOut) and fills ``text_sink`` with the
    first Text's content, mirroring run_turn's observation copy."""

    def __init__(self, events=()) -> None:
        self._events = list(events)
        self.notify_count = 0  # _notify_turn_complete spy (the gateway sink fires it)

    def _notify_turn_complete(self) -> None:
        self.notify_count += 1

    async def run_turn(self, req, emit, drain, *, stream, usage_sink=None, text_sink=None) -> TurnOutcome:
        for ev in self._events:
            await emit(ev)
            if text_sink is not None and isinstance(ev, Text) and ev.content:
                text_sink["text"] = ev.content
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)


def test_build_gateway_requires_a_running_loop():
    # Scheduler pins its home loop at construction (submit must come from that
    # loop), so build_gateway must be called under a running loop — the gateway
    # command builds it inside run(), not in its sync prologue. This is a sync
    # test (no loop) on purpose: every async test runs under pytest's loop, so
    # only a sync call reproduces the "no running event loop" startup crash.
    with pytest.raises(RuntimeError):
        build_gateway(_ReplyAgent(), {})


async def test_build_gateway_registers_an_outlet_per_channel():
    channels = {"telegram": _FakeChannel("telegram"), "discord": _FakeChannel("discord")}
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(_ReplyAgent(), channels)
    try:
        # The hub routes by channel name; both channels must have an outlet registered.
        assert {"telegram", "discord"} <= set(hub._outlets)
    finally:
        await teardown()


async def test_build_gateway_defaults_to_canonical_pool_and_retry_sizes():
    scheduler, _hub, _rb, _sources, teardown = build_gateway(_ReplyAgent(), {})
    try:
        assert scheduler._pools._user._value == 4
        assert scheduler._pools._system._value == 2
        assert _hub._send_max_retries == 3
    finally:
        await teardown()


async def test_build_gateway_honors_configured_pool_and_retry_sizes():
    scheduler, hub, _rb, _sources, teardown = build_gateway(
        _ReplyAgent(), {}, user_pool=7, system_pool=3, send_max_retries=5
    )
    try:
        assert scheduler._pools._user._value == 7
        assert scheduler._pools._system._value == 3
        assert hub._send_max_retries == 5
    finally:
        await teardown()


async def test_proactive_reply_reaches_the_channel_via_outlet():
    # End-to-end: build_gateway -> submit (origin=CRON) -> run_turn emits Text ->
    # hub -> ChannelOutletAdapter -> channel.send.
    ch = _FakeChannel("telegram")
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(
        _ReplyAgent([Text(content="reminder!")]), {"telegram": ch}
    )
    try:
        await scheduler.submit(_req(channel="telegram", chat_id="c9")).result()
        await hub.wait_idle("telegram")
    finally:
        await teardown()

    assert len(ch.sent) == 1
    assert ch.sent[0][0] == "c9"          # chat_id (channel routing is by source.channel)
    assert ch.sent[0][1] == "reminder!"   # content


async def test_readback_captures_cron_reply_text():
    # The reachability leg: a CRON turn's reply text is captured into
    # readback_texts[conversation] (the submitter cannot pass run_turn's text_sink
    # itself — the capturing runner bridges it) so the cron handler can read it
    # back for its system event after result() resolves.
    ch = _FakeChannel("telegram")
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(
        _ReplyAgent([Text(content="done at 17:05")]), {"telegram": ch}
    )
    try:
        await scheduler.submit(_req(channel="telegram", conversation="cron:42")).result()
        await hub.wait_idle("telegram")
        assert readback_texts["cron:42"] == "done at 17:05"
    finally:
        await teardown()


async def test_readback_skips_non_readback_origin():
    # A delivery-only turn (origin=USER) is never stored — only its hub delivery
    # happens; storing it would leak in the long-running daemon (no one pops it).
    ch = _FakeChannel("telegram")
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(
        _ReplyAgent([Text(content="hello")]), {"telegram": ch}
    )
    user_req = TurnRequest(
        origin=Origin.USER, source=_src("telegram", "u1"), text="hi", conversation="telegram:u1"
    )
    try:
        await scheduler.submit(user_req).result()
        await hub.wait_idle("telegram")
        assert "telegram:u1" not in readback_texts  # USER turn not captured
        assert len(ch.sent) == 1  # but still delivered
    finally:
        await teardown()


async def test_readback_skips_heartbeat_origin():
    # heartbeat is delivery-only: its reply rides the hub like a user
    # reply, and nothing reads it back, so it must not be captured — storing it
    # would leak (no one pops "heartbeat"). HEARTBEAT is out of _READBACK_ORIGINS.
    ch = _FakeChannel("telegram")
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(
        _ReplyAgent([Text(content="tasks done")]), {"telegram": ch}
    )
    hb_req = TurnRequest(
        origin=Origin.HEARTBEAT, source=_src("telegram", "u1"), text="run tasks", conversation="heartbeat"
    )
    try:
        await scheduler.submit(hb_req).result()
        await hub.wait_idle("telegram")
        assert "heartbeat" not in readback_texts  # not captured (deliver-only)
        assert len(ch.sent) == 1  # but delivered via the hub
    finally:
        await teardown()


async def test_proactive_media_reply_sends_local_paths():
    ch = _FakeChannel("telegram")
    media = (Media(path="/tmp/chart.png", mime="image/png", kind="image"),)
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(
        _ReplyAgent([MediaOut(media=media)]), {"telegram": ch}
    )
    try:
        await scheduler.submit(_req(channel="telegram")).result()
        await hub.wait_idle("telegram")
    finally:
        await teardown()

    assert len(ch.sent) == 1 and ch.sent[0][2] == ["/tmp/chart.png"]


async def test_reply_to_unregistered_channel_is_dropped_not_raised():
    # The hub drops (warning, not raise) a deliverable whose source channel has no
    # outlet — so the gateway must register every channel a proactive source can target,
    # else its reply is silently lost.
    ch = _FakeChannel("telegram")
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(
        _ReplyAgent([Text(content="hi")]), {"telegram": ch}
    )
    try:
        # Submit a turn whose source channel ("discord") has no registered outlet.
        await scheduler.submit(_req(channel="discord", conversation="cron:2")).result()
        await hub.wait_idle("telegram")
    finally:
        await teardown()

    assert ch.sent == []  # dropped, and no exception propagated


# --- gateway sink lifecycle: on_turn_complete + TurnFailed error reply ---
# Restores the bus drainer's _dispatch finally(notify)/except(Sorry) side effects
# that the plain hub sink dropped. cancel-vs-error: Sorry only on a real failure,
# never on a /stop cancel (mirrors the bus CancelledError-no-Sorry path + TUI sink).


async def test_gateway_sink_notifies_on_turn_end():
    agent = _ReplyAgent([Text(content="hi")])
    ch = _FakeChannel("telegram")
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(agent, {"telegram": ch})
    try:
        await scheduler.submit(_req(channel="telegram")).result()
        await hub.wait_idle("telegram")
    finally:
        await teardown()
    assert agent.notify_count >= 1  # on_turn_complete fired (wake signal preserved)


async def test_gateway_sink_sends_error_reply_on_failure():
    class _BoomAgent(_ReplyAgent):
        async def run_turn(self, req, emit, drain, *, stream, usage_sink=None, text_sink=None):
            raise RuntimeError("boom")

    agent = _BoomAgent()
    ch = _FakeChannel("telegram")
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(agent, {"telegram": ch})
    try:
        try:
            await scheduler.submit(_req(channel="telegram", chat_id="c9")).result()
        except Exception:
            pass  # the failed turn's future may surface the error; the reply is the point
        await hub.wait_idle("telegram")
    finally:
        await teardown()
    # A non-cancelled failure delivers a user-visible error reply to the channel,
    # and still fires on_turn_complete (bus _dispatch except + finally parity).
    assert len(ch.sent) == 1 and ch.sent[0][1] == "Sorry, I encountered an error."
    assert ch.sent[0][0] == "c9"   # chat_id (channel routing is by source.channel)
    assert agent.notify_count >= 1


async def test_gateway_sink_no_error_reply_on_cancel():
    # /stop cancels a running turn -> TurnFailed(cancelled=True) -> notify but NO
    # "Sorry" (the bus path re-raises CancelledError without the error message).
    started = asyncio.Event()

    class _BlockingAgent(_ReplyAgent):
        async def run_turn(self, req, emit, drain, *, stream, usage_sink=None, text_sink=None):
            started.set()
            await asyncio.Event().wait()  # block until cancelled

    agent = _BlockingAgent()
    ch = _FakeChannel("telegram")
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(agent, {"telegram": ch})
    try:
        handle = scheduler.submit(_req(channel="telegram", conversation="telegram:u1"))
        await started.wait()
        scheduler.cancel_conversation("telegram:u1")
        try:
            await handle.result()
        except (asyncio.CancelledError, Exception):
            pass
        await hub.wait_idle("telegram")
    finally:
        await teardown()
    assert ch.sent == []  # no "Sorry" on a cancel
    assert agent.notify_count >= 1  # but the wake signal still fires


async def test_build_gateway_teardown_leaves_no_pending_tasks():
    baseline = asyncio.all_tasks()
    ch = _FakeChannel("telegram")
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(
        _ReplyAgent([Text(content="hi")]), {"telegram": ch}
    )
    await scheduler.submit(_req(channel="telegram")).result()
    await hub.wait_idle("telegram")
    spawned = asyncio.all_tasks() - baseline - {asyncio.current_task()}
    assert any(not t.done() for t in spawned)  # live spine tasks exist before teardown
    await teardown()
    assert all(t.done() for t in spawned)  # teardown stopped every one
