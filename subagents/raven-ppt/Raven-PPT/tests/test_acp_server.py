"""The connection loop, over an in-memory pipe.

No subprocess and no network: the reader is fed bytes directly and the writer is
a buffer, so what is exercised is the framing, the one-task-per-frame dispatch
that keeps ``session/cancel`` reachable during a prompt, and the shutdown that
answers rather than cancels.
"""

import asyncio
import json

import pytest

from raven.acp import protocol
from raven.acp.server import serve
from raven.acp.stdio import MAX_FRAME_BYTES, read_frames

PIPE_TIMEOUT_S = 10.0


class Sink:
    """A writer that records whole frames, standing in for the claimed stdout."""

    def __init__(self) -> None:
        self.raw = bytearray()

    def write(self, data: bytes) -> int:
        self.raw.extend(data)
        return len(data)

    def flush(self) -> None:
        return None

    def frames(self) -> list[dict]:
        return [json.loads(line) for line in self.raw.decode("utf-8").splitlines() if line.strip()]

    def replies(self) -> dict:
        return {f["id"]: f for f in self.frames() if "id" in f and f["id"] is not None}


class _Engine:
    """An engine whose turn blocks until it is cancelled or released."""

    def __init__(self, session):
        self.session = session
        self.outlet = _Outlet()
        self.scheduler = _Scheduler(session)
        self.torn_down = False

    async def teardown(self) -> None:
        self.torn_down = True


class _Outlet:
    def say(self, *_args, **_kwargs) -> None:
        return None

    def send_media(self, *_args, **_kwargs) -> None:
        return None


class _Scheduler:
    def __init__(self, session):
        self._session = session
        self.cancelled: list[str] = []

    def submit(self, _req):
        return None

    def cancel_conversation(self, conversation_id: str) -> int:
        self.cancelled.append(conversation_id)
        return 1


def _feed(reader: asyncio.StreamReader, *frames: dict) -> None:
    for frame in frames:
        reader.feed_data(protocol.encode(frame))


async def _serve(tmp_path, *frames, engines: list | None = None, eof: bool = True):
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    sink = Sink()
    made = engines if engines is not None else []

    async def factory(session):
        engine = _Engine(session)
        made.append(engine)
        return engine

    _feed(reader, *frames)
    if eof:
        reader.feed_eof()
    await asyncio.wait_for(
        serve(reader, sink, jobs_root=tmp_path / "jobs", config=None, engine_factory=factory),
        PIPE_TIMEOUT_S,
    )
    return sink, made


@pytest.fixture
def cwd(tmp_path):
    path = tmp_path / "client"
    path.mkdir()
    return str(path)


async def test_a_finite_input_gets_every_reply(tmp_path, cwd):
    """A scripted client piping three frames and closing must be answered.
    Cancelling handlers on EOF instead would answer a batch of three with
    nothing at all."""
    sink, _ = await _serve(
        tmp_path,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": cwd, "mcpServers": []}},
        {"jsonrpc": "2.0", "id": 3, "method": "session/list", "params": {}},
    )
    replies = sink.replies()
    assert set(replies) == {1, 2, 3}
    assert replies[1]["result"]["protocolVersion"] == protocol.PROTOCOL_VERSION
    assert replies[2]["result"]["sessionId"].startswith("acp:")
    assert replies[3]["error"]["code"] == protocol.METHOD_NOT_FOUND


async def test_every_session_is_released_when_the_client_leaves(tmp_path, cwd):
    """A process that exits holding an engine leaves the next launch to fight for
    the MCP subprocesses and the memory backend's client."""
    engines: list = []
    await _serve(
        tmp_path,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": cwd, "mcpServers": []}},
        engines=engines,
    )
    assert len(engines) == 1
    assert engines[0].torn_down is True


async def test_a_cancel_is_read_while_a_prompt_is_still_suspended(tmp_path, cwd):
    """The one thing the protocol requires to always work. Handling frames inline
    would make it unreachable: the prompt is suspended for as long as the turn
    takes, which for a deck is tens of minutes."""
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    sink = Sink()
    engines: list = []
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")

    async def factory(session):
        engine = _Engine(session)
        engines.append(engine)
        return engine

    served = asyncio.create_task(serve(reader, sink, jobs_root=tmp_path / "jobs", config=None, engine_factory=factory))
    _feed(
        reader,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": cwd, "mcpServers": []}},
    )
    while not engines:
        await asyncio.sleep(0)
    session_id = engines[0].session.session_id

    # The prompt suspends: this scheduler never settles the turn on its own.
    _feed(
        reader,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": str(notes)}]},
        },
    )
    while engines[0].session.turn is None:
        await asyncio.sleep(0)
    assert 3 not in sink.replies()

    _feed(reader, {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})
    reader.feed_eof()
    await asyncio.wait_for(served, PIPE_TIMEOUT_S)

    assert engines[0].scheduler.cancelled == [session_id]
    assert sink.replies()[3]["result"] == {"stopReason": "cancelled"}


async def test_a_prompt_still_running_at_eof_is_answered_as_cancelled(tmp_path, cwd):
    """What the spec says a torn-down turn resolves as. Releasing the sessions
    settles the future, which is what lets the handler return through its own code
    rather than be cancelled mid-flight."""
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    sink = Sink()
    engines: list = []
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")

    async def factory(session):
        engine = _Engine(session)
        engines.append(engine)
        return engine

    served = asyncio.create_task(serve(reader, sink, jobs_root=tmp_path / "jobs", config=None, engine_factory=factory))
    _feed(
        reader,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": cwd, "mcpServers": []}},
    )
    while not engines:
        await asyncio.sleep(0)
    session_id = engines[0].session.session_id
    _feed(
        reader,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": str(notes)}]},
        },
    )
    while engines[0].session.turn is None:
        await asyncio.sleep(0)

    reader.feed_eof()
    await asyncio.wait_for(served, PIPE_TIMEOUT_S)
    assert sink.replies()[3]["result"] == {"stopReason": "cancelled"}


# -- framing -----------------------------------------------------------------


async def test_malformed_input_is_answered_rather_than_raised(tmp_path):
    """An agent that dies on bad input leaves every pending request unresolved,
    and a client whose promise never settles has nothing to show and nothing to
    retry."""
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    reader.feed_data(b"not json at all\n")
    reader.feed_data(b"[1, 2, 3]\n")
    reader.feed_data(b"\xff\xfe not utf-8\n")
    reader.feed_data(b'{"jsonrpc":"2.0","id":9,"method":"initialize"}\n')
    reader.feed_eof()

    errors: list[dict] = []
    frames = [frame async for frame in read_frames(reader, errors.append)]
    assert [f["id"] for f in frames] == [9]
    assert [e["error"]["code"] for e in errors] == [protocol.PARSE_ERROR] * 3
    # A null id, because the id lived in the bytes that could not be read.
    assert all(e["id"] is None for e in errors)


async def test_an_oversized_frame_resynchronises_the_stream(tmp_path):
    """Without dropping through the newline, reading resumes inside the discarded
    frame and every frame after it is garbage too."""
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    reader.feed_data(b'{"pad":"' + b"x" * 300 + b'"}\n')
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    reader.feed_eof()

    errors: list[dict] = []
    frames = [frame async for frame in read_frames(reader, errors.append, max_frame_bytes=64)]
    assert [f["id"] for f in frames] == [1]
    assert errors[0]["error"]["code"] == protocol.INVALID_REQUEST


async def test_a_frame_truncated_by_a_dying_client_is_not_acted_on(tmp_path):
    """Guessing at a partial frame is how half a tool call gets executed."""
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"sess')
    reader.feed_eof()
    errors: list[dict] = []
    assert [frame async for frame in read_frames(reader, errors.append)] == []
    assert errors == []


async def test_blank_lines_are_not_frames(tmp_path):
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    reader.feed_data(b'\n   \n{"jsonrpc":"2.0","id":1,"method":"initialize"}\n\n')
    reader.feed_eof()
    errors: list[dict] = []
    frames = [frame async for frame in read_frames(reader, errors.append)]
    assert [f["id"] for f in frames] == [1]
    assert errors == []


def test_a_frame_is_one_line_and_keeps_its_non_ascii(tmp_path):
    """``ensure_ascii=False`` so a non-ASCII prompt is not inflated into escapes,
    and no embedded newline can appear because json escapes them -- which is what
    makes line framing safe at all."""
    raw = protocol.encode({"text": "季度报告\nsecond line"})
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert "季度报告" in raw.decode("utf-8")
    assert json.loads(raw.decode("utf-8"))["text"] == "季度报告\nsecond line"


async def test_a_prompt_whose_handler_starts_after_eof_is_answered_at_once(tmp_path, cwd):
    """The latch. A frame read before EOF whose handler had not run yet would open
    a turn nothing will ever settle, and then sit there for the whole shutdown
    grace period -- so a client that closes stdin would see the process hang."""
    from raven.acp.session import AcpSession, SessionTable

    sessions = SessionTable()
    session = AcpSession(session_id="acp:s1", cwd=cwd, root=tmp_path / "job")
    sessions.add(session)
    assert sessions.settle_all() == 0
    assert session.closing is True
    assert await asyncio.wait_for(session.begin_turn(), 1) == "cancelled"


async def test_settle_all_leaves_the_engine_alone(tmp_path, cwd):
    """It is the first of three shutdown steps: the handler it releases still reads
    the engine on its way out, so the teardown cannot have happened yet."""
    from raven.acp.session import AcpSession, SessionTable

    sessions = SessionTable()
    session = AcpSession(session_id="acp:s1", cwd=cwd, root=tmp_path / "job")
    engine = _Engine(session)
    session.engine = engine
    sessions.add(session)
    future = session.begin_turn()

    assert sessions.settle_all() == 1
    assert await asyncio.wait_for(future, 1) == "cancelled"
    assert engine.torn_down is False
    assert session.engine is engine

    await sessions.aclose()
    assert engine.torn_down is True
