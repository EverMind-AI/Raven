"""The one-shot CLI spine: protocol conformance, stream forwarding, and rendering."""

import asyncio

from raven.agent.spine_runner import AgentTurnRunner
from raven.cli._one_shot_spine import (
    CliOutlet,
    TurnUsageSummary,
    _render_summary_line,
    build_one_shot_spine,
    make_hub_sink,
)
from raven.spine import (
    AnswerlessTurnError,
    ChatType,
    Notice,
    NoticeKind,
    Origin,
    Source,
    Text,
    ToolEvent,
    ToolPhase,
    TurnEnded,
    TurnFailed,
    TurnOutcome,
    TurnRequest,
    TurnRunner,
    TurnStarted,
    Usage,
)
from raven.spine.delivery import Outlet
from raven.spine.events import Reasoning


def _src(channel="cli", chat_id="c1") -> Source:
    return Source(channel=channel, chat_id=chat_id, sender_id="user", chat_type=ChatType.DM)


class FakeAgentLoop:
    def __init__(self, reply="hello") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def run_turn(self, req, emit, drain, *, stream) -> TurnOutcome:
        self.calls.append(
            {
                "text": req.text,
                "stream": stream,
                "conversation": req.conversation,
            }
        )
        # The REPL wires stream=False, so run_turn emits the reply as one Text.
        await emit(Text(content=self.reply, source=req.source))
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)


def test_pieces_satisfy_their_spine_protocols():
    assert isinstance(CliOutlet("cli", lambda s: None), Outlet)
    assert isinstance(AgentTurnRunner(object(), stream=False), TurnRunner)


def _collect():
    events: list = []

    async def emit(e):
        events.append(e)

    return events, emit


# --- AgentTurnRunner (the native runner; REPL uses stream=False) ---


async def test_runner_delegates_to_run_turn_with_stream_false():
    loop = FakeAgentLoop("hi there")
    runner = AgentTurnRunner(loop, stream=False)
    src = _src()
    req = TurnRequest(origin=Origin.USER, source=src, text="hi", conversation="cli:c1")
    events, emit = _collect()
    outcome = await runner.run(req, emit, lambda: [])
    # The REPL runner passes stream=False so run_turn emits a Text, not StreamDelta.
    assert loop.calls == [{"text": "hi", "stream": False, "conversation": "cli:c1"}]
    assert len(events) == 1 and isinstance(events[0], Text)
    assert events[0].content == "hi there" and events[0].source is src
    assert outcome.explicit_reply is True


async def test_runner_stream_flag_is_forwarded():
    loop = FakeAgentLoop()
    runner = AgentTurnRunner(loop, stream=True)
    events, emit = _collect()
    await runner.run(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="cli:c1"), emit, lambda: [])
    assert loop.calls[0]["stream"] is True  # build_rpc_spine would pass True; build_one_shot_spine False


async def test_runner_binds_an_unattended_permission_turn():
    # Decision, not omission: a -m turn has no human round-trip (ask_user has no
    # broker on this surface), so the permission turn binds with no responder --
    # the ask tier refuses with a reason instead of waiting on nobody, and the
    # denied-digest memory is scoped to this turn rather than a shared default.
    from raven.cli._one_shot_spine import _OneShotTurnRunner
    from raven.permissions.turn import current_turn

    seen: dict = {}

    class _Recording(FakeAgentLoop):
        async def run_turn(self, req, emit, drain, **kwargs):
            turn = current_turn()
            seen["responder"] = turn.responder
            seen["conversation_id"] = turn.conversation_id
            return await super().run_turn(req, emit, drain, **kwargs)

    runner = _OneShotTurnRunner(_Recording(), stream=False)
    events, emit = _collect()
    await runner.run(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="cli:c1"), emit, lambda: [])
    assert seen["responder"] is None
    assert seen["conversation_id"] == "cli:c1"


# --- CliOutlet ---


async def test_cli_outlet_renders_text():
    rendered: list[str] = []
    outlet = CliOutlet("cli", rendered.append)
    await outlet.deliver(Text(content="rendered me"))
    assert rendered == ["rendered me"]


async def test_cli_outlet_eats_non_text():
    rendered: list[str] = []
    outlet = CliOutlet("cli", rendered.append)
    # No render_notice: both Notice kinds eaten, status quo.
    await outlet.deliver(Notice(kind=NoticeKind.PROGRESS))
    await outlet.deliver(Notice(kind=NoticeKind.TOOL_HINT))
    assert rendered == []  # eaten, not rendered


async def test_cli_outlet_renders_completed_file_delivery():
    rendered: list[str] = []
    outlet = CliOutlet("cli", rendered.append)
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="deliver-1",
            metadata={
                "raven_delivery": {
                    "message": "Final outputs",
                    "files": [{"name": "report.pdf", "path": "/tmp/report.pdf"}],
                }
            },
        )
    )
    assert rendered == ["Final outputs\nDelivered files:\n- report.pdf: /tmp/report.pdf"]


# --- CliOutlet progress rendering (-m path): the two-gate parity ---


def _notice_outlet(*, send_progress: bool, send_tool_hints: bool):
    notices: list[str] = []
    outlet = CliOutlet(
        "cli",
        lambda t: None,
        render_notice=notices.append,
        send_progress=send_progress,
        send_tool_hints=send_tool_hints,
    )
    return notices, outlet


async def test_cli_outlet_renders_progress_when_send_progress_on():
    notices, outlet = _notice_outlet(send_progress=True, send_tool_hints=False)
    await outlet.deliver(Notice(kind=NoticeKind.PROGRESS, detail="thinking"))
    assert notices == ["thinking"]


async def test_cli_outlet_default_config_does_not_leak_tool_hints():
    # The over-show regression the fork guards against: with the default config
    # (send_progress=True, send_tool_hints=False), progress shows but tool-hint
    # text (read_file(...)) must NOT — exactly as the bus path did.
    notices, outlet = _notice_outlet(send_progress=True, send_tool_hints=False)
    await outlet.deliver(Notice(kind=NoticeKind.PROGRESS, detail="thinking"))
    await outlet.deliver(Notice(kind=NoticeKind.TOOL_HINT, detail='read_file("x")'))
    assert notices == ["thinking"]  # tool-hint suppressed by send_tool_hints=False


async def test_cli_outlet_renders_tool_hint_when_send_tool_hints_on():
    notices, outlet = _notice_outlet(send_progress=False, send_tool_hints=True)
    await outlet.deliver(Notice(kind=NoticeKind.PROGRESS, detail="thinking"))
    await outlet.deliver(Notice(kind=NoticeKind.TOOL_HINT, detail='read_file("x")'))
    assert notices == ['read_file("x")']  # progress suppressed, tool-hint shown


async def test_cli_outlet_eats_reasoning_without_progress():
    # Reasoning is never a CLI deliverable: without render_notice it is eaten.
    rendered: list[str] = []
    await CliOutlet("cli", rendered.append).deliver(Reasoning(content="searching..."))
    assert rendered == []
    # With render_notice set it is eaten too -- CliOutlet renders Notice only.
    notices, outlet = _notice_outlet(send_progress=False, send_tool_hints=False)
    await outlet.deliver(Reasoning(content="searching..."))
    assert notices == []


# --- make_hub_sink ---


class FakeHub:
    def __init__(self) -> None:
        self.dispatched: list = []

    async def dispatch(self, event) -> None:
        self.dispatched.append(event)


async def test_sink_routes_deliverables_and_drops_lifecycle():
    hub = FakeHub()
    sink = make_hub_sink(hub)
    await sink(Text(content="t", source=_src()))
    await sink(TurnStarted())
    await sink(TurnFailed(error="e", cancelled=False))
    await sink(TurnEnded(usage=Usage(0, 0, 0), latency_ms=1.0, explicit_reply=False))
    assert len(hub.dispatched) == 1  # the three lifecycle events dropped (no source -> never to _enqueue)
    assert isinstance(hub.dispatched[0], Text)


# --- build_one_shot_spine: real scheduler + hub + CliOutlet, only the agent loop faked ---


class _EchoLoop:
    async def run_turn(self, req, emit, drain, *, stream) -> TurnOutcome:
        # The one-shot path wires stream=False; run_turn emits the reply as one Text.
        await emit(Text(content=f"reply<{req.text}>", source=req.source))
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)


async def test_build_one_shot_spine_defaults_to_single_slot_pools():
    scheduler, _hub, teardown = build_one_shot_spine(_EchoLoop(), "cli", lambda t: None)
    try:
        assert scheduler._pools._user._value == 1
        assert scheduler._pools._system._value == 1
    finally:
        await teardown()


async def test_build_one_shot_spine_honors_configured_pool_sizes():
    scheduler, _hub, teardown = build_one_shot_spine(_EchoLoop(), "cli", lambda t: None, user_pool=5, system_pool=3)
    try:
        assert scheduler._pools._user._value == 5
        assert scheduler._pools._system._value == 3
    finally:
        await teardown()


async def test_build_one_shot_spine_teardown_leaves_no_pending_tasks():
    # The two bugs were both in teardown/interrupt; guard it: after a real turn,
    # scheduler.shutdown() + hub.aclose() must stop every task build_one_shot_spine/submit
    # spawned (lane worker, reaper, outlet worker) — no "Task destroyed pending".
    baseline = asyncio.all_tasks()
    scheduler, hub, teardown = build_one_shot_spine(_EchoLoop(), "cli", lambda t: None)
    handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="cli:c1"))
    await handle.result()
    await hub.wait_idle("cli")
    spawned = asyncio.all_tasks() - baseline - {asyncio.current_task()}
    assert any(not t.done() for t in spawned)  # live spine tasks exist before teardown
    await teardown()  # the same teardown production runs in its finally
    assert all(t.done() for t in spawned)  # teardown stopped every one


async def test_build_one_shot_spine_prints_a_failed_turn_in_its_own_words():
    """The hub sink drops lifecycle events, so a turn that failed used to print
    nothing at all; the one-shot reader is owed the failure's own words."""

    class _FailingLoop:
        async def run_turn(self, req, emit, drain, *, stream) -> TurnOutcome:
            raise AnswerlessTurnError("Error calling LLM (network@stub): boom")

    rendered: list[str] = []
    failures: list[str] = []
    scheduler, hub, teardown = build_one_shot_spine(
        _FailingLoop(), "cli", rendered.append, render_error=failures.append
    )
    try:
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="cli:c1"))
        outcome = await handle.result()
        assert isinstance(outcome, TurnFailed)
        assert outcome.error == "Error calling LLM (network@stub): boom"
        await hub.wait_idle("cli")
        assert failures == ["Error calling LLM (network@stub): boom"]
        assert rendered == [], "a failure is not drawn as the reply"
        # A stop is the reader's own act and prints nothing, as before.
        await scheduler._sink(TurnFailed(error="cancelled", cancelled=True, conversation_id="cli:c1", turn_id="t2"))
        await hub.wait_idle("cli")
        assert failures == ["Error calling LLM (network@stub): boom"]
    finally:
        await teardown()


async def test_build_one_shot_spine_falls_back_to_render_for_a_failed_turn():
    class _FailingLoop:
        async def run_turn(self, req, emit, drain, *, stream) -> TurnOutcome:
            raise AnswerlessTurnError("Error calling LLM (network@stub): boom")

    rendered: list[str] = []
    scheduler, hub, teardown = build_one_shot_spine(_FailingLoop(), "cli", rendered.append)
    try:
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="cli:c1"))
        outcome = await handle.result()
        assert isinstance(outcome, TurnFailed)
        assert outcome.error == "Error calling LLM (network@stub): boom"
        await hub.wait_idle("cli")
        assert rendered == ["Error calling LLM (network@stub): boom"]
    finally:
        await teardown()


class _FakeUsageTracker:
    """snapshot()-only stand-in; each set() swaps in a new lifetime total."""

    def __init__(self) -> None:
        from raven.contracts.token_strategy import UsageSnapshot

        self._snap = UsageSnapshot(model="stub")

    def snapshot(self):
        return self._snap

    def set(self, **totals) -> None:
        from raven.contracts.token_strategy import UsageSnapshot

        self._snap = UsageSnapshot(model="stub", **totals)


def test_turn_summary_tiny_cost_shows_floor_not_free():
    # 4 decimal places round a sub-cent cost like 0.00004 down to "$0",
    # which reads as a free call; the line must show a floor instead.
    tracker = _FakeUsageTracker()
    summary = TurnUsageSummary(tracker)
    summary.turn_started()
    tracker.set(input_tokens=100, output_tokens=10, cost_usd=0.00004)
    line = summary.take_line()
    assert line is not None
    assert "<$0.0001" in line
    assert " $0 " not in f" {line} "


def test_turn_summary_normal_cost_still_renders_exact():
    tracker = _FakeUsageTracker()
    summary = TurnUsageSummary(tracker)
    summary.turn_started()
    tracker.set(input_tokens=200, output_tokens=20, cost_usd=0.0042)
    line = summary.take_line()
    assert line is not None
    assert "$0.0042" in line


def test_summary_line_indented_like_progress_notices(capsys):
    # agent_commands renders progress notices as '  [dim]. ...' (two-space
    # indent); the summary line sits in the same visual column.
    _render_summary_line("1.2k in / 340 out tokens")
    out = capsys.readouterr().out
    assert out.startswith("  ↳")


async def test_summary_distinguishes_free_unknown_and_partial():
    from raven.contracts.token_strategy import UsageSnapshot
    from raven.token_wise.usage_tracker import UsageTracker

    tracker = UsageTracker(persist=False)
    summary = TurnUsageSummary(tracker)
    summary.turn_started()
    await tracker.after_llm_call({}, UsageSnapshot(model="model", input_tokens=10, cost_usd=0))
    assert "$0" in summary.take_line()
    summary.turn_started()
    await tracker.after_llm_call({}, UsageSnapshot(model="model", input_tokens=10))
    line = summary.take_line()
    assert "cost unknown" in line and "$0" not in line
    summary.turn_started()
    await tracker.after_llm_call({}, UsageSnapshot(model="model", input_tokens=10, cost_usd=0.4))
    await tracker.after_llm_call({}, UsageSnapshot(model="model", input_tokens=10))
    line = summary.take_line()
    assert "$0.4" in line and "1 calls with unknown cost" in line


async def test_build_one_shot_spine_hands_the_turns_refusals_to_the_caller():
    """The gate records a refusal from inside the turn's own task, which the
    scheduler built -- so the entrance cannot read its own context back. The
    handle the runner keeps is how a one-shot learns what was turned down."""
    from raven.permissions.turn import note_refusal

    class _RefusingLoop:
        async def run_turn(self, req, emit, drain, *, stream) -> TurnOutcome:
            note_refusal("write_file", "write_file path=a.txt", "not interactive", "unattended")
            await emit(Text(content="done, supposedly", source=req.source))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    received: list = []
    scheduler, hub, teardown = build_one_shot_spine(_RefusingLoop(), "cli", lambda t: None, on_refusals=received.extend)
    try:
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="cli:c1"))
        await handle.result()
        await hub.wait_idle("cli")
    finally:
        await teardown()

    assert [(r.tool_name, r.source) for r in received] == [("write_file", "unattended")]


async def test_build_one_shot_spine_hands_unanswered_questions_to_the_caller():
    """A question noted inside the turn is readable after teardown, the same
    way a refusal is: the entrance cannot read its own context back."""
    from raven.permissions.turn import note_unanswered

    class _AskingLoop:
        async def run_turn(self, req, emit, drain, *, stream) -> TurnOutcome:
            note_unanswered("Which base branch?")
            await emit(Text(content="assumed main", source=req.source))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    received: list = []
    scheduler, hub, teardown = build_one_shot_spine(_AskingLoop(), "cli", lambda t: None, on_unanswered=received.extend)
    try:
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="cli:c1"))
        await handle.result()
        await hub.wait_idle("cli")
    finally:
        await teardown()

    assert [item.question for item in received] == ["Which base branch?"]


async def test_background_refusals_are_collected_after_the_parent_turn_finishes():
    from raven.permissions.turn import note_refusal

    child: asyncio.Task | None = None

    class _BackgroundRefusingLoop:
        async def run_turn(self, req, emit, drain, **kwargs):
            nonlocal child

            async def refuse_later():
                await asyncio.sleep(0)
                note_refusal("exec", "git push origin topic", "not interactive", "unattended")

            child = asyncio.create_task(refuse_later())
            await emit(Text(content="parent done", source=req.source))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    received: list = []
    scheduler, _hub, teardown = build_one_shot_spine(
        _BackgroundRefusingLoop(),
        "cli",
        lambda t: None,
        on_refusals=received.extend,
    )
    handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="first", conversation="cli:c1"))
    await handle.result()
    assert received == []
    assert child is not None
    await child
    await teardown()

    assert [(r.tool_name, r.action) for r in received] == [("exec", "git push origin topic")]


async def test_runner_keeps_refusals_from_each_one_shot_follow_up_turn():
    from raven.cli._one_shot_spine import _OneShotTurnRunner
    from raven.permissions.turn import note_refusal

    class _OnceRefusingLoop(FakeAgentLoop):
        async def run_turn(self, req, emit, drain, **kwargs):
            if req.text == "first":
                note_refusal("exec", "rm x", "not interactive", "unattended")
            return await super().run_turn(req, emit, drain, **kwargs)

    runner = _OneShotTurnRunner(_OnceRefusingLoop(), stream=False)
    _, emit = _collect()
    for text in ("first", "second"):
        await runner.run(TurnRequest(origin=Origin.USER, source=_src(), text=text, conversation="cli:c1"), emit, list)
    assert [(r.tool_name, r.action) for r in runner.refusals()] == [("exec", "rm x")]


def test_turn_summary_adds_what_sub_agents_billed_the_root():
    """A product sub-agent is its own ACP process, so its calls never reach the
    tracker here; a line that reads only that tracker undercounted a delegating
    turn by the whole delegation."""
    from raven.contracts.token_strategy import UsageSnapshot

    windows: list[tuple] = []

    def delegated(root, since, until):
        windows.append((root, since, until))
        return UsageSnapshot(model="d", input_tokens=900, output_tokens=100, cost_usd=0.3, calls=2)

    tracker = _FakeUsageTracker()
    summary = TurnUsageSummary(tracker, delegated=delegated)
    summary.turn_started("cli:root")
    tracker.set(input_tokens=100, output_tokens=20, cost_usd=0.1, calls=1)
    line = summary.take_line()

    assert line is not None
    assert "1k in / 120 out tokens" in line
    assert "$0.4" in line
    assert "incl. 2 sub-agent calls" in line
    assert windows[0][0] == "cli:root"

    # A second line in the same turn picks up where the first left off, or a
    # delegation would be billed on both.
    summary.take_line()
    assert windows[1][1] == windows[0][2]


def test_follow_up_turn_does_not_skip_usage_recorded_after_the_first_reply():
    from raven.contracts.token_strategy import UsageSnapshot

    windows: list[tuple] = []

    def delegated(root, since, until):
        windows.append((root, since, until))
        calls = 1 if len(windows) == 2 else 0
        return UsageSnapshot(model="d", input_tokens=20 * calls, output_tokens=5 * calls, cost_usd=0.1, calls=calls)

    tracker = _FakeUsageTracker()
    summary = TurnUsageSummary(tracker, delegated=delegated)
    summary.turn_started("cli:root")
    tracker.set(input_tokens=10, output_tokens=2, cost_usd=0.01, calls=1)
    summary.take_line()

    summary.turn_started("cli:root")
    line = summary.take_line()

    assert windows[1][1] == windows[0][2]
    assert line is not None
    assert "incl. 1 sub-agent calls" in line
    assert "$0.1" in line


def test_turn_summary_prices_a_turn_whose_only_calls_were_delegated():
    from raven.contracts.token_strategy import UsageSnapshot

    tracker = _FakeUsageTracker()
    summary = TurnUsageSummary(
        tracker,
        delegated=lambda *_: UsageSnapshot(model="d", input_tokens=10, output_tokens=5, cost_usd=0.02, calls=1),
    )
    summary.turn_started("cli:root")
    line = summary.take_line()
    assert line is not None and "$0.02" in line and "cost unknown" not in line


def test_turn_summary_survives_a_ledger_it_cannot_read():
    def broken(*_):
        raise OSError("telemetry dir vanished")

    tracker = _FakeUsageTracker()
    summary = TurnUsageSummary(tracker, delegated=broken)
    summary.turn_started("cli:root")
    tracker.set(input_tokens=200, output_tokens=20, cost_usd=0.0042, calls=1)
    line = summary.take_line()
    assert line is not None and "$0.0042" in line and "sub-agent" not in line
