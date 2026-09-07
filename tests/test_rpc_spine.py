"""The RPC spine seam: protocol conformance and the asking capabilities a turn carries."""

import asyncio
from dataclasses import replace

from raven.acp_client.asker import current_ask
from raven.agent.tools.ask_user import AskUserTool
from raven.agent.tools.message import MessageTool
from raven.agent.tools.shell import ExecTool
from raven.config.raven import SubagentQuestionsConfig
from raven.contracts.asking import SupportsApprovalTurn, SupportsDirectAsk
from raven.rpc.spine import (
    RpcOutlet,
    RpcTurnRunner,
    build_rpc_spine,
    make_dag_progress_sink,
)
from raven.sandbox import ExecResult, SandboxExecutor
from raven.spine import (
    ChatType,
    EpisodeStart,
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

    def has_subscribers(self, session_key: str) -> bool:
        return False


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


class _DirectRecordingExecutor(SandboxExecutor):
    def __init__(self) -> None:
        self.commands: list[str] = []

    @property
    def is_sandboxed(self) -> bool:
        return False

    async def exec(self, command: str, **kwargs) -> ExecResult:
        self.commands.append(command)
        return ExecResult(stdout="ok", stderr="", exit_code=0)


class _ApprovalResponder:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.requests: list[dict] = []

    async def await_approval(self, **request) -> bool:
        self.requests.append(request)
        return self.answer


class _ApprovalRunLoop:
    def __init__(self, tool: ExecTool) -> None:
        self.tools = {"exec": tool}
        self.result = ""

    async def run_turn(self, req, emit, drain, **kwargs) -> TurnOutcome:
        self.tools["exec"].set_tool_call_id("call-a")
        self.result = await self.tools["exec"].execute("rm file.txt")
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)


def _collect():
    events: list = []

    async def emit(e):
        events.append(e)

    return events, emit


# --- protocol conformance ---


def test_pieces_satisfy_their_spine_protocols():
    assert isinstance(RpcTurnRunner(object(), FakeEmitter(), {}, {}), TurnRunner)
    outlet = RpcOutlet("tui", FakeEmitter())
    assert isinstance(outlet, Outlet)
    assert isinstance(outlet, SupportsStreaming)
    assert outlet.capabilities.streaming is True


def test_the_real_tools_satisfy_the_asking_seams_the_runner_probes():
    # The runner binds by shape, so the shapes must keep fitting the tools that
    # ship: a rename on either side would otherwise fail closed in silence.
    assert isinstance(ExecTool(executor=_DirectRecordingExecutor()), SupportsApprovalTurn)
    assert isinstance(AskUserTool(), SupportsDirectAsk)


# --- RpcTurnRunner (drives run_turn stream=True; stashes rich usage) ---


async def test_runner_drives_run_turn_and_stashes_rich_usage():
    rich = {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8, "cost_usd": 0.01, "context_used": 42}
    loop = _RunTurnLoop(events=[StreamDelta(delta="he"), StreamDelta(delta="llo")], usage=rich)
    usages: dict[str, dict] = {}
    runner = RpcTurnRunner(loop, FakeEmitter(), usages, {})
    req = TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1")
    events, emit = _collect()

    outcome = await runner.run(req, emit, lambda: [])

    # run_turn's events pass straight through emit (the outlet maps them to wire).
    assert [e.delta for e in events] == ["he", "llo"]
    # The full usage_sink (cost / context, richer than 3-field Usage) is stashed
    # for the sink to attach to message.complete.
    assert usages["tui:c1"] == rich
    assert outcome.explicit_reply is True


async def test_user_turn_receives_tui_approval_capability(tmp_path):
    executor = _DirectRecordingExecutor()
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))
    loop = _ApprovalRunLoop(tool)
    responder = _ApprovalResponder(True)
    runner = RpcTurnRunner(loop, FakeEmitter(), {}, {}, approval_responder=responder)
    # The id rides the request: the lane resolved it before the runner ran, so the
    # approval binding names this turn rather than whatever a per-lane slot holds.
    req = TurnRequest(origin=Origin.USER, source=_src(), text="delete", conversation="tui:c1", turn_id="turn-a")
    _events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    assert executor.commands == ["rm file.txt"]
    assert responder.requests[0]["turn_id"] == "turn-a"


async def test_cron_turn_does_not_receive_tui_approval_capability(tmp_path):
    executor = _DirectRecordingExecutor()
    tool = ExecTool(executor=executor, working_dir=str(tmp_path))
    loop = _ApprovalRunLoop(tool)
    responder = _ApprovalResponder(True)
    runner = RpcTurnRunner(loop, FakeEmitter(), {}, {}, approval_responder=responder)
    req = TurnRequest(origin=Origin.CRON, source=_src(), text="delete", conversation="cron:c1", turn_id="turn-a")
    _events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    assert "requires user approval" in loop.result.model_text
    assert executor.commands == []
    assert responder.requests == []


class _ApprovalSeam:
    """Not an ExecTool -- only the seam the runner binds approval through."""

    def __init__(self) -> None:
        self.bound: list[tuple[object, str, str]] = []

    def start_approval_turn(self, responder, *, conversation_id: str, turn_id: str) -> None:
        self.bound.append((responder, conversation_id, turn_id))


async def test_the_approval_binding_reaches_any_tool_exposing_the_seam():
    # An entrance probes capability, not concrete class: whatever a shelf seats
    # at ``exec`` is bound if it exposes start_approval_turn.
    tool = _ApprovalSeam()
    responder = _ApprovalResponder(True)
    runner = RpcTurnRunner(_RunTurnLoop(tools={"exec": tool}), FakeEmitter(), {}, {}, approval_responder=responder)
    req = TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="turn-b")
    _events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    assert tool.bound == [(responder, "tui:c1", "turn-b")]


async def test_a_tool_at_exec_without_the_seam_is_left_alone():
    # A real Tool, just not one with the approval seam: the probe declines, the
    # turn runs, and nothing reaches a method the tool does not have.
    tool = MessageTool()
    runner = RpcTurnRunner(
        _RunTurnLoop(tools={"exec": tool}), FakeEmitter(), {}, {}, approval_responder=_ApprovalResponder(True)
    )
    req = TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="turn-b")
    _events, emit = _collect()

    outcome = await runner.run(req, emit, lambda: [])

    assert not isinstance(tool, SupportsApprovalTurn)
    assert outcome.explicit_reply is True


class _DirectAsk:
    """Not an AskUserTool -- only the seam AskViaTool adapts."""

    def __init__(self) -> None:
        self.asked: list[tuple] = []

    async def ask_direct(self, prompt, choices, conversation_id, timeout_s=None, *, index=0, total=1, batch=None):
        self.asked.append((prompt, choices, conversation_id, index, total, batch))
        return "yes"


class _AskBindingLoop(_RunTurnLoop):
    """Records what the turn bound as its asker. Read inside run_turn: that is
    the context the binding is made in, and the one a sub-agent reads it from."""

    def __init__(self, ask_tool) -> None:
        super().__init__(tools={"ask_user": ask_tool})
        self.subagent_questions_config = SubagentQuestionsConfig()
        self.bound: tuple = (None, "")

    async def run_turn(self, req, emit, drain, **kwargs) -> TurnOutcome:
        self.bound = current_ask()
        return await super().run_turn(req, emit, drain, **kwargs)


async def test_the_asker_binding_reaches_any_tool_exposing_ask_direct():
    tool = _DirectAsk()
    loop = _AskBindingLoop(tool)
    runner = RpcTurnRunner(loop, FakeEmitter(), {}, {})
    req = TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="turn-c")
    _events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    asker, cid = loop.bound
    assert cid == "tui:c1"
    assert await asker.ask("proceed?", ["yes", "no"], "tui:c1") == "yes"
    assert tool.asked == [("proceed?", ["yes", "no"], "tui:c1", 0, 1, None)]


async def test_a_tool_at_ask_user_without_ask_direct_binds_no_asker():
    # Bound to None rather than left unset, so a sub-agent's question declines
    # instead of reaching a method the tool does not have.
    tool = MessageTool()
    loop = _AskBindingLoop(tool)
    runner = RpcTurnRunner(loop, FakeEmitter(), {}, {})
    req = TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="turn-c")
    _events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    assert not isinstance(tool, SupportsDirectAsk)
    assert loop.bound == (None, "tui:c1")


class _WatchedEmitter(FakeEmitter):
    """FakeEmitter that pretends a set of conversations have live viewers."""

    def __init__(self, watched: set[str]):
        super().__init__()
        self._watched = watched

    def has_subscribers(self, session_key: str) -> bool:
        return session_key in self._watched


async def test_a_subagent_relay_in_a_watched_conversation_binds_the_asker():
    tool = _DirectAsk()
    loop = _AskBindingLoop(tool)
    runner = RpcTurnRunner(loop, _WatchedEmitter({"tui:c1"}), {}, {})
    req = TurnRequest(
        origin=Origin.SUBAGENT,
        source=_src(),
        text="done",
        conversation="tui:c1",
        turn_id="turn-s",
        delegated={"kind": "dag", "label": "r1", "status": "ok"},
    )
    _events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    asker, cid = loop.bound
    assert cid == "tui:c1"
    assert asker is not None
    assert await asker.ask("proceed?", ["yes", "no"], "tui:c1") == "yes"
    assert tool.asked == [("proceed?", ["yes", "no"], "tui:c1", 0, 1, None)]


async def test_a_subagent_relay_nobody_watches_binds_no_asker():
    tool = _DirectAsk()
    loop = _AskBindingLoop(tool)
    runner = RpcTurnRunner(loop, _WatchedEmitter(set()), {}, {})
    req = TurnRequest(origin=Origin.SUBAGENT, source=_src(), text="done", conversation="tui:c1", turn_id="turn-s")
    _events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    assert loop.bound == (None, "tui:c1")


async def test_a_cron_turn_binds_no_asker_even_in_a_watched_conversation():
    tool = _DirectAsk()
    loop = _AskBindingLoop(tool)
    runner = RpcTurnRunner(loop, _WatchedEmitter({"tui:c1"}), {}, {})
    req = TurnRequest(origin=Origin.CRON, source=_src(), text="job", conversation="cron:j1", turn_id="turn-c")
    _events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    assert loop.bound == (None, "cron:j1")


async def test_runner_emits_eve22_synthetic_tool_complete_when_message_tool_fired():
    message_tool = MessageTool()
    loop = _RunTurnLoop(tools={"message": message_tool})

    async def _run_turn(req, emit, drain, *, stream, inline_tool_stream=False, usage_sink=None):
        # the message tool replied this turn (turn-local sent flag)
        message_tool._turn.set(replace(message_tool._cur(), sent=True))
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    loop.run_turn = _run_turn
    runner = RpcTurnRunner(loop, FakeEmitter(), {}, {})
    req = TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="T7")
    events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    # A lone synthetic ToolEvent(COMPLETE) keyed by the turn id (no matching start;
    # the loop skips the message tool on its general path).
    assert len(events) == 1 and isinstance(events[0], ToolEvent)
    assert events[0].phase is ToolPhase.COMPLETE and events[0].tool_call_id == "msg-T7"


async def test_runner_no_synthetic_when_message_tool_did_not_fire():
    loop = _RunTurnLoop(tools={"message": MessageTool()})  # sent flag stays False
    runner = RpcTurnRunner(loop, FakeEmitter(), {}, {})
    events, emit = _collect()
    await runner.run(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1"), emit, lambda: [])
    assert events == []  # no synthetic completion


async def test_runner_cron_captures_reply_non_streaming():
    # A CRON turn runs non-streaming and its reply is read back for the cron
    # fan-out (the cron:<job_id> conversation has no subscriber, so streaming it
    # would deliver nowhere). Mirrors the gateway's GatewayTurnRunner read-back.
    loop = _RunTurnLoop(reply_text="reminder fired")
    readback: dict[str, str] = {}
    runner = RpcTurnRunner(loop, FakeEmitter(), {}, readback)
    req = TurnRequest(origin=Origin.CRON, source=_src(chat_id="direct"), text="[cron]", conversation="cron:job1")
    events, emit = _collect()

    await runner.run(req, emit, lambda: [])

    assert loop.last_stream is False  # CRON runs non-streaming
    assert readback["cron:job1"] == "reminder fired"  # reply captured for fan-out


# --- RpcOutlet.deliver: maps each spine event to its wire event ---


async def test_outlet_deliver_reasoning_to_thinking_delta():
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.deliver(Reasoning(content="thinking", conversation_id="tui:c1"))
    assert emitter.emitted == [("tui:c1", {"type": "thinking.delta", "payload": {"text": "thinking"}})]


async def test_outlet_deliver_tool_event_to_tool_start_and_complete():
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
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
            {
                "type": "tool.start",
                "payload": {
                    "tool_call_id": "t1",
                    "name": "shell",
                    "arguments": {"cmd": "ls"},
                    "blocking": False,
                    "display": None,
                },
            },
        ),
        (
            "tui:c1",
            {
                "type": "tool.complete",
                "payload": {
                    "tool_call_id": "t1",
                    "result_preview": "ok",
                    "truncated": False,
                    "ok": True,
                    "metadata": None,
                    "diff": None,
                },
            },
        ),
    ]


async def test_outlet_deliver_tool_complete_forwards_the_diff():
    """The outlet's half of the write-diff chain.

    The page reads this field off tool.complete and prefers it over the guess it
    made from the arguments at tool.start, because a whole-file write's previous
    content survives nowhere else.
    """
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="t1",
            result_preview="ok",
            truncated=False,
            diff="--- a\n+++ b\n-before\n+after",
            conversation_id="tui:c1",
        )
    )

    assert emitter.emitted[0][1]["payload"]["diff"] == "--- a\n+++ b\n-before\n+after"


async def test_outlet_deliver_tool_start_forwards_blocking():
    """A blocking tool must reach the wire flagged: the web client suspends its
    turn-stream idle clock on this, and a sub-agent run outlasts that clock."""
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.START,
            tool_call_id="t1",
            name="run_subagent_dag",
            blocking=True,
            conversation_id="tui:c1",
        )
    )
    assert emitter.emitted[0][1]["payload"]["blocking"] is True


async def test_outlet_deliver_text_to_token_delta():
    # A non-streamed reply (clarification / hook short-circuit) rides one
    # token.delta — previously dropped on the TUI (the earlier dual-path runner ignored the
    # direct path's return value).
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.deliver(Text(content="please clarify", conversation_id="tui:c1"))
    assert emitter.emitted == [("tui:c1", {"type": "token.delta", "payload": {"text": "please clarify"}})]


async def test_outlet_deliver_eats_chatty_notices():
    # Progress and tool-hint notices exist for channels that cannot draw a tool
    # row. This client draws every call, so forwarding them narrates the same
    # work twice.
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.deliver(Notice(kind=NoticeKind.PROGRESS, detail="working", conversation_id="tui:c1"))
    await outlet.deliver(Notice(kind=NoticeKind.TOOL_HINT, detail="reading", conversation_id="tui:c1"))
    assert emitter.emitted == []  # no wire event for these today


async def test_outlet_deliver_media_to_a_media_event():
    """A reply's files reach the wire instead of being dropped at this hop.

    They used to be eaten here, which made the loss invisible to every layer
    above: a turn that produced a chart answered with text that referred to a
    file the client was never told about.
    """
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.deliver(
        MediaOut(
            media=(
                Media(path="/tmp/x.png", mime="image/png", kind="image"),
                Media(path="/tmp/y.csv", mime="text/csv", kind="file"),
            ),
            conversation_id="tui:c1",
        )
    )
    assert emitter.emitted == [
        (
            "tui:c1",
            {
                "type": "media",
                "payload": {
                    "items": [
                        {"path": "/tmp/x.png", "mime": "image/png", "kind": "image"},
                        {"path": "/tmp/y.csv", "mime": "text/csv", "kind": "file"},
                    ]
                },
            },
        )
    ]


async def test_outlet_deliver_media_with_nothing_in_it_emits_nothing():
    """The contract says ``items`` is never empty, so this cannot be forwarded.

    Not a theoretical guard: the emit site builds the tuple from a reply's path
    list, and an empty event would still make a client draw an attachment row
    with nothing behind it.
    """
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.deliver(MediaOut(media=(), conversation_id="tui:c1"))
    assert emitter.emitted == []


async def test_outlet_deliver_tool_complete_forwards_the_file_change():
    """The structured change beside the rendered diff.

    An ACP client cannot use the diff string at all -- its Diff content block is
    ``{path, newText, oldText}`` -- so this field is the only thing that reaches
    it, and it is absent rather than null when a call wrote nothing so that every
    payload the wire already carried keeps its shape.
    """
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="t1",
            result_preview="ok",
            truncated=False,
            file_change={"path": "/tmp/a.txt", "after": "new", "before": "old"},
            conversation_id="tui:c1",
        )
    )
    assert emitter.emitted[0][1]["payload"]["file_change"] == {
        "path": "/tmp/a.txt",
        "after": "new",
        "before": "old",
    }

    emitter.emitted.clear()
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="t2",
            result_preview="ok",
            truncated=False,
            conversation_id="tui:c1",
        )
    )
    assert "file_change" not in emitter.emitted[0][1]["payload"]


async def test_a_blocked_action_rides_notice_and_never_the_token_stream():
    """The one notice that replaces the answer instead of accompanying it.

    Pushed as ``token.delta`` -- which is how it used to reach the client -- it
    lands in the buffer holding the model's own prose, so it renders as the
    model's answer: run together with whatever the model narrated just before
    it, wearing the answer's copy and branch actions, and in English no matter
    what language the conversation is in.
    """
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.deliver(
        Notice(
            kind=NoticeKind.ACTION_BLOCKED,
            detail="Error: Command blocked by safety guard",
            conversation_id="tui:c1",
        )
    )
    assert emitter.emitted == [
        (
            "tui:c1",
            {
                "type": "notice",
                "payload": {"kind": "action_blocked", "detail": "Error: Command blocked by safety guard"},
            },
        )
    ]
    assert not any(ev["type"] == "token.delta" for _, ev in emitter.emitted)


async def test_every_outlet_emission_validates_against_the_wire_contract():
    """Whatever the outlet emits must parse as a declared ``TurnEvent``.

    The schema-match tests compare the two *declarations* to each other; this
    compares what the code actually puts on the wire to the declaration. Both are
    needed, and this is the one that was missing when ``tool.complete`` grew a
    ``file_change`` field that no declaration knew about: the payload models are
    ``extra="forbid"``, so an undeclared field makes a validating consumer drop
    the entire event rather than the unknown key.

    Driven through ``deliver`` with one of every Deliverable, so a new branch that
    invents a payload shape fails here instead of at a client.
    """
    from pydantic import TypeAdapter

    from raven.rpc.models import TurnEvent

    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    events = [
        Reasoning(content="thinking", conversation_id="tui:c1"),
        ToolEvent(phase=ToolPhase.START, tool_call_id="t1", name="read_file", conversation_id="tui:c1"),
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="t1",
            result_preview="ok",
            diff="--- a\n+++ b",
            metadata={"k": "v"},
            file_change={"path": "/tmp/a.txt", "after": "new", "before": "old"},
            conversation_id="tui:c1",
        ),
        Text(content="hello", conversation_id="tui:c1"),
        Notice(kind=NoticeKind.ACTION_BLOCKED, detail="blocked", conversation_id="tui:c1"),
        EpisodeStart(index=0, conversation_id="tui:c1"),
        MediaOut(media=(Media(path="/tmp/x.png", mime="image/png", kind="image"),), conversation_id="tui:c1"),
    ]
    for event in events:
        await outlet.deliver(event)
    await outlet.send_stream_chunk("tui:c1", "tui:c1", "delta")
    await outlet.emit_complete("tui:c1", "turn-1", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
    await outlet.emit_error("tui:c1", -32099, "boom", "internal", detail="stack")

    adapter = TypeAdapter(TurnEvent)
    assert len(emitter.emitted) == len(events) + 3
    for _, wire in emitter.emitted:
        adapter.validate_python(wire)


async def test_outlet_emits_token_delta_on_a_chunk():
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.send_stream_chunk("c1", "tui:c1", "hi", done=False)
    assert emitter.emitted == [("tui:c1", {"type": "token.delta", "payload": {"text": "hi"}})]


async def test_outlet_done_chunk_is_a_noop():
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.send_stream_chunk("c1", "tui:c1", "", done=True)
    assert emitter.emitted == []  # front-end has no stream-done event; complete is the sink's


async def test_outlet_eats_empty_delta():
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.send_stream_chunk("c1", "tui:c1", "", done=False)
    assert emitter.emitted == []


async def test_outlet_emit_complete_and_error_shapes():
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.emit_complete("tui:c1", "t1", {"total_tokens": 7})
    await outlet.emit_error("tui:c1", -32099, "turn_failed", "internal")
    assert emitter.emitted == [
        ("tui:c1", {"type": "message.complete", "payload": {"turn_id": "t1", "usage": {"total_tokens": 7}}}),
        ("tui:c1", {"type": "error", "payload": {"code": -32099, "message": "turn_failed", "reason": "internal"}}),
    ]


async def test_outlet_emit_error_includes_detail_when_present():
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.emit_error("tui:c1", -32099, "turn_failed", "internal", "No module named 'orjson'")
    assert emitter.emitted == [
        (
            "tui:c1",
            {
                "type": "error",
                "payload": {
                    "code": -32099,
                    "message": "turn_failed",
                    "reason": "internal",
                    "detail": "No module named 'orjson'",
                },
            },
        ),
    ]


# --- build_rpc_spine: real Scheduler + DeliveryHub + RpcOutlet, only the edges faked ---
# (faking the spine path would make the ordering / deadlock tests pass trivially;
#  message.complete-after-token and the empty-turn finalize only hold on the real
#  async path through the hub's per-outlet queue + wait_idle barrier.)


async def test_build_rpc_spine_defaults_to_single_slot_pools():
    scheduler, _hub, _turn_ids, teardown = build_rpc_spine(_RunTurnLoop(events=[]), FakeEmitter())
    try:
        assert scheduler._pools._user._value == 1
        assert scheduler._pools._system._value == 1
    finally:
        await teardown()


async def test_build_rpc_spine_honors_configured_pool_sizes():
    scheduler, _hub, _turn_ids, teardown = build_rpc_spine(
        _RunTurnLoop(events=[]), FakeEmitter(), user_pool=6, system_pool=4
    )
    try:
        assert scheduler._pools._user._value == 6
        assert scheduler._pools._system._value == 4
    finally:
        await teardown()


async def test_a_pool_sized_zero_lets_every_turn_in_at_once():
    """A deployment that wants no queuing says 0; the lane holds a gate that
    never waits, shaped like the semaphore it replaces."""
    from raven.spine.scheduler import OriginPools

    pools = OriginPools(user=0, system=0)
    req = TurnRequest(origin=Origin.USER, source=_src("cli", "c"), text="hi")
    gate = pools.for_request(req)
    entered = 0
    async with gate:
        async with gate:
            async with gate:
                entered = 3
    assert entered == 3 and gate.locked() is False


async def test_the_boundary_carries_the_delivery_identity_and_text():
    """The delivery row belongs at the moment the turn actually starts, so the
    boundary that marks that moment has to carry the identity -- and the
    injected text -- or the live client has nothing to draw."""
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[StreamDelta(delta="result")])
    scheduler, hub, turn_ids, teardown = build_rpc_spine(loop, emitter)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(
            TurnRequest(
                origin=Origin.SUBAGENT,
                source=_src(),
                text="[BEGIN UNTRUSTED subagent #ab12cd34 ...]\nresult\n[END UNTRUSTED subagent #ab12cd34]",
                conversation="tui:c1",
                turn_id="t1",
                delegated={"kind": "dag", "label": "run-7", "status": "ok", "run_id": "run-7"},
            )
        )
        await handle.result()
    finally:
        await teardown()

    start = next(e for k, e in emitter.emitted if e["type"] == "turn.started")
    d = start["payload"]["delegated"]
    assert d["kind"] == "dag" and d["label"] == "run-7" and d["run_id"] == "run-7"
    # The text that re-entered the conversation rides along, verbatim.
    assert d["content"] == "[BEGIN UNTRUSTED subagent #ab12cd34 ...]\nresult\n[END UNTRUSTED subagent #ab12cd34]"


async def test_a_runtime_turn_emits_the_boundary_and_no_message_start():
    """A SUBAGENT turn WITH a delivery identity gets a turn.started boundary
    (the client advances its live turn counter on it) and NO message.start --
    that event belongs to turn.send. A user turn gets neither from the spine:
    message.start comes from turn.send, and there is nothing to advance."""
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[StreamDelta(delta="result")])
    scheduler, hub, turn_ids, teardown = build_rpc_spine(loop, emitter)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(
            TurnRequest(
                origin=Origin.SUBAGENT,
                source=_src(),
                text="injected",
                conversation="tui:c1",
                turn_id="t1",
                delegated={"kind": "dag", "label": "r1", "status": "ok"},
            )
        )
        await handle.result()
    finally:
        await teardown()

    types = emitter.types()
    assert "turn.started" in types
    assert "message.start" not in types
    start = next(e for k, e in emitter.emitted if e["type"] == "turn.started")
    assert start["payload"]["turn_id"] == "t1"
    # The boundary carries the delivery identity, and nothing else.
    assert set(start["payload"]) == {"turn_id", "delegated"}


async def test_a_subagent_turn_without_delegated_identity_emits_no_boundary():
    """The reviewer's shape: deep research's deliver_text turn is Origin.
    SUBAGENT but persists only an assistant entry -- no delegated user entry
    opens it on reload. Emitting a live boundary for it would advance the
    workspace counter in a way the stored history does not, so it must not."""
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[StreamDelta(delta="answer")])
    scheduler, hub, turn_ids, teardown = build_rpc_spine(loop, emitter)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(
            TurnRequest(
                origin=Origin.SUBAGENT,
                source=_src(),
                text="",
                conversation="tui:c1",
                turn_id="t1",
                deliver_text="the answer",
            )
        )
        await handle.result()
    finally:
        await teardown()
    assert "turn.started" not in emitter.types()


async def test_a_user_turn_gets_no_boundary_from_the_spine():
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[StreamDelta(delta="hi")])
    scheduler, hub, turn_ids, teardown = build_rpc_spine(loop, emitter)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="t1")
        )
        await handle.result()
    finally:
        await teardown()
    assert "turn.started" not in emitter.types()


async def test_streaming_turn_emits_token_deltas_then_message_complete():
    emitter = FakeEmitter()
    loop = _RunTurnLoop(
        events=[StreamDelta(delta="a"), StreamDelta(delta="b")],
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )
    scheduler, hub, turn_ids, teardown = build_rpc_spine(loop, emitter)
    try:
        turn_ids["tui:c1"] = "t1"  # turn.send binds this; emulate here
        handle = scheduler.submit(
            TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="t1")
        )
        await handle.result()
    finally:
        await teardown()

    # message.complete lands AFTER both token.delta events (wait_idle barrier).
    assert emitter.types() == ["token.delta", "token.delta", "message.complete"]
    last_key, last = emitter.emitted[-1]
    assert last_key == "tui:c1"
    # The turn's own clock: a real elapsed time, so it is checked for shape
    # rather than value -- and lifted out so the rest of the payload keeps being
    # compared whole, which is what catches a field arriving unannounced.
    payload = dict(last["payload"])
    duration_ms = payload.pop("duration_ms")
    assert isinstance(duration_ms, int) and duration_ms >= 0
    assert payload == {
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
    scheduler, hub, turn_ids, teardown = build_rpc_spine(loop, emitter)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="t1")
        )
        await handle.result()
    finally:
        await teardown()

    assert emitter.types() == ["thinking.delta", "tool.start", "token.delta", "tool.complete", "message.complete"]


async def test_non_streamed_text_reaches_the_wire_as_a_token_delta():
    # A clarification / hook short-circuit reply (a Text, not a stream) now renders
    # on the TUI — the earlier runner dropped it (it ignored the direct path's return value).
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[Text(content="which file?")])
    scheduler, hub, turn_ids, teardown = build_rpc_spine(loop, emitter)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="t1")
        )
        await handle.result()
    finally:
        await teardown()

    assert emitter.types() == ["token.delta", "message.complete"]
    assert emitter.emitted[0][1]["payload"]["text"] == "which file?"


async def test_empty_stream_turn_still_emits_message_complete():
    # Deadlock regression: a turn that streams nothing must STILL finalize, or the
    # front-end's turn slot never clears and the next turn is rejected forever.
    emitter = FakeEmitter()
    scheduler, hub, turn_ids, teardown = build_rpc_spine(_RunTurnLoop(events=[]), emitter)
    try:
        turn_ids["tui:c1"] = "t9"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="t9")
        )
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
    scheduler, hub, turn_ids, teardown = build_rpc_spine(loop, emitter, readback_texts=readback)
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
    scheduler, hub, turn_ids, teardown = build_rpc_spine(_BoomLoop(), emitter)
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
    scheduler, hub, turn_ids, teardown = build_rpc_spine(_HangLoop(), emitter)
    try:
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1"))
        await started.wait()
        await handle.cancel()
        await handle.result()
    finally:
        await teardown()

    assert emitter.emitted == []  # no error from the sink on cancellation


# ---------------------------------------------------------------------------
# Turn identity: which turn a completion says ended, and whose slots it releases
# ---------------------------------------------------------------------------


async def test_an_internally_submitted_turns_completion_carries_its_own_id():
    """A lane is serial, so a turn the runtime submits itself (a sub-agent
    announce, a deep-research delivery) can end while a client's turn is still
    QUEUED behind it on the same lane. Stamping the completion from the lane's
    slot names the queued turn instead, and the client reads its own turn as
    ended -- losing that turn's whole content. The completion must name the turn
    that actually ended.
    """
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[Text(content="announce")])
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(loop, emitter)
    try:
        turn_ids["tui:c1"] = "u1"  # turn.send bound the client's turn; it is queued
        handle = scheduler.submit(
            TurnRequest(origin=Origin.SUBAGENT, source=_src(), text="done", conversation="tui:c1")
        )
        await handle.result()
    finally:
        await teardown()

    completions = [e for _k, e in emitter.emitted if e["type"] == "message.complete"]
    assert len(completions) == 1, emitter.emitted
    stamped = completions[0]["payload"]["turn_id"]
    assert stamped != "u1", "the announce turn's end was reported under the client turn's id"
    assert isinstance(stamped, str) and stamped


async def test_an_internally_submitted_turns_end_leaves_the_client_slots_alone():
    """The lane's client-facing slots belong to the turn ``turn.send`` bound them
    for. Releasing them at another turn's end opens the -32003 guard for a second
    send and leaves the queued turn's own end with no binding to report against.
    """
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[Text(content="announce")])
    targets = {"tui:c1": {"agent": "Raven-Code", "handle": "refactor-auth"}}
    ended: list[str] = []
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(
        loop, emitter, direct_targets=targets, on_turn_end=ended.append
    )
    try:
        turn_ids["tui:c1"] = "u1"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.SUBAGENT, source=_src(), text="done", conversation="tui:c1")
        )
        await handle.result()
    finally:
        await teardown()

    assert turn_ids["tui:c1"] == "u1"
    assert targets == {"tui:c1": {"agent": "Raven-Code", "handle": "refactor-auth"}}
    assert ended == []


async def test_a_turn_nobody_bound_still_reports_a_populated_turn_id():
    """``MessageCompletePayload.turn_id`` is a required ``str``, yet a turn no
    client bound an id for used to complete with ``None`` -- a frame a strict
    validator rejects, and the shape that makes a consumer's own-id guard fall
    through. The lane mints one, so no turn ends unidentified.
    """
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[Text(content="announce")])
    scheduler, _hub, _turn_ids, teardown = build_rpc_spine(loop, emitter)
    try:
        handle = scheduler.submit(
            TurnRequest(origin=Origin.SUBAGENT, source=_src(), text="done", conversation="tui:c1")
        )
        await handle.result()
    finally:
        await teardown()

    completions = [e for _k, e in emitter.emitted if e["type"] == "message.complete"]
    assert len(completions) == 1, emitter.emitted
    stamped = completions[0]["payload"]["turn_id"]
    assert isinstance(stamped, str) and stamped, f"turn_id must always be populated; got {stamped!r}"


async def test_the_owning_turns_end_still_releases_its_slots():
    """The gate must not become a leak: the turn that bound the lane's slots is
    still the one that frees them, or the -32003 guard never reopens."""
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[Text(content="done")])
    targets = {"tui:c1": {"agent": "Raven-Code", "handle": "refactor-auth"}}
    ended: list[str] = []
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(
        loop, emitter, direct_targets=targets, on_turn_end=ended.append
    )
    try:
        turn_ids["tui:c1"] = "u1"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="u1")
        )
        await handle.result()
    finally:
        await teardown()

    assert "tui:c1" not in turn_ids
    assert targets == {}
    assert ended == ["tui:c1"]
    completions = [e for _k, e in emitter.emitted if e["type"] == "message.complete"]
    assert completions[0]["payload"]["turn_id"] == "u1"


async def test_a_non_owning_turns_end_does_not_clobber_the_owners_live_usage():
    """``usages`` is keyed by lane (``conversation_id``), the same key
    ``turn_ids`` / ``direct_targets`` use -- a turn cancelled while queued and
    the turn that actually owns the lane share it. ``_drop``'s pop must be
    gated by ownership like those other releases already are, or a non-owning
    turn's end pops the owner's just-written usage out from under it, and the
    owner's own message.complete then reports it zeroed."""
    from raven.rpc.spine import RpcOutlet, _make_rpc_sink
    from raven.spine.delivery import DeliveryHub
    from raven.spine.events import TurnEnded, TurnFailed

    hub = DeliveryHub()
    emitter = FakeEmitter()
    turn_ids = {"tui:c1": "owner"}
    usages: dict[str, dict] = {}
    direct_targets: dict[str, dict] = {}
    outlet = RpcOutlet("tui", emitter, direct_targets)
    hub.register(outlet)
    sink = _make_rpc_sink(hub, outlet, "tui", turn_ids, usages, direct_targets, None)

    rich = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    usages["tui:c1"] = dict(rich)  # the owner's runner wrote this before its own TurnEnded fired

    # A different turn on the same lane, cancelled while queued, ends first.
    await sink(TurnFailed(error="cancelled", cancelled=True, conversation_id="tui:c1", turn_id="queued-1"))

    assert usages.get("tui:c1") == rich, "a non-owning turn's end must not drop the owner's live usage"

    await sink(
        TurnEnded(
            usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            latency_ms=1.0,
            explicit_reply=True,
            conversation_id="tui:c1",
            turn_id="owner",
        )
    )
    completions = [e for _k, e in emitter.emitted if e["type"] == "message.complete"]
    assert len(completions) == 1
    assert completions[0]["payload"]["usage"] == rich


async def test_the_synthetic_message_tool_call_id_uses_the_turns_own_id():
    """The synthetic id is derived from the turn, not from the lane's slot: read
    from the slot, an internally submitted turn's synthetic completion collides
    with the client turn queued behind it, and a client turn whose slot was
    already released gets a bare ``msg-``."""
    message_tool = MessageTool()

    class _MessageToolLoop:
        tools = {"message": message_tool}

        async def run_turn(self, req, emit, drain, **_kwargs) -> TurnOutcome:
            message_tool._turn.set(replace(message_tool._cur(), sent=True))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    emitter = FakeEmitter()
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(_MessageToolLoop(), emitter)
    try:
        turn_ids["tui:c1"] = "u1"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.SUBAGENT, source=_src(), text="done", conversation="tui:c1")
        )
        await handle.result()
    finally:
        await teardown()

    completes = [e for _k, e in emitter.emitted if e["type"] == "tool.complete"]
    assert len(completes) == 1, emitter.emitted
    tcid = completes[0]["payload"]["tool_call_id"]
    assert tcid != "msg-u1"
    assert tcid.startswith("msg-") and len(tcid) > len("msg-")


class TestDagProgressSink:
    """``run_subagent_dag`` publishes its progress on a side channel, not through
    the delivery hub, so it needs its own mapping onto the wire protocol. The
    graph is the only view the user gets of a fan-out whose tool result is
    clamped to 200 chars, so the mapping has to carry the whole topology."""

    async def test_run_started_carries_the_topology(self):
        emitter = FakeEmitter()
        sink = make_dag_progress_sink(emitter)

        await sink(
            "tui:c1",
            "dag_run_started",
            {
                "run_id": "dag-1",
                "tool_call_id": "call-a",
                "task_summary": "compare the two pricing pages",
                "nodes": [
                    {"id": "a", "subagent": "echo", "depends_on": [], "instance": None},
                    {
                        "id": "b",
                        "subagent": "echo",
                        "depends_on": ["a"],
                        "instance": "shared",
                        "node_summary": "read the pricing pages",
                    },
                ],
            },
        )

        assert emitter.types() == ["dag.run_started"]
        key, event = emitter.emitted[0]
        assert key == "tui:c1"
        assert event["payload"]["run_id"] == "dag-1"
        assert event["payload"]["tool_call_id"] == "call-a"
        # The line the graph was dispatched with. The sheet above the composer is
        # titled by it, and this event is the only place it can reach a live
        # reader -- dropping it left every running graph titled "Orchestration".
        assert event["payload"]["task_summary"] == "compare the two pricing pages"
        # "a" carries no node_summary in the input and must carry none on the wire
        # (absent, not null); "b" proves the field survives the live path at all.
        assert event["payload"]["nodes"] == [
            {"id": "a", "subagent": "echo", "depends_on": []},
            {
                "id": "b",
                "subagent": "echo",
                "depends_on": ["a"],
                "instance": "shared",
                "node_summary": "read the pricing pages",
            },
        ]

    async def test_node_updated_carries_status_and_timestamps(self):
        emitter = FakeEmitter()
        sink = make_dag_progress_sink(emitter)

        await sink(
            "tui:c1",
            "dag_node_updated",
            {"run_id": "dag-1", "node": "a", "status": "completed", "started_at": 10, "ended_at": 42},
        )

        assert emitter.types() == ["dag.node_updated"]
        assert emitter.emitted[0][1]["payload"] == {
            "run_id": "dag-1",
            "node": "a",
            "status": "completed",
            "started_at": 10,
            "ended_at": 42,
        }

    async def test_run_completed_flattens_the_manifest_and_drops_node_output_text(self):
        """``terminal_outputs`` holds every sink node's full text -- already in the
        tool result the model and the transcript both get. Repeating it here would
        put an unbounded blob on a progress frame."""
        emitter = FakeEmitter()
        sink = make_dag_progress_sink(emitter)

        await sink(
            "tui:c1",
            "dag_run_completed",
            {
                "run_id": "dag-1",
                "manifest": {
                    "dir": "/w/.ravenx_dag/dag-1",
                    "summary": {"total": 2, "completed": 1, "failed": 1, "skipped": 0},
                    "files": [
                        {"node": "a", "status": "completed", "output_file": "/w/.ravenx_dag/dag-1/a.out.md"},
                        {"node": "b", "status": "failed", "error": "boom"},
                    ],
                    "terminal_outputs": [{"node": "a", "text": "x" * 5000}],
                },
            },
        )

        assert emitter.types() == ["dag.run_completed"]
        payload = emitter.emitted[0][1]["payload"]
        assert payload["dir"] == "/w/.ravenx_dag/dag-1"
        assert payload["summary"] == {"total": 2, "completed": 1, "failed": 1, "skipped": 0}
        assert payload["files"] == [
            {"node": "a", "status": "completed", "output_file": "/w/.ravenx_dag/dag-1/a.out.md"},
            {"node": "b", "status": "failed", "error": "boom"},
        ]
        assert "terminal_outputs" not in payload

    async def test_run_completed_always_carries_files_even_with_no_manifest_to_flatten(self):
        """A run that ends without a manifest still has to close its graph.

        A collapse or a stop sends the detail and nothing else, so there is no
        `files` key to flatten -- and the consumer maps over that field
        unguarded (`fromCompletion`, ui-tui/src/domain/dagRun.ts), settling
        every node the manifest does not name. The coercion here is what makes
        that ending a closed graph rather than a crash, so the field has to
        survive an empty manifest.
        """
        emitter = FakeEmitter()
        sink = make_dag_progress_sink(emitter)

        await sink(
            "tui:c1",
            "dag_run_completed",
            {"run_id": "dag-1", "manifest": {"stopped": True, "summary": {"total": 2}}},
        )

        payload = emitter.emitted[0][1]["payload"]
        assert payload["files"] == []
        assert payload["summary"] == {"total": 2}
        assert payload["dir"] == ""

    async def test_an_unknown_event_name_is_dropped(self):
        """The sink is shared with the web channel, which may grow events this
        wire protocol has no variant for. Forwarding one blind would reach the
        client as an unhandled type."""
        emitter = FakeEmitter()
        sink = make_dag_progress_sink(emitter)

        await sink("tui:c1", "dag_something_new", {"run_id": "dag-1"})

        assert emitter.emitted == []


# ---------------------------------------------------------------------------
# Direct chat: which conversation a turn's events belong to
# ---------------------------------------------------------------------------


async def test_a_direct_turns_events_carry_its_target():
    """The client can switch instances mid-turn or reconnect, so it cannot infer
    the owning transcript from its own state -- only from the event."""
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[Text(content="done")])
    targets = {"tui:c1": {"agent": "Raven-Code", "handle": "refactor-auth"}}
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(loop, emitter, direct_targets=targets)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="t1")
        )
        await handle.result()
    finally:
        await teardown()

    assert emitter.types() == ["token.delta", "message.complete"]
    assert [e["payload"]["target"] for _k, e in emitter.emitted] == [
        {"agent": "Raven-Code", "handle": "refactor-auth"},
        {"agent": "Raven-Code", "handle": "refactor-auth"},
    ]


async def test_a_streamed_direct_turn_tags_every_chunk():
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[StreamDelta(delta="a"), StreamDelta(delta="b")])
    targets = {"tui:c1": {"agent": "mir", "handle": "scan"}}
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(loop, emitter, direct_targets=targets)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="t1")
        )
        await handle.result()
    finally:
        await teardown()

    deltas = [e["payload"] for _k, e in emitter.emitted if e["type"] == "token.delta"]
    assert [d["target"] for d in deltas] == [{"agent": "mir", "handle": "scan"}] * 2


async def test_the_turns_end_drops_the_binding():
    """Held past the turn, the next main-agent reply would be painted into the
    sub-agent's transcript."""
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[Text(content="done")])
    targets = {"tui:c1": {"agent": "Raven-Code", "handle": "refactor-auth"}}
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(loop, emitter, direct_targets=targets)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="t1")
        )
        await handle.result()
    finally:
        await teardown()

    assert targets == {}


async def test_a_failed_direct_turn_reports_against_its_target():
    """The error is what clears the spinner, so it has to name the same view the
    turn was streaming into -- and it is emitted after the binding is dropped."""

    class _BoomLoop:
        async def run_turn(self, req, emit, drain, **kwargs):
            raise RuntimeError("boom")

    emitter = FakeEmitter()
    targets = {"tui:c1": {"agent": "Raven-Code", "handle": "refactor-auth"}}
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(_BoomLoop(), emitter, direct_targets=targets)
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="t1")
        )
        await handle.result()
    finally:
        await teardown()

    errors = [e for _k, e in emitter.emitted if e["type"] == "error"]
    assert errors[0]["payload"]["target"] == {"agent": "Raven-Code", "handle": "refactor-auth"}


async def test_a_main_agent_turn_emits_no_target_key_at_all():
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[Text(content="done")])
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(loop, emitter, direct_targets={})
    try:
        turn_ids["tui:c1"] = "t1"
        handle = scheduler.submit(
            TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1", turn_id="t1")
        )
        await handle.result()
    finally:
        await teardown()

    assert all("target" not in e["payload"] for _k, e in emitter.emitted)


# ---------------------------------------------------------------------------
# Direct chat: concurrency (spec 2026-08-16-concurrent-direct-chats)
# ---------------------------------------------------------------------------


class _BlockingLoop:
    """A runner whose turns wait on a gate, so two can be in flight at once."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.started: list[str] = []

    async def run_turn(self, req, emit, drain, **_kwargs):
        from raven.spine.runner import TurnOutcome

        self.started.append(req.conversation or "")
        await self.gate.wait()
        await emit(Text(content=f"done {req.conversation}"))
        return TurnOutcome(usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0), explicit_reply=True)


async def test_two_instances_answer_at_the_same_time():
    """One lane is a serial domain, so an instance answering while another is
    answering needs a lane of its own. Both turns must be *inside* the runner
    before either finishes."""
    emitter = FakeEmitter()
    loop = _BlockingLoop()
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(loop, emitter)
    try:
        a = scheduler.submit(
            TurnRequest(
                origin=Origin.USER,
                source=_src(),
                text="hi",
                conversation="tui:c1#A/one",
                direct_target=("A", "one"),
            )
        )
        b = scheduler.submit(
            TurnRequest(
                origin=Origin.USER,
                source=_src(),
                text="hi",
                conversation="tui:c1#B/two",
                direct_target=("B", "two"),
            )
        )
        for _ in range(100):
            if len(loop.started) == 2:
                break
            await asyncio.sleep(0.01)

        assert sorted(loop.started) == ["tui:c1#A/one", "tui:c1#B/two"]
        loop.gate.set()
        await a.result()
        await b.result()
    finally:
        await teardown()


async def test_a_direct_turn_does_not_block_the_main_agent():
    """The point of a separate pool: several instances answering must not make
    the user queue to say one sentence to Raven."""
    emitter = FakeEmitter()
    loop = _BlockingLoop()
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(loop, emitter)
    try:
        direct = scheduler.submit(
            TurnRequest(
                origin=Origin.USER,
                source=_src(),
                text="hi",
                conversation="tui:c1#A/one",
                direct_target=("A", "one"),
            )
        )
        main = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1"))
        for _ in range(100):
            if len(loop.started) == 2:
                break
            await asyncio.sleep(0.01)

        assert sorted(loop.started) == ["tui:c1", "tui:c1#A/one"]
        loop.gate.set()
        await direct.result()
        await main.result()
    finally:
        await teardown()


async def test_a_direct_turns_events_reach_the_sessions_subscription():
    """The client holds one subscription per session and demultiplexes on the
    event's target; emitting on the lane key would reach nobody."""
    emitter = FakeEmitter()
    loop = _RunTurnLoop(events=[StreamDelta(delta="a"), Text(content="b")])
    targets = {"tui:c1#A/one": {"agent": "A", "handle": "one"}}
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(loop, emitter, direct_targets=targets)
    try:
        turn_ids["tui:c1#A/one"] = "t1"
        handle = scheduler.submit(
            TurnRequest(
                origin=Origin.USER,
                source=_src(),
                text="hi",
                conversation="tui:c1#A/one",
                direct_target=("A", "one"),
                turn_id="t1",
            )
        )
        await handle.result()
    finally:
        await teardown()

    assert {key for key, _e in emitter.emitted} == {"tui:c1"}
    assert all(e["payload"].get("target") == {"agent": "A", "handle": "one"} for _k, e in emitter.emitted)
