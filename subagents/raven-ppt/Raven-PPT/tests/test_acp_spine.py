"""``AcpOutlet`` maps spine events onto ``session/update``, and the sink answers
the prompt after the render barrier.

The mapping is the whole of this transport, so every branch is asserted against
the shape the protocol names -- ``sessionUpdate`` values, ``status`` values, where
the text lives inside ``content`` -- rather than against a summary of it.
"""

import asyncio
from pathlib import Path

import pytest

from raven.acp.redact import REPLACEMENT, redact
from raven.acp.session import AcpSession, SessionTable
from raven.acp.spine import (
    MAX_RESULT_PREVIEW,
    AcpOutlet,
    build_acp,
    resource_link,
    usage_update,
)
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
    Usage,
)
from raven.spine.delivery import Outlet, SupportsStreaming
from raven.spine.message import Media

SESSION_ID = "acp:s1"

# Every ``await`` on a session future carries a deadline. A prompt that is never
# settled is exactly the regression these assert against, and without one the
# test wedges instead of failing -- which in CI reads as a hung job rather than as
# a broken invariant.
SETTLE_TIMEOUT_S = 2.0


def _src() -> Source:
    return Source(channel="acp", chat_id="s1", sender_id="client", chat_type=ChatType.DM)


class Frames:
    """Collects the frames an emit would have written."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    def __call__(self, frame: dict) -> None:
        self.frames.append(frame)

    def updates(self) -> list[dict]:
        return [f["params"]["update"] for f in self.frames if f.get("method") == "session/update"]

    def kinds(self) -> list[str]:
        return [u["sessionUpdate"] for u in self.updates()]


@pytest.fixture
def wired(tmp_path: Path):
    sessions = SessionTable()
    session = AcpSession(session_id=SESSION_ID, cwd=str(tmp_path / "client"), root=tmp_path / "job")
    (tmp_path / "client").mkdir()
    sessions.add(session)
    frames = Frames()
    return sessions, session, frames, AcpOutlet("acp", frames, sessions)


# -- the outlet is an Outlet, and a streaming one -----------------------------


def test_outlet_satisfies_the_spine_protocols(wired):
    _sessions, _session, _frames, outlet = wired
    assert isinstance(outlet, Outlet)
    assert isinstance(outlet, SupportsStreaming)
    # Both halves matter: the hub only sends chunks to an outlet that *declares*
    # streaming as well as implementing it.
    assert outlet.capabilities.streaming is True


# -- text ---------------------------------------------------------------------


async def test_stream_delta_becomes_an_agent_message_chunk(wired):
    _sessions, _session, frames, outlet = wired
    await outlet.send_stream_chunk("s1", SESSION_ID, "half a ")
    await outlet.send_stream_chunk("s1", SESSION_ID, "sentence")
    assert frames.kinds() == ["agent_message_chunk", "agent_message_chunk"]
    assert [u["content"]["text"] for u in frames.updates()] == ["half a ", "sentence"]
    assert all(u["content"]["type"] == "text" for u in frames.updates())


async def test_a_closing_chunk_writes_no_frame(wired):
    _sessions, _session, frames, outlet = wired
    await outlet.send_stream_chunk("s1", SESSION_ID, "", done=True)
    # There is no stream-done event in ACP: the prompt's own stopReason ends the
    # turn, and a frame here would be a second ending.
    assert frames.frames == []


async def test_a_non_streamed_text_rides_the_same_chunk_kind(wired):
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(Text(content="the whole reply", source=_src(), conversation_id=SESSION_ID))
    assert frames.kinds() == ["agent_message_chunk"]
    assert frames.updates()[0]["content"]["text"] == "the whole reply"


async def test_reasoning_becomes_a_thought_chunk(wired):
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(Reasoning(content="considering", source=_src(), conversation_id=SESSION_ID))
    assert frames.kinds() == ["agent_thought_chunk"]
    assert frames.updates()[0]["content"] == {"type": "text", "text": "considering"}


async def test_empty_content_writes_nothing(wired):
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(Text(content="", source=_src(), conversation_id=SESSION_ID))
    await outlet.deliver(Reasoning(content="", source=_src(), conversation_id=SESSION_ID))
    await outlet.send_stream_chunk("s1", SESSION_ID, "")
    assert frames.frames == []


async def test_a_reply_chunk_is_recorded_on_the_session(wired):
    """The deck search reads the ``MEDIA:`` line out of what the client received,
    so the session's record has to be exactly the text that went on the wire."""
    _sessions, session, _frames, outlet = wired
    await outlet.send_stream_chunk("s1", SESSION_ID, "MEDIA: ")
    await outlet.deliver(Text(content="/tmp/d.pptx", source=_src(), conversation_id=SESSION_ID))
    await outlet.deliver(Reasoning(content="not part of the reply", source=_src(), conversation_id=SESSION_ID))
    assert session.reply_text() == "MEDIA: /tmp/d.pptx"


# -- tool calls ---------------------------------------------------------------


async def test_tool_start_becomes_an_in_progress_tool_call(wired):
    _sessions, session, frames, outlet = wired
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.START,
            tool_call_id="c1",
            name="read_file",
            arguments={"path": "notes.md"},
            source=_src(),
            conversation_id=SESSION_ID,
        )
    )
    update = frames.updates()[0]
    assert update["sessionUpdate"] == "tool_call"
    assert update["toolCallId"] == "c1"
    assert update["kind"] == "read"
    assert update["title"] == "read_file: notes.md"
    # Never "pending": by the time this event exists the call is running, and a
    # pending row that never changes reads as a hang.
    assert update["status"] == "in_progress"
    # Resolved against the session's cwd, because the spec requires absolute.
    # The session's root, not the client's `cwd`: the loop is built with
    # `workspace=session.root`, so a relative tool path means a file under it.
    assert update["locations"] == [{"path": str(Path(session.root) / "notes.md")}]


async def test_a_tool_call_never_carries_raw_input(wired):
    """``rawInput`` would publish the whole command line into a transcript the
    client persists."""
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.START,
            tool_call_id="c1",
            name="exec",
            arguments={"command": "curl -H 'Authorization: Bearer sk-secret' https://x"},
            source=_src(),
            conversation_id=SESSION_ID,
        )
    )
    update = frames.updates()[0]
    assert "rawInput" not in update
    assert update["kind"] == "execute"


async def test_a_tool_display_wins_over_a_derived_title(wired):
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.START,
            tool_call_id="c1",
            name="ppt_build",
            arguments={"project": "deck"},
            display="Building 20 pages",
            source=_src(),
            conversation_id=SESSION_ID,
        )
    )
    assert frames.updates()[0]["title"] == "Building 20 pages"


async def test_tool_complete_becomes_a_completed_update(wired):
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="c1",
            result_preview="wrote 4 pages",
            source=_src(),
            conversation_id=SESSION_ID,
        )
    )
    update = frames.updates()[0]
    assert update["sessionUpdate"] == "tool_call_update"
    assert update["toolCallId"] == "c1"
    assert update["status"] == "completed"
    assert update["content"] == [{"type": "content", "content": {"type": "text", "text": "wrote 4 pages"}}]


async def test_a_failing_preview_marks_the_call_failed(wired):
    """The registry's convention is that a failed tool's model-facing text starts
    with ``Error``; spine's ToolEvent carries no ok flag, so that convention is
    the only source."""
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="c1",
            result_preview="Error: no such file",
            source=_src(),
            conversation_id=SESSION_ID,
        )
    )
    assert frames.updates()[0]["status"] == "failed"


async def test_a_truncated_preview_says_so(wired):
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="c1",
            result_preview="a lot",
            truncated=True,
            source=_src(),
            conversation_id=SESSION_ID,
        )
    )
    assert frames.updates()[0]["content"][0]["content"]["text"] == "a lot\n[truncated]"


async def test_an_empty_preview_carries_no_content_key(wired):
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(
        ToolEvent(phase=ToolPhase.COMPLETE, tool_call_id="c1", source=_src(), conversation_id=SESSION_ID)
    )
    assert "content" not in frames.updates()[0]


# -- credentials do not reach the transcript ----------------------------------


async def test_a_result_preview_arrives_redacted(wired):
    """``read_file`` is not disabled and has no workspace fence, so one read of a
    credentials file would otherwise publish it into a transcript the client
    persists."""
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="c1",
            result_preview="[default]\naws_secret_access_key = wJalrXUtnFEMI-K7MDENG-bPxRfiCY\n",
            source=_src(),
            conversation_id=SESSION_ID,
        )
    )
    text = frames.updates()[0]["content"][0]["content"]["text"]
    assert "wJalrXUtnFEMI" not in text
    assert REPLACEMENT in text
    # The label survives, so the reader still learns which credential was read.
    assert "aws_secret_access_key" in text


async def test_a_credential_is_scanned_before_the_preview_is_cut(wired):
    """A value straddling the cap: cutting first leaves too few characters for the
    pattern to match, and the head of the credential then rides out as ordinary
    text."""
    _sessions, _session, frames, outlet = wired
    label = 'password="'
    # Placed so that exactly three characters of the value sit above the cap --
    # below the six the pattern needs, which is what makes the order observable.
    preview = "x" * (MAX_RESULT_PREVIEW - len(label) - 3) + label + "hunter2secretvalue"
    assert "hun" in redact(preview[:MAX_RESULT_PREVIEW]), "the other order would publish the head"

    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="c1",
            result_preview=preview,
            source=_src(),
            conversation_id=SESSION_ID,
        )
    )

    text = frames.updates()[0]["content"][0]["content"]["text"]
    assert "hun" not in text
    assert text.endswith("[truncated]")


async def test_a_tool_title_arrives_redacted(wired):
    """For ``exec`` the title is the command line, and a command line carries
    header tokens."""
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.START,
            tool_call_id="c1",
            name="exec",
            arguments={"command": 'curl -H "Authorization: Bearer sk-ant-api03-AAAABBBBCCCCDDDD" https://x'},
            source=_src(),
            conversation_id=SESSION_ID,
        )
    )
    title = frames.updates()[0]["title"]
    assert "sk-ant-api03" not in title
    assert f"Authorization: Bearer {REPLACEMENT}" in title


# -- media --------------------------------------------------------------------


async def test_media_becomes_one_resource_link_per_file(wired, tmp_path):
    _sessions, _session, frames, outlet = wired
    deck = tmp_path / "job" / "out" / "deck.pptx"
    await outlet.deliver(
        MediaOut(
            media=(Media(path=str(deck), mime="application/x-pptx", kind="file"),),
            source=_src(),
            conversation_id=SESSION_ID,
        )
    )
    update = frames.updates()[0]
    assert update["sessionUpdate"] == "agent_message_chunk"
    assert update["content"] == {
        "type": "resource_link",
        "uri": deck.as_uri(),
        "name": "deck.pptx",
        "mimeType": "application/x-pptx",
    }


async def test_a_relative_media_path_with_a_cwd_is_resolved(wired):
    _sessions, session, frames, outlet = wired
    await outlet.deliver(
        MediaOut(media=(Media(path="out/d.pptx", mime="", kind="file"),), source=_src(), conversation_id=SESSION_ID)
    )
    assert frames.updates()[0]["content"]["uri"] == (Path(session.root) / "out/d.pptx").as_uri()


def test_a_resource_link_omits_an_unknown_mime():
    assert "mimeType" not in resource_link("/tmp/a.bin")


# -- eaten events -------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        Notice(kind=NoticeKind.PROGRESS, detail="working", source=_src(), conversation_id=SESSION_ID),
        Notice(kind=NoticeKind.TOOL_HINT, detail="about to build", source=_src(), conversation_id=SESSION_ID),
        EpisodeStart(index=2, source=_src(), conversation_id=SESSION_ID),
    ],
)
async def test_events_with_no_acp_counterpart_are_eaten(wired, event):
    _sessions, _session, frames, outlet = wired
    await outlet.deliver(event)
    assert frames.frames == []


async def test_an_update_for_a_released_session_is_dropped(wired):
    """A frame naming a sessionId the client has closed is a protocol violation,
    not a late delivery."""
    sessions, _session, frames, outlet = wired
    await sessions.release(SESSION_ID)
    await outlet.deliver(Text(content="too late", source=_src(), conversation_id=SESSION_ID))
    assert frames.frames == []


# -- usage --------------------------------------------------------------------


def test_usage_needs_both_window_numbers():
    # An update of zeroes is not the same statement as no update: a size of zero
    # has a client drawing a full bar or dividing by it.
    assert usage_update({"context_used": 10, "context_max": 0}) is None
    assert usage_update({"context_used": 10}) is None
    assert usage_update(None) is None
    assert usage_update({"context_used": 10, "context_max": 100}) == {
        "sessionUpdate": "usage_update",
        "used": 10,
        "size": 100,
    }


def test_usage_carries_a_cost_when_there_is_one():
    assert usage_update({"context_used": 1, "context_max": 2, "cost_usd": 0.25})["cost"] == {
        "amount": 0.25,
        "currency": "USD",
    }


# -- the sink: a turn's end answers the prompt --------------------------------


class _ScriptedLoop:
    """A fake AgentLoop whose run_turn emits scripted spine events."""

    def __init__(self, events=(), usage=None, fail: str | None = None) -> None:
        self._events = list(events)
        self._usage = usage or {}
        self._fail = fail

    async def run_turn(self, req, emit, drain, **kwargs):
        self.last_kwargs = kwargs
        for event in self._events:
            await emit(event)
        if kwargs.get("usage_sink") is not None:
            kwargs["usage_sink"].update(self._usage)
        if self._fail is not None:
            raise RuntimeError(self._fail)
        return TurnOutcome(usage=Usage(1, 2, 3), explicit_reply=True)


def _request() -> TurnRequest:
    return TurnRequest(origin=Origin.USER, source=_src(), text="build a deck", conversation=SESSION_ID)


async def test_the_runner_streams_and_fills_the_usage_sink(wired):
    sessions, session, frames, _outlet = wired
    loop = _ScriptedLoop(
        events=[StreamDelta(delta="hello", source=_src(), conversation_id=SESSION_ID)],
        usage={"context_used": 40, "context_max": 100, "cost_usd": 0.5},
    )
    scheduler, _hub, _outlet2, teardown = build_acp(loop, frames, sessions)
    try:
        future = session.begin_turn()
        scheduler.submit(_request())
        assert await asyncio.wait_for(future, SETTLE_TIMEOUT_S) == "end_turn"
    finally:
        await teardown()
    # Streaming is the point of this transport, so the runner must ask for it.
    assert loop.last_kwargs["stream"] is True
    # The usage frame rides out before the client is told the turn ended, so the
    # cost is known while the turn is still on screen.
    assert frames.kinds() == ["agent_message_chunk", "usage_update"]
    assert frames.updates()[-1] == {
        "sessionUpdate": "usage_update",
        "used": 40,
        "size": 100,
        "cost": {"amount": 0.5, "currency": "USD"},
    }


async def test_every_update_is_on_the_wire_before_the_stop_reason(wired):
    """The render barrier: a client that closes its stream on the response must
    not drop the chunks queued behind it."""
    sessions, session, frames, _outlet = wired
    loop = _ScriptedLoop(
        events=[
            Reasoning(content="thinking", source=_src(), conversation_id=SESSION_ID),
            ToolEvent(
                phase=ToolPhase.START, tool_call_id="c1", name="ppt_build", source=_src(), conversation_id=SESSION_ID
            ),
            StreamDelta(delta="done", source=_src(), conversation_id=SESSION_ID),
        ]
    )
    scheduler, _hub, _outlet2, teardown = build_acp(loop, frames, sessions)
    try:
        future = session.begin_turn()
        scheduler.submit(_request())
        assert await asyncio.wait_for(future, SETTLE_TIMEOUT_S) == "end_turn"
    finally:
        await teardown()
    assert frames.kinds() == ["agent_thought_chunk", "tool_call", "agent_message_chunk"]


async def test_a_failed_turn_says_why_and_still_ends_the_turn(wired):
    """A prompt is never answered with a JSON-RPC error: the failure is message
    content and the turn still resolves with a stopReason."""
    sessions, session, frames, _outlet = wired
    scheduler, _hub, _outlet2, teardown = build_acp(_ScriptedLoop(fail="the model refused"), frames, sessions)
    try:
        future = session.begin_turn()
        scheduler.submit(_request())
        assert await asyncio.wait_for(future, SETTLE_TIMEOUT_S) == "end_turn"
    finally:
        await teardown()
    assert frames.kinds() == ["agent_message_chunk"]
    assert "the model refused" in frames.updates()[0]["content"]["text"]


async def test_a_failed_turn_message_is_redacted(wired):
    sessions, session, frames, _outlet = wired
    scheduler, _hub, _outlet2, teardown = build_acp(
        _ScriptedLoop(fail="POST /v1 failed with api_key=sk-proj-abcdefghijklmnop"), frames, sessions
    )
    try:
        future = session.begin_turn()
        scheduler.submit(_request())
        assert await asyncio.wait_for(future, SETTLE_TIMEOUT_S) == "end_turn"
    finally:
        await teardown()
    text = frames.updates()[0]["content"]["text"]
    assert "sk-proj-abcdefghijklmnop" not in text
    assert REPLACEMENT in text


async def test_a_cancelled_turn_ends_as_cancelled_with_no_explanation(wired):
    """The client asked for the cancel; a message explaining it would be a second
    answer to its own request."""
    sessions, session, frames, _outlet = wired
    scheduler, _hub, _outlet2, teardown = build_acp(_ScriptedLoop(), frames, sessions)
    try:
        future = session.begin_turn()
        scheduler.submit(_request())
        scheduler.cancel_conversation(SESSION_ID)
        session.settle("cancelled")
        assert await asyncio.wait_for(future, SETTLE_TIMEOUT_S) == "cancelled"
    finally:
        await teardown()
    assert "agent_message_chunk" not in frames.kinds()


async def test_settling_twice_is_a_no_op(wired):
    """A cancel followed by the sink's own failure event is the normal shape, not
    a bug -- so the second settle must not raise inside a delivery worker."""
    _sessions, session, _frames, _outlet = wired
    future = session.begin_turn()
    assert session.settle("cancelled") is True
    assert session.settle("end_turn") is False
    assert await asyncio.wait_for(future, SETTLE_TIMEOUT_S) == "cancelled"


async def test_a_second_prompt_on_one_session_is_refused(wired):
    from raven.acp.session import TurnAlreadyRunningError

    _sessions, session, _frames, _outlet = wired
    session.begin_turn()
    with pytest.raises(TurnAlreadyRunningError):
        session.begin_turn()
