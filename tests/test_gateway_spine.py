"""The gateway spine: what ``build_gateway`` wires, and what a turn meets on it.

``raven/gateway/spine.py`` assembles the Scheduler, the delivery sink and the
channel registry into the object every entrance submits turns to. These pin the
assembly (it needs a running loop), the sink's lifecycle (``on_turn_complete``,
and the error reply a ``TurnFailed`` becomes), and the route a turn takes back to
a human -- the asker and question autofill, which arm on the user origin only.
"""

import asyncio

import pytest

from raven.acp_client.asker import ApprovalViaAsk, AskViaTool, current_ask, current_autofill
from raven.acp_client.resolver import Autofill
from raven.agent.loop.recovery import EMPTY_REPLY_FAILURE
from raven.agent.tools.ask_user import AskUserTool
from raven.config.raven import SubagentQuestionsConfig
from raven.gateway.spine import _DELIVERY_GRACE, _make_gateway_sink, build_gateway
from raven.providers.base import bound_llm_detail
from raven.spine import (
    AnswerlessTurnError,
    ChatType,
    MediaOut,
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
from raven.spine.delivery import Capabilities, DeliveryHub
from raven.spine.message import Media


def _src(channel="telegram", chat_id="c1") -> Source:
    return Source(channel=channel, chat_id=chat_id, sender_id="user", chat_type=ChatType.DM)


def _req(
    text="ping", *, channel="telegram", chat_id="c1", conversation="cron:1", origin: Origin = Origin.CRON
) -> TurnRequest:
    return TurnRequest(
        origin=origin,
        source=_src(channel, chat_id),
        text=text,
        conversation=conversation,
    )


def _user_req(chat_id="c7") -> TurnRequest:
    return _req(channel="telegram", chat_id=chat_id, conversation=f"telegram:{chat_id}", origin=Origin.USER)


class _FakeChannel:
    def __init__(self, name="telegram", *, file_attachments=False) -> None:
        self.name = name
        self.capabilities = Capabilities(file_attachments=file_attachments)
        self.sent: list[tuple[str, str, list[str] | None]] = []

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        self.sent.append((chat_id, content, media))


class _EndsTurns(_FakeChannel):
    def __init__(self, name="telegram") -> None:
        super().__init__(name)
        self.ended: list[str] = []

    async def end_turn(self, chat_id: str) -> None:
        self.ended.append(chat_id)


class _ReplyAgent:
    """run_turn emits scripted spine events (the gateway runner wires stream=False
    -> proactive replies are Text / MediaOut) and fills ``text_sink`` with the
    first Text's content, mirroring run_turn's observation copy."""

    def __init__(self, events=()) -> None:
        self._events = list(events)
        self.notify_count = 0  # notify_turn_complete spy (the gateway sink fires it)

    def notify_turn_complete(self) -> None:
        self.notify_count += 1

    async def run_turn(self, req, emit, drain, *, stream, usage_sink=None, text_sink=None) -> TurnOutcome:
        for ev in self._events:
            await emit(ev)
            if text_sink is not None and isinstance(ev, Text) and ev.content:
                text_sink["text"] = ev.content
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)


class _SilentAgent(_ReplyAgent):
    """A turn that ended without a reply: nothing emitted, nothing explicit."""

    def __init__(self, *, silent: bool = True) -> None:
        super().__init__()
        self._silent = silent

    async def run_turn(self, req, emit, drain, *, stream, usage_sink=None, text_sink=None) -> TurnOutcome:
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False, silent=self._silent)


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


async def test_teardown_shuts_down_with_the_configured_grace(monkeypatch):
    scheduler, _hub, _rb, _sources, teardown = build_gateway(_ReplyAgent(), {}, shutdown_grace=2.5)
    seen: list[float] = []
    original = type(scheduler).shutdown

    async def spy(self, grace: float) -> None:
        seen.append(grace)
        await original(self, grace)

    monkeypatch.setattr(type(scheduler), "shutdown", spy)
    await teardown()
    assert seen == [2.5]


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
    assert ch.sent[0][0] == "c9"  # chat_id (channel routing is by source.channel)
    assert ch.sent[0][1] == "reminder!"  # content


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
    user_req = TurnRequest(origin=Origin.USER, source=_src("telegram", "u1"), text="hi", conversation="telegram:u1")
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
    ch = _FakeChannel("telegram", file_attachments=True)
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
    assert ch.sent[0][0] == "c9"  # chat_id (channel routing is by source.channel)
    assert agent.notify_count >= 1


async def _reply_to_a_failed_turn(
    exc: BaseException, *, origin: Origin = Origin.CRON, channel: _FakeChannel | None = None
) -> list[str]:
    """What a channel reader is sent when ``run_turn`` of an ``origin`` turn ends with ``exc``."""

    class _GaveUpAgent(_ReplyAgent):
        async def run_turn(self, req, emit, drain, *, stream, usage_sink=None, text_sink=None):
            raise exc

    ch = channel or _FakeChannel("telegram")
    scheduler, hub, _readback, _sources, teardown = build_gateway(_GaveUpAgent(), {"telegram": ch})
    try:
        try:
            await scheduler.submit(
                _req(channel="telegram", chat_id="c9", conversation=f"{origin.value}:1", origin=origin)
            ).result()
        except Exception:
            pass
        await hub.wait_idle("telegram")
    finally:
        await teardown()
    return [sent[1] for sent in ch.sent]


async def test_gateway_sink_tells_the_channel_the_category_not_the_vendors_body():
    """A model call's failure reaches a chat as its category and endpoint. The
    vendor's own account is the operator's diagnostic: an auth body carries a
    masked key and the account URL it was rejected for, and a group chat is not
    where either belongs."""
    vendor_body = (
        "AuthenticationError: OpenrouterException - No auth credentials found for key sk-or-v1-a1b2...ef90; "
        "see https://openrouter.ai/settings/keys"
    )
    sent = await _reply_to_a_failed_turn(AnswerlessTurnError(f"Error calling LLM (auth@openrouter): {vendor_body}"))

    assert sent == [
        "The provider rejected the credentials (openrouter). The runtime log has the provider's own account."
    ]
    assert "sk-or-v1" not in sent[0]
    assert "openrouter.ai/settings" not in sent[0]


async def test_gateway_sink_quotes_a_failure_the_runner_worded_itself():
    """Not every answerless turn is a model call's. A report the runner wrote
    for a reader is quoted as it stands -- bounded, because nothing upstream
    promises it is short -- rather than flattened to the canned sentence."""
    sent = await _reply_to_a_failed_turn(AnswerlessTurnError("The model returned no content on 4 attempts."))
    assert sent == ["The model returned no content on 4 attempts."]

    long_sent = await _reply_to_a_failed_turn(AnswerlessTurnError("y" * 5000))
    from raven.providers.base import LLM_ERROR_DETAIL_MAX

    assert len(long_sent[0]) == LLM_ERROR_DETAIL_MAX and long_sent[0].endswith("...")


async def test_gateway_sink_keeps_the_canned_reply_for_a_crash():
    """A crash's message names hosts, paths and whatever an SDK put in it, and
    nobody wrote it for a chat reader; it stays the one sentence channels have
    always got."""
    sent = await _reply_to_a_failed_turn(RuntimeError("connect /Users/ops/.raven/run/agent.sock: no such file"))
    assert sent == ["Sorry, I encountered an error."]
    assert "/Users/ops" not in sent[0]


# --- a scheduled run that got no output from the model is not reported to a chat ---


async def test_a_cron_run_that_got_no_output_is_not_delivered():
    """A scheduled run has no reader waiting on it, and its job records the
    failure. It started no typing indicator either, so there is none to end."""
    ch = _EndsTurns("telegram")
    sent = await _reply_to_a_failed_turn(AnswerlessTurnError(EMPTY_REPLY_FAILURE, no_output=True), channel=ch)

    assert sent == []
    assert ch.ended == []


async def test_a_user_turn_that_got_no_output_is_told_so():
    """A person is waiting on the answer, so the failure is what they get, and
    sending it is what stops the typing indicator."""
    ch = _EndsTurns("telegram")
    sent = await _reply_to_a_failed_turn(
        AnswerlessTurnError(EMPTY_REPLY_FAILURE, no_output=True), origin=Origin.USER, channel=ch
    )

    assert sent == [bound_llm_detail(EMPTY_REPLY_FAILURE)]
    assert ch.ended == []


@pytest.mark.parametrize("origin", [Origin.HEARTBEAT, Origin.SENTINEL, Origin.SUBAGENT])
async def test_only_a_cron_runs_no_output_failure_is_withheld(origin):
    """The other turns the runtime opens keep the report they always had."""
    sent = await _reply_to_a_failed_turn(AnswerlessTurnError(EMPTY_REPLY_FAILURE, no_output=True), origin=origin)

    assert sent == [bound_llm_detail(EMPTY_REPLY_FAILURE)]


class _RecordingHub(DeliveryHub):
    """Keeps what the sink dispatches instead of queueing it for an outlet."""

    def __init__(self) -> None:
        super().__init__()
        self.dispatched: list[Text] = []

    async def dispatch(self, out) -> None:
        self.dispatched.append(out)


def _no_output_failure(conversation_id: str, turn_id: str) -> TurnFailed:
    return TurnFailed(
        error=EMPTY_REPLY_FAILURE,
        cancelled=False,
        conversation_id=conversation_id,
        turn_id=turn_id,
        reported=True,
        no_output=True,
    )


async def test_a_no_output_failure_is_judged_by_its_own_turn():
    """The gateway runs a cron run and a person's turn at the same time, each in
    its own pool: the cron run's failure is withheld, and the same failure on the
    turn beside it is not."""
    hub = _RecordingHub()
    sources = {"cron:1": _src(chat_id="c7"), "telegram:u1": _src(chat_id="u1")}
    sink = _make_gateway_sink(hub, _ReplyAgent(), sources)

    await sink(TurnStarted(origin=Origin.CRON, conversation_id="cron:1", turn_id="t-cron"))
    await sink(TurnStarted(origin=Origin.USER, conversation_id="telegram:u1", turn_id="t-user"))
    await sink(_no_output_failure("telegram:u1", "t-user"))
    await sink(_no_output_failure("cron:1", "t-cron"))

    told = [(out.source.chat_id, out.content) for out in hub.dispatched]
    assert told == [("u1", bound_llm_detail(EMPTY_REPLY_FAILURE))]


def _finish_of(how: str, conversation_id: str, turn_id: str) -> TurnEnded | TurnFailed:
    """The three ways a turn leaves the sink: answered, failed, cancelled."""
    if how == "ended":
        return TurnEnded(
            usage=Usage(0, 0, 0),
            latency_ms=0.0,
            explicit_reply=True,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
    return TurnFailed(
        error="boom" if how == "failed" else "cancelled",
        cancelled=how == "cancelled",
        conversation_id=conversation_id,
        turn_id=turn_id,
    )


def _silent_end(conversation_id: str, turn_id: str) -> TurnEnded:
    return TurnEnded(
        usage=Usage(0, 0, 0),
        latency_ms=0.0,
        explicit_reply=False,
        conversation_id=conversation_id,
        turn_id=turn_id,
    )


@pytest.mark.parametrize("how", ["ended", "failed", "cancelled"])
async def test_a_finished_cron_turn_is_forgotten(how):
    """The gateway runs for weeks, so the sink holds a turn's origin only while
    it runs. The id is reused here to see whether it is still held: a no-output
    failure on a turn that is not a cron run is told."""
    hub = _RecordingHub()
    sources = {"cron:1": _src(chat_id="c7")}
    sink = _make_gateway_sink(hub, _ReplyAgent(), sources)
    await sink(TurnStarted(origin=Origin.CRON, conversation_id="cron:1", turn_id="t1"))
    await sink(_finish_of(how, "cron:1", "t1"))
    told_before = len(hub.dispatched)

    sources["cron:1"] = _src(chat_id="c7")
    await sink(_no_output_failure("cron:1", "t1"))

    assert len(hub.dispatched) == told_before + 1


def test_a_failed_turn_reply_reads_by_what_the_failure_was():
    """A canonical model-call failure is told as its category; a runner's own
    report is quoted, bounded; a crash, or a report with no words in it, gets
    the one sentence channels have always got."""
    from raven.gateway.spine import _TURN_FAILED_REPLY, _failed_turn_reply

    told = _failed_turn_reply(
        "Error calling LLM (auth@openrouter): Incorrect API key sk-proj-AbCd****xyz9", reported=True
    )
    assert "sk-proj" not in told and "openrouter" in told
    assert _failed_turn_reply("The model returned no content on 4 attempts.", reported=True) == (
        "The model returned no content on 4 attempts."
    )
    assert len(_failed_turn_reply("x" * 5000, reported=True)) <= 205
    assert _failed_turn_reply("ValueError: boom", reported=False) == _TURN_FAILED_REPLY
    assert _failed_turn_reply("", reported=True) == _TURN_FAILED_REPLY
    assert _failed_turn_reply("   ", reported=True) == _TURN_FAILED_REPLY


async def test_a_failed_turn_clears_the_readback_its_last_run_left():
    """A read-back conversation stores its reply on success only. Without a
    clear on the way out, the next turn on that conversation failing would let
    its submitter pop the previous run's text and record it as this run's."""
    ch = _FakeChannel("telegram")
    agent = _ReplyAgent([Text(content="done at 17:05")])
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(agent, {"telegram": ch})
    try:
        await scheduler.submit(_req(channel="telegram", conversation="cron:42")).result()
        assert readback_texts["cron:42"] == "done at 17:05"

        async def _fail(req, emit, drain, *, stream, usage_sink=None, text_sink=None):
            raise AnswerlessTurnError("Error calling LLM (server@openrouter): 503")

        agent.run_turn = _fail
        try:
            await scheduler.submit(_req(channel="telegram", conversation="cron:42")).result()
        except Exception:
            pass
        assert "cron:42" not in readback_texts
    finally:
        await teardown()


async def test_gateway_sink_tells_the_channel_when_a_reload_cut_the_turn():
    # The same cancel, but during a generation swap: the user did not ask for
    # it, so the channel gets one sentence instead of silence.
    started = asyncio.Event()

    class _BlockingAgent(_ReplyAgent):
        async def run_turn(self, req, emit, drain, *, stream, usage_sink=None, text_sink=None):
            started.set()
            await asyncio.Event().wait()

    agent = _BlockingAgent()
    ch = _FakeChannel("telegram")
    scheduler, hub, readback_texts, _sources, teardown = build_gateway(
        agent, {"telegram": ch}, cut_by_reload=lambda: True
    )
    try:
        handle = scheduler.submit(_req(channel="telegram", conversation="telegram:u1"))
        await started.wait()
        assert scheduler.has_running()
        scheduler.cancel_conversation("telegram:u1")
        try:
            await handle.result()
        except (asyncio.CancelledError, Exception):
            pass
        await hub.wait_idle("telegram")
    finally:
        await teardown()
    assert len(ch.sent) == 1 and "reload" in ch.sent[0][1]
    assert not scheduler.has_running()


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


# --- the turn's route to a human: asker + question autofill, on the USER origin only ---


class _AskingAgent(_ReplyAgent):
    """A loop carrying a real ask_user tool, recording what its turn bound.

    Read inside run_turn: start_ask_turn writes ContextVars on the lane task, so
    a read from the test's own context would see nothing however it is wired.
    """

    def __init__(self) -> None:
        super().__init__([Text(content="hi")])
        self.tools = {"ask_user": AskUserTool()}
        self.subagent_questions_config = SubagentQuestionsConfig()
        self.bound: dict = {}

    async def run_turn(self, req, emit, drain, **kwargs) -> TurnOutcome:
        from raven.permissions.turn import current_turn

        self.bound = {"ask": current_ask(), "autofill": current_autofill(), "permission": current_turn()}
        return await super().run_turn(req, emit, drain, **kwargs)


async def test_gateway_user_turn_binds_the_asker_and_the_autofill():
    # A channel user can answer: gateway_commands wires a QuestionBroker onto the
    # ask_user tool, so an ACP sub-agent's question goes out as a channel message
    # and its reply routes back. Unbound, that question is declined instead.
    agent = _AskingAgent()
    scheduler, _hub, _rb, _sources, teardown = build_gateway(agent, {"telegram": _FakeChannel("telegram")})
    user_req = TurnRequest(origin=Origin.USER, source=_src("telegram", "u1"), text="hi", conversation="telegram:u1")
    try:
        await scheduler.submit(user_req).result()
    finally:
        await teardown()
    assert isinstance(agent.bound["ask"][0], AskViaTool)
    assert agent.bound["ask"][1] == "telegram:u1"
    assert isinstance(agent.bound["autofill"], Autofill)
    # An approval rides the same round-trip a question does: the channel user
    # who can answer ask_user can also click through the ask tier.
    assert isinstance(agent.bound["permission"].responder, ApprovalViaAsk)
    assert agent.bound["permission"].conversation_id == "telegram:u1"


async def test_gateway_background_turn_binds_neither():
    # A cron turn has no reader, so its sub-agents' questions must decline rather
    # than wait on nobody -- and nothing may be answered on the user's behalf when
    # the user is not there. Bound to None, not merely left unset.
    agent = _AskingAgent()
    scheduler, _hub, _rb, _sources, teardown = build_gateway(agent, {"telegram": _FakeChannel("telegram")})
    try:
        await scheduler.submit(_req(channel="telegram")).result()
    finally:
        await teardown()
    assert agent.bound["ask"] == (None, "cron:1")
    assert agent.bound["autofill"] is None
    # Unattended by decision: the permission turn is bound with no responder,
    # so the ask tier refuses with a reason instead of waiting on nobody.
    assert agent.bound["permission"].responder is None
    assert agent.bound["permission"].conversation_id == "cron:1"


# --- the approval that rides the ask round-trip ---


class _ScriptedAsker:
    """Stands in for AskViaTool: returns one scripted answer, records the call."""

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.calls: list[dict] = []

    async def ask(self, prompt, choices, conversation_id, **kwargs):
        self.calls.append({"prompt": prompt, "choices": choices, "conversation_id": conversation_id})
        return self.answer


@pytest.mark.parametrize(
    ("answer", "choice", "feedback", "answered"),
    [
        ("Allow once", "allow", "", True),
        ("1", "allow", "", True),
        ("Deny", "deny", "", True),
        ("2", "deny", "", True),
        ("Deny and stop", "deny_stop", "", True),
        ("3", "deny_stop", "", True),
        ("keep it, I need that file", "deny", "keep it, I need that file", True),
        # Nobody said anything. `None` is a round trip this turn cannot make at
        # all; an empty reply is the ask timing out or being skipped. Both still
        # deny -- this fails closed by shape -- and both used to reach the gate
        # spelled exactly like a person choosing Deny, so a model was told it had
        # been refused and went looking for another way.
        (None, "deny", "", False),
        ("", "deny", "", False),
    ],
)
async def test_approval_via_ask_maps_the_reply(answer, choice, feedback, answered):
    asker = _ScriptedAsker(answer)
    responder = ApprovalViaAsk(asker, "telegram:u1")

    outcome = await responder.await_approval(
        command="rm scratch.txt", description="Approve this action: rm scratch.txt"
    )

    assert outcome.choice.value == choice
    assert outcome.feedback == feedback
    assert outcome.answered is answered
    call = asker.calls[0]
    assert "rm scratch.txt" in call["prompt"]
    assert call["choices"] == ["Allow once", "Deny", "Deny and stop"]
    assert call["conversation_id"] == "telegram:u1"


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


# --- teardown delivers what the sink handed it before closing the outlets ---


async def test_teardown_delivers_what_is_still_queued_before_closing_the_hub():
    """The barrier the reload notice depends on, tested at the seam.

    ``_make_gateway_sink`` enqueues the cut-turn sentence while
    ``scheduler.shutdown`` resolves that turn, and the outlet worker has not
    run yet when ``teardown`` moves on. Closing the hub there cancels the
    worker, and the sentence is lost. The test above proves the sink enqueues
    it; this proves teardown lets it out.
    """
    ch = _FakeChannel("telegram")
    _scheduler, hub, _readback, _sources, teardown = build_gateway(_ReplyAgent(), {"telegram": ch})
    await hub.dispatch(Text(content="owed to a cut turn", source=_src()))

    await teardown()

    assert [content for _to, content, _media in ch.sent if "owed" in content], ch.sent


async def test_a_channel_that_never_takes_the_send_does_not_hold_the_swap_open():
    """Bounded, so one dead transport cannot stall a generation swap."""
    entered = asyncio.Event()

    class _StuckChannel(_FakeChannel):
        async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
            entered.set()
            await asyncio.Event().wait()

    ch = _StuckChannel("telegram")
    _scheduler, hub, _readback, _sources, teardown = build_gateway(_ReplyAgent(), {"telegram": ch}, shutdown_grace=0.05)
    await hub.dispatch(Text(content="never lands", source=_src()))
    await asyncio.wait_for(entered.wait(), timeout=5)

    await asyncio.wait_for(teardown(), timeout=_DELIVERY_GRACE + 5)

    assert ch.sent == []


async def test_the_gateway_probes_the_asking_capability_not_the_class():
    """A shelf may hand the loop another ask tool; the entrance must not care
    which class it is, only that it can be asked through directly. The probe is
    the paper the rpc entrance already uses, so both doors read one shape."""
    import inspect

    from raven.contracts.asking import SupportsDirectAsk
    from raven.gateway import spine as gateway_spine

    class _DuckAskTool:
        name = "ask_user"

        async def ask_direct(self, *a, **k):
            return None

    class _MuteTool:
        name = "ask_user"

    assert isinstance(_DuckAskTool(), SupportsDirectAsk)
    assert not isinstance(_MuteTool(), SupportsDirectAsk)
    source = inspect.getsource(gateway_spine)
    assert "isinstance(ask_tool, SupportsDirectAsk)" in source
    assert "isinstance(ask_tool, AskUserTool)" not in source


# --- a turn that ends with nothing sent still tells its channel it is over ---


async def test_a_turn_that_ended_without_a_reply_tells_the_channel_it_is_over():
    """A typing indicator stops when the reply is sent. A user's message the model
    answered with silence sends nothing, so the channel is told the turn is over
    instead."""
    ch = _EndsTurns("telegram")
    scheduler, hub, _rb, _sources, teardown = build_gateway(_SilentAgent(), {"telegram": ch})
    try:
        await scheduler.submit(_user_req()).result()
        await hub.wait_idle("telegram")
    finally:
        await teardown()

    assert ch.ended == ["c7"]
    assert ch.sent == []


async def test_a_turn_without_a_reply_that_was_not_marked_silent_is_ended_too():
    """What triggers the end is that nothing was sent, not that the model said
    so."""
    ch = _EndsTurns("telegram")
    scheduler, _hub, _rb, _sources, teardown = build_gateway(_SilentAgent(silent=False), {"telegram": ch})
    try:
        await scheduler.submit(_user_req()).result()
    finally:
        await teardown()

    assert ch.ended == ["c7"]


async def test_a_turn_that_sent_a_reply_is_not_told_it_is_over():
    """The send already stopped what the channel started for the turn; a second
    stop would be a network call for nothing on matrix and weixin."""
    ch = _EndsTurns("telegram")
    scheduler, hub, _rb, _sources, teardown = build_gateway(_ReplyAgent([Text(content="done")]), {"telegram": ch})
    try:
        await scheduler.submit(_user_req()).result()
        await hub.wait_idle("telegram")
    finally:
        await teardown()

    assert ch.ended == []
    assert [content for _to, content, _media in ch.sent] == ["done"]


class _NeverEnds(_EndsTurns):
    """A channel whose stop is a network call that does not come back."""

    def __init__(self, name="telegram") -> None:
        super().__init__(name)
        self.release = asyncio.Event()
        self.cut_at: float | None = None

    async def end_turn(self, chat_id: str) -> None:
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cut_at = asyncio.get_running_loop().time()
            raise
        await super().end_turn(chat_id)


async def test_a_slow_channel_cannot_hold_a_turn_that_ended_without_a_reply():
    """Stopping a typing indicator can be a network call, and the turn is already
    over: its result, and the next turn on its lane, do not wait for one."""
    ch = _NeverEnds("telegram")
    scheduler, hub, _rb, _sources, teardown = build_gateway(_SilentAgent(), {"telegram": ch})
    try:
        await asyncio.wait_for(scheduler.submit(_user_req()).result(), timeout=2)
    finally:
        ch.release.set()
        await teardown()

    assert ch.ended == ["c7"]


async def test_teardown_stops_an_end_turn_the_channel_never_finishes(monkeypatch):
    """A channel whose transport is gone gets the delivery grace to end the turn,
    then is cut: it costs the indicator, not the swap."""
    import raven.gateway.spine as gateway_spine

    monkeypatch.setattr(gateway_spine, "_DELIVERY_GRACE", 0.05)
    ch = _NeverEnds("telegram")
    scheduler, hub, _rb, _sources, teardown = build_gateway(_SilentAgent(), {"telegram": ch})
    await asyncio.wait_for(scheduler.submit(_user_req()).result(), timeout=2)

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(teardown(), timeout=2)

    assert ch.cut_at is not None
    assert ch.cut_at - started >= 0.04
    assert ch.ended == []


async def test_a_finished_turn_end_is_not_kept():
    """The gateway runs for weeks, and a user's message may well be answered with
    silence: a turn end that has finished leaves the set teardown settles."""
    ending: set[asyncio.Task[None]] = set()
    ch = _EndsTurns("telegram")
    sink = _make_gateway_sink(
        DeliveryHub(), _SilentAgent(), {"telegram:c7": _src(chat_id="c7")}, None, {"telegram": ch}, ending
    )

    await sink(TurnStarted(origin=Origin.USER, conversation_id="telegram:c7", turn_id="t1"))
    await sink(_silent_end("telegram:c7", "t1"))
    (task,) = ending
    await task
    await asyncio.sleep(0)

    assert ch.ended == ["c7"]
    assert ending == set()


@pytest.mark.parametrize(
    ("origin", "told"),
    [
        (Origin.USER, True),
        (Origin.HEARTBEAT, False),
        (Origin.CRON, False),
        (Origin.SENTINEL, False),
        (Origin.SUBAGENT, False),
    ],
)
async def test_only_a_turn_a_user_started_is_told_it_is_over(origin, told):
    """Only a user's message starts a typing indicator. A silent heartbeat or cron
    turn has none to end, and telling the chat would cancel the indicator of a
    user turn running in it."""
    ch = _EndsTurns("telegram")
    scheduler, _hub, _rb, _sources, teardown = build_gateway(_SilentAgent(), {"telegram": ch})
    try:
        await scheduler.submit(
            _req(channel="telegram", chat_id="c7", conversation=f"{origin.value}:1", origin=origin)
        ).result()
    finally:
        await teardown()

    assert ch.ended == (["c7"] if told else [])


@pytest.mark.parametrize("how", ["ended", "failed", "cancelled"])
async def test_a_finished_user_turn_is_forgotten(how):
    """The sink holds a turn's origin only while it runs. The id is reused here to
    see whether it is still held: a silent end on a turn that is not a user's
    is not told."""
    ending: set[asyncio.Task[None]] = set()
    ch = _EndsTurns("telegram")
    sources = {"telegram:c7": _src(chat_id="c7")}
    sink = _make_gateway_sink(_RecordingHub(), _SilentAgent(), sources, None, {"telegram": ch}, ending)
    await sink(TurnStarted(origin=Origin.USER, conversation_id="telegram:c7", turn_id="t1"))
    await sink(_finish_of(how, "telegram:c7", "t1"))

    sources["telegram:c7"] = _src(chat_id="c7")
    await sink(_silent_end("telegram:c7", "t1"))
    await asyncio.gather(*ending)

    assert ch.ended == []


class _RaisingEnd(_EndsTurns):
    async def end_turn(self, chat_id: str) -> None:
        await super().end_turn(chat_id)
        raise ConnectionError("transport is gone")


async def test_a_channel_whose_end_turn_raises_costs_a_log_line_not_the_turn():
    """The turn is already over when its channel is told so: an adapter that
    cannot stop its indicator is logged, and nothing is raised into the task
    teardown waits on."""
    from loguru import logger

    from raven.gateway.spine import _end_turn

    ch = _RaisingEnd("telegram")
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        await _end_turn(ch, "c7")
    finally:
        logger.remove(sink_id)

    assert ch.ended == ["c7"]
    assert any("transport is gone" in line for line in lines)


@pytest.mark.parametrize("registered", [True, False])
async def test_a_turn_ends_quietly_for_a_channel_that_has_no_end_turn(registered):
    """A channel need not carry the hook, and a turn's source need not be a
    channel the gateway holds (the web page is not one)."""
    channels = {"telegram": _FakeChannel("telegram")} if registered else {}
    scheduler, _hub, _rb, _sources, teardown = build_gateway(_SilentAgent(), channels)
    try:
        outcome = await scheduler.submit(_user_req()).result()
    finally:
        await teardown()

    assert isinstance(outcome, TurnOutcome)
