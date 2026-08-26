"""The ACP spine: typed-event translation, the settle gate, and the pool pin."""

import asyncio
from typing import Any

import pytest

from raven.acp.spine import (
    USER_POOL,
    AcpOutlet,
    AcpSession,
    AcpSessions,
    TurnAlreadyRunningError,
    _usage_update,
    build_acp,
)
from raven.spine import (
    ChatType,
    Media,
    MediaOut,
    Notice,
    NoticeKind,
    Origin,
    Reasoning,
    Source,
    Text,
    ToolEvent,
    ToolPhase,
    TurnOutcome,
    TurnRequest,
    Usage,
)

SID = "acp:20260825_000000_abc123"
SRC = Source(channel="acp", chat_id="20260825_000000_abc123", sender_id="acp-client", chat_type=ChatType.DM)


def _outlet(frames: list[dict]) -> AcpOutlet:
    return AcpOutlet("acp", frames.append, cwd="/work")


def _update(frame: dict) -> dict:
    assert frame["method"] == "session/update"
    assert frame["params"]["sessionId"] == SID
    return frame["params"]["update"]


# -- outlet translation -------------------------------------------------------


async def test_reasoning_becomes_thought_chunk():
    frames: list[dict] = []
    await _outlet(frames).deliver(Reasoning(content="hmm", source=SRC, conversation_id=SID))
    assert _update(frames[0]) == {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}}


async def test_text_becomes_message_chunk_and_empty_is_eaten():
    frames: list[dict] = []
    outlet = _outlet(frames)
    await outlet.deliver(Text(content="answer", source=SRC, conversation_id=SID))
    await outlet.deliver(Text(content="", source=SRC, conversation_id=SID))
    assert len(frames) == 1
    assert _update(frames[0])["sessionUpdate"] == "agent_message_chunk"


async def test_stream_chunks_become_message_chunks_and_done_is_silent():
    frames: list[dict] = []
    outlet = _outlet(frames)
    await outlet.send_stream_chunk("chat", SID, "hel", done=False)
    await outlet.send_stream_chunk("chat", SID, "", done=False)
    await outlet.send_stream_chunk("chat", SID, "", done=True)
    assert len(frames) == 1
    assert _update(frames[0])["content"]["text"] == "hel"


async def test_tool_start_maps_kind_title_locations_and_hides_raw_input():
    frames: list[dict] = []
    await _outlet(frames).deliver(
        ToolEvent(
            phase=ToolPhase.START,
            tool_call_id="t1",
            name="read_file",
            arguments={"path": "notes.md", "api_key": "sk-abcdefghijklmnop1234"},
            source=SRC,
            conversation_id=SID,
        )
    )
    update = _update(frames[0])
    assert update["sessionUpdate"] == "tool_call"
    assert update["toolCallId"] == "t1"
    assert update["kind"] == "read"
    assert update["status"] == "in_progress"
    assert update["locations"] == [{"path": "/work/notes.md"}]
    assert "rawInput" not in update
    assert "sk-abcdefghijklmnop1234" not in str(update)


async def test_tool_complete_carries_status_from_the_failure_convention():
    frames: list[dict] = []
    outlet = _outlet(frames)
    ok = ToolEvent(
        phase=ToolPhase.COMPLETE, tool_call_id="t1", result_preview="all good", source=SRC, conversation_id=SID
    )
    bad = ToolEvent(
        phase=ToolPhase.COMPLETE,
        tool_call_id="t2",
        result_preview="Error: no such file",
        source=SRC,
        conversation_id=SID,
    )
    await outlet.deliver(ok)
    await outlet.deliver(bad)
    assert _update(frames[0])["status"] == "completed"
    assert _update(frames[1])["status"] == "failed"
    assert _update(frames[1])["content"][0]["content"]["text"] == "Error: no such file"


async def test_tool_complete_redacts_and_truncates_the_preview():
    frames: list[dict] = []
    truncated = ToolEvent(
        phase=ToolPhase.COMPLETE,
        tool_call_id="t3",
        result_preview="api_key=sk-abcdefghijklmnop1234",
        truncated=True,
        source=SRC,
        conversation_id=SID,
    )
    await _outlet(frames).deliver(truncated)
    text = _update(frames[0])["content"][0]["content"]["text"]
    assert "sk-abcdefghijklmnop1234" not in text
    assert text.endswith("[truncated]")


async def test_progress_notice_is_thought_never_answer_text():
    # Progress narration in agent_message_chunk would be read by the consuming
    # raven as answer text; it must ride the thought stream.
    frames: list[dict] = []
    outlet = _outlet(frames)
    await outlet.deliver(Notice(kind=NoticeKind.PROGRESS, detail="round 1/8", source=SRC, conversation_id=SID))
    await outlet.deliver(Notice(kind=NoticeKind.TOOL_HINT, detail='web_search("x")', source=SRC, conversation_id=SID))
    await outlet.deliver(Notice(kind=NoticeKind.INJECTED, detail="internal", source=SRC, conversation_id=SID))
    kinds = [_update(f)["sessionUpdate"] for f in frames]
    assert kinds == ["agent_thought_chunk", "agent_thought_chunk"]


async def test_media_out_is_named_by_path():
    frames: list[dict] = []
    media = MediaOut(
        media=(Media(path="/out/report.pdf", mime="application/pdf", kind="file"),), source=SRC, conversation_id=SID
    )
    await _outlet(frames).deliver(media)
    assert "/out/report.pdf" in _update(frames[0])["content"]["text"]


def test_usage_update_needs_real_window_numbers():
    assert _usage_update(None) is None
    assert _usage_update({}) is None
    assert _usage_update({"context_used": 10, "context_max": 0}) is None
    update = _usage_update({"context_used": 10, "context_max": 100, "cost_usd": 0.5})
    assert update == {
        "sessionUpdate": "usage_update",
        "used": 10,
        "size": 100,
        "cost": {"amount": 0.5, "currency": "USD"},
    }
    assert "cost" not in _usage_update({"context_used": 10, "context_max": 100, "cost_usd": True})


# -- the settle gate ----------------------------------------------------------


async def test_exactly_one_terminating_event_settles_a_prompt():
    sessions = AcpSessions()
    sessions.add(AcpSession(session_id=SID, chat_id="c"))
    future = sessions.begin_turn(SID)
    assert sessions.pending(SID)
    assert sessions.settle(SID, "cancelled") is True
    # The second terminating event is normal timing, not a bug: it must no-op.
    assert sessions.settle(SID, "end_turn") is False
    assert await future == "cancelled"
    sessions.end_turn(SID)
    assert not sessions.pending(SID)


async def test_a_second_prompt_on_a_busy_session_is_refused():
    sessions = AcpSessions()
    sessions.add(AcpSession(session_id=SID, chat_id="c"))
    sessions.begin_turn(SID)
    with pytest.raises(TurnAlreadyRunningError):
        sessions.begin_turn(SID)


async def test_close_settles_every_pending_prompt_as_cancelled():
    sessions = AcpSessions()
    sessions.add(AcpSession(session_id="acp:a", chat_id="a"))
    sessions.add(AcpSession(session_id="acp:b", chat_id="b"))
    fa = sessions.begin_turn("acp:a")
    fb = sessions.begin_turn("acp:b")
    sessions.close()
    assert await fa == "cancelled"
    assert await fb == "cancelled"


def test_user_pool_is_pinned_to_one():
    # Plan §6 decision B: WebSearchTool/WebFetchTool keep per-turn state as
    # shared instance attributes; a second concurrent turn silently pollutes
    # the generated distribution. Widening this is a per-session-AgentLoop
    # change, never a knob.
    assert USER_POOL == 1


# -- end to end through the real scheduler ------------------------------------


class _StubLoop:
    """Emits like AgentLoop.run_turn on the non-streaming path: progress
    notices and tool events mid-turn, one closing Text as the reply."""

    workspace = None

    def __init__(self, fail: bool = False, hang: bool = False):
        self.fail = fail
        self.hang = hang
        self.stream_flags: list[bool] = []

    async def run_turn(self, req, emit, drain, stream=False, usage_sink=None, **_kw):
        self.stream_flags.append(stream)
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise RuntimeError("provider exploded: api_key=sk-abcdefghijklmnop1234")
        await emit(Notice(kind=NoticeKind.PROGRESS, detail="round 1/8"))
        await emit(Text(content="hello world"))
        if usage_sink is not None:
            usage_sink.update({"context_used": 11, "context_max": 100, "cost_usd": 0.01})
        return TurnOutcome(usage=Usage(1, 2, 3), explicit_reply=True)


def _request() -> TurnRequest:
    return TurnRequest(origin=Origin.USER, source=SRC, text="hi")


async def _run_one(loop: Any, frames: list[dict]) -> str:
    scheduler, _hub, sessions, teardown = build_acp(loop, frames.append)
    try:
        sessions.add(AcpSession(session_id=SID, chat_id=SRC.chat_id))
        future = sessions.begin_turn(SID)
        handle = scheduler.submit(_request())
        sessions.bind_handle(SID, handle)
        return await asyncio.wait_for(future, timeout=5)
    finally:
        await teardown()


async def test_happy_turn_delivers_reply_then_usage_then_settles():
    frames: list[dict] = []
    loop = _StubLoop()
    stop = await _run_one(loop, frames)
    assert stop == "end_turn"
    # The answer-semantics pin (AcpTurnRunner docstring): the turn runs
    # NON-streaming, so the reply is the one final committed Text -- streamed
    # deltas would also carry mid-turn narration and the DR verify pass
    # re-stating the answer, and the consuming raven accumulates every
    # message chunk as the reply.
    assert loop.stream_flags == [False]
    updates = [_update(f) for f in frames]
    kinds = [u["sessionUpdate"] for u in updates]
    # Every frame belonging to the turn is on the wire before the prompt
    # settles, and the usage rides out last.
    assert kinds == ["agent_thought_chunk", "agent_message_chunk", "usage_update"]
    assert updates[1]["content"]["text"] == "hello world"
    assert updates[-1]["used"] == 11


async def test_failed_turn_lands_as_content_with_end_turn_and_no_secret():
    frames: list[dict] = []
    stop = await _run_one(_StubLoop(fail=True), frames)
    assert stop == "end_turn"
    updates = [_update(f) for f in frames]
    assert updates[-1]["sessionUpdate"] == "agent_message_chunk"
    assert "The turn failed" in updates[-1]["content"]["text"]
    assert "sk-abcdefghijklmnop1234" not in updates[-1]["content"]["text"]


async def test_cancelled_turn_settles_cancelled_and_stays_silent():
    frames: list[dict] = []
    loop = _StubLoop(hang=True)
    scheduler, _hub, sessions, teardown = build_acp(loop, frames.append)
    try:
        sessions.add(AcpSession(session_id=SID, chat_id=SRC.chat_id))
        future = sessions.begin_turn(SID)
        handle = scheduler.submit(_request())
        sessions.bind_handle(SID, handle)
        await asyncio.sleep(0.05)
        handle.cancel()
        assert await asyncio.wait_for(future, timeout=5) == "cancelled"
        # The sink's own cancelled TurnFailed said nothing.
        assert frames == []
    finally:
        await teardown()


class _TwoTurnLoop:
    """First turn hangs until cancelled and takes 200ms to abort (a tool
    unwinding); the second answers normally. The slow abort is load-bearing:
    it is the window in which the next prompt arms while the cancelled turn's
    terminating event is still in flight -- with an instant abort the old code
    passed this test too."""

    workspace = None

    def __init__(self):
        self.calls = 0

    async def run_turn(self, req, emit, drain, stream=False, usage_sink=None, **_kw):
        self.calls += 1
        if self.calls == 1:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)
                raise
        await emit(Text(content="second answer"))
        return TurnOutcome(usage=Usage(1, 2, 3), explicit_reply=True)


async def test_a_cancelled_turns_late_ending_cannot_hijack_the_next_prompt():
    """Cancel a running turn, then prompt again at once -- the natural "cancel
    the long research turn and ask the refined question" flow. The cancelled
    turn's terminating event must answer only its own prompt: before
    session/cancel waited out the unwind, the event was still in flight when
    the next prompt armed, and settled it as a chunkless "cancelled" the host
    reads as an empty turn.
    """
    from raven.acp.methods import AcpMethods

    frames: list[dict] = []
    loop = _TwoTurnLoop()
    scheduler, _hub, sessions, teardown = build_acp(loop, frames.append)
    try:
        methods = AcpMethods(submit=scheduler.submit, sessions=sessions, emit=frames.append, session_manager=None)
        await methods.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        new = await methods.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/tmp", "mcpServers": []}}
        )
        sid = new["result"]["sessionId"]

        def _prompt(request_id: int, text: str):
            return methods.handle(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
                }
            )

        prompt_a = asyncio.ensure_future(_prompt(3, "first question"))
        await asyncio.sleep(0.05)
        await methods.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
        assert (await asyncio.wait_for(prompt_a, timeout=5))["result"] == {"stopReason": "cancelled"}

        response_b = await asyncio.wait_for(_prompt(4, "refined question"), timeout=5)
        assert response_b["result"] == {"stopReason": "end_turn"}
        chunks = [
            f["params"]["update"]["content"]["text"]
            for f in frames
            if f.get("method") == "session/update"
            and f["params"]["update"].get("sessionUpdate") == "agent_message_chunk"
        ]
        assert chunks == ["second answer"]
    finally:
        await teardown()


async def test_turn_ending_for_a_foreign_lane_settles_nothing():
    # A subagent result re-injection ends on its own conversation id; it must
    # not answer an ACP session's prompt or put frames on its stream.
    frames: list[dict] = []
    scheduler, _hub, sessions, teardown = build_acp(_StubLoop(), frames.append)
    try:
        sessions.add(AcpSession(session_id=SID, chat_id=SRC.chat_id))
        future = sessions.begin_turn(SID)
        foreign = TurnRequest(
            origin=Origin.USER,
            source=Source(channel="acp", chat_id="other", sender_id="x", chat_type=ChatType.DM),
            text="hi",
        )
        handle = scheduler.submit(foreign)
        await asyncio.wait_for(handle.result(), timeout=5)
        await asyncio.sleep(0.05)
        assert not future.done()
    finally:
        await teardown()
