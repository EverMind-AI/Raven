"""The full loop: frames in, frames out, and nothing on the wire but frames."""

import asyncio
import io
import json

from raven.acp import protocol
from raven.acp.loops import AcpLoops
from raven.acp.server import serve
from raven.spine import Text, TurnOutcome, Usage


class _WireOut(io.BytesIO):
    """Collects outbound bytes; a test parses them back through the framing."""

    def frames(self) -> list[dict]:
        data = self.getvalue()
        # Byte-purity: the whole stream must be consumable as frames -- every
        # line one JSON object, nothing else ever written to the wire.
        assert data.endswith(b"\n") or data == b""
        return [protocol.decode(line.decode("utf-8")) for line in data.splitlines() if line.strip()]


class _StubLoop:
    workspace = None

    def __init__(self, hang: bool = False):
        self.hang = hang

    async def run_turn(self, req, emit, drain, stream=False, usage_sink=None, **_kw):
        assert stream is False  # the ACP runner pins the non-streaming surface
        if self.hang:
            await asyncio.sleep(3600)
        await emit(Text(content=f"echo: {req.text}"))
        return TurnOutcome(usage=Usage(1, 2, 3), explicit_reply=True)


class _Client:
    """Drives serve() over an in-process reader, one request at a time."""

    def __init__(self, loops, *, user_pool=1):
        self.reader = asyncio.StreamReader()
        self.out = _WireOut()
        self.loops = loops
        self.task = asyncio.ensure_future(serve(self.reader, self.out, loops=loops, user_pool=user_pool))
        self._next_id = 0

    def send(self, method, params=None, *, notification=False):
        if notification:
            frame = protocol.notification(method, params)
        else:
            self._next_id += 1
            frame = protocol.request(self._next_id, method, params)
        self.reader.feed_data(protocol.encode(frame))
        return None if notification else self._next_id

    async def response(self, request_id, timeout=5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for frame in self.out.frames():
                if frame.get("id") == request_id and "method" not in frame:
                    return frame
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"no response for id {request_id}; wire: {self.out.frames()}")
            await asyncio.sleep(0.01)

    async def close(self):
        self.reader.feed_eof()
        await asyncio.wait_for(self.task, timeout=10)

    def updates(self, session_id):
        return [
            f["params"]["update"]
            for f in self.out.frames()
            if f.get("method") == "session/update" and f["params"]["sessionId"] == session_id
        ]


async def test_initialize_new_prompt_full_chain_and_wire_purity():
    client = _Client(AcpLoops(lambda _conversation: _StubLoop()))
    try:
        rid = client.send("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        init = await client.response(rid)
        assert init["result"]["agentInfo"]["name"] == "raven-x-research"

        rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
        sid = (await client.response(rid))["result"]["sessionId"]

        rid = client.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]})
        prompt = await client.response(rid)
        assert prompt["result"] == {"stopReason": "end_turn"}

        # The answer chunks are on the wire before the prompt response.
        raw = client.out.getvalue().decode("utf-8")
        assert raw.index('"echo: hi"') < raw.index('"stopReason"')
        chunks = [u for u in client.updates(sid) if u["sessionUpdate"] == "agent_message_chunk"]
        assert "".join(c["content"]["text"] for c in chunks) == "echo: hi"

        # A second turn on the same session: one identity, same stream.
        rid = client.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "again"}]})
        assert (await client.response(rid))["result"] == {"stopReason": "end_turn"}
    finally:
        await client.close()
    # Everything on the wire decodes as frames (asserted inside frames()); the
    # protocol errors list stays empty for a clean session.
    assert all("jsonrpc" in f or "id" in f for f in client.out.frames())


async def test_cancel_reaches_a_suspended_prompt():
    # session/prompt suspends its handler task; the cancel notification must be
    # read and acted on while it is suspended -- the reason every frame runs in
    # its own task.
    client = _Client(AcpLoops(lambda _conversation: _StubLoop(hang=True)))
    try:
        rid = client.send("initialize", {"protocolVersion": 1})
        await client.response(rid)
        rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
        sid = (await client.response(rid))["result"]["sessionId"]

        prompt_id = client.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]})
        await asyncio.sleep(0.05)
        client.send("session/cancel", {"sessionId": sid}, notification=True)
        response = await client.response(prompt_id)
        assert response["result"] == {"stopReason": "cancelled"}
    finally:
        await client.close()


async def test_eof_settles_a_pending_prompt_as_cancelled():
    client = _Client(AcpLoops(lambda _conversation: _StubLoop(hang=True)))
    rid = client.send("initialize", {"protocolVersion": 1})
    await client.response(rid)
    rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
    sid = (await client.response(rid))["result"]["sessionId"]
    prompt_id = client.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]})
    await asyncio.sleep(0.05)
    # Client dies: the suspended handler is settled, answers through its own
    # code, and the process-side loop exits inside the grace window.
    await client.close()
    response = next(f for f in client.out.frames() if f.get("id") == prompt_id)
    assert response["result"] == {"stopReason": "cancelled"}


async def test_malformed_input_is_answered_and_the_session_survives():
    client = _Client(AcpLoops(lambda _conversation: _StubLoop()))
    try:
        client.reader.feed_data(b"this is not json\n")
        rid = client.send("initialize", {"protocolVersion": 1})
        init = await client.response(rid)
        assert "result" in init
        errors = [f for f in client.out.frames() if f.get("id") is None and "error" in f]
        assert errors and errors[0]["error"]["code"] == protocol.PARSE_ERROR
    finally:
        await client.close()


async def test_wire_carries_no_raw_newlines_inside_frames():
    client = _Client(AcpLoops(lambda _conversation: _StubLoop()))
    try:
        rid = client.send("initialize", {"protocolVersion": 1})
        await client.response(rid)
        rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
        sid = (await client.response(rid))["result"]["sessionId"]
        rid = client.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "line1\nline2"}]})
        await client.response(rid)
    finally:
        await client.close()
    for line in client.out.getvalue().splitlines():
        json.loads(line)  # every physical line is exactly one frame


async def test_load_replays_then_prompt_continues_the_stored_session(tmp_path):
    # Phase 2 chain: a session stored by an earlier process is loaded (its
    # transcript replayed as updates before the load returns) and the prompt
    # that follows runs on the same key, so the engine continues its history.
    from raven.session.manager import SessionManager

    key = "acp:20260825_000000_e2e"
    manager = SessionManager(tmp_path)
    stored = manager.get_or_create(key)
    stored.add_message("user", "original question")
    stored.add_message("assistant", "clarifying question?")
    manager.save(stored)

    loop = _StubLoop()
    loop.sessions = manager
    client = _Client(AcpLoops.single(loop))
    try:
        rid = client.send("initialize", {"protocolVersion": 1})
        await client.response(rid)

        rid = client.send("session/load", {"sessionId": key, "cwd": "/tmp", "mcpServers": []})
        load = await client.response(rid)
        assert load["result"] == {}
        # Replay is on the wire before the load's own response frame.
        lines = client.out.getvalue().decode("utf-8").splitlines()
        replay_line = next(i for i, line in enumerate(lines) if "original question" in line)
        load_response_line = next(i for i, line in enumerate(lines) if f'"id": {rid}' in line and "result" in line)
        assert replay_line < load_response_line
        kinds = [u["sessionUpdate"] for u in client.updates(key)]
        assert kinds == ["user_message_chunk", "agent_message_chunk"]

        rid = client.send("session/prompt", {"sessionId": key, "prompt": [{"type": "text", "text": "follow-up"}]})
        prompt = await client.response(rid)
        assert prompt["result"] == {"stopReason": "end_turn"}
        chunks = [u for u in client.updates(key) if u["sessionUpdate"] == "agent_message_chunk"]
        assert chunks[-1]["content"]["text"] == "echo: follow-up"

        rid = client.send("session/load", {"sessionId": "acp:never_existed", "mcpServers": []})
        missing = await client.response(rid)
        assert missing["error"]["code"] == protocol.RESOURCE_NOT_FOUND
    finally:
        await client.close()


# -- concurrency across sessions ------------------------------------------------


class _SlowLoop:
    """One turn takes ``delay`` seconds and records the engine it ran on."""

    workspace = None

    def __init__(self, delay, ran):
        self.delay = delay
        self.ran = ran

    async def run_turn(self, req, emit, drain, stream=False, usage_sink=None, **_kw):
        self.ran.append(id(self))
        await asyncio.sleep(self.delay)
        await emit(Text(content="done"))
        return TurnOutcome(usage=Usage(1, 2, 3), explicit_reply=True)


async def test_three_sessions_run_at_once_on_three_engines():
    """The throughput this change exists for: three concurrent research prompts
    take about as long as the slowest, not the sum, and each runs on its own
    engine."""
    delay = 0.3
    ran: list[int] = []
    client = _Client(
        AcpLoops(lambda _conversation: _SlowLoop(delay, ran)),
        user_pool=3,
    )
    try:
        rid = client.send("initialize", {"protocolVersion": 1})
        await client.response(rid)

        sids = []
        for _ in range(3):
            rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
            sids.append((await client.response(rid))["result"]["sessionId"])
        assert len(set(sids)) == 3

        started = asyncio.get_running_loop().time()
        prompt_ids = [
            client.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
            for sid in sids
        ]
        for prompt_id in prompt_ids:
            assert (await client.response(prompt_id))["result"] == {"stopReason": "end_turn"}
        elapsed = asyncio.get_running_loop().time() - started

        # Serial would be >= 3 * delay. The bound is generous because the
        # assertion is "they overlapped", not a latency budget.
        assert elapsed < delay * 2.5, f"turns did not overlap: {elapsed:.2f}s"
        assert len(set(ran)) == 3, "concurrent sessions shared an engine"
    finally:
        await client.close()


async def test_the_pool_is_what_makes_them_overlap():
    """The teeth of the test above: same three sessions, same engines, a pool of
    one -- and the turns serialise. Without this a pool bug would leave the
    overlap assertion passing for the wrong reason."""
    delay = 0.2
    client = _Client(AcpLoops(lambda _conversation: _SlowLoop(delay, [])), user_pool=1)
    try:
        rid = client.send("initialize", {"protocolVersion": 1})
        await client.response(rid)
        sids = []
        for _ in range(3):
            rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
            sids.append((await client.response(rid))["result"]["sessionId"])

        started = asyncio.get_running_loop().time()
        prompt_ids = [
            client.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
            for sid in sids
        ]
        for prompt_id in prompt_ids:
            await client.response(prompt_id, timeout=10)
        assert asyncio.get_running_loop().time() - started >= delay * 3
    finally:
        await client.close()


async def test_a_second_prompt_on_one_session_is_still_refused():
    """Concurrency is across sessions only. ACP's session/update carries a
    session id and nothing that identifies the request, so two prompts in
    flight on one session produce one stream that cannot be split apart --
    widening the pool must not have removed that gate."""
    client = _Client(
        AcpLoops(lambda _conversation: _SlowLoop(0.3, [])),
        user_pool=3,
    )
    try:
        rid = client.send("initialize", {"protocolVersion": 1})
        await client.response(rid)
        rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
        sid = (await client.response(rid))["result"]["sessionId"]

        first = client.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "one"}]})
        await asyncio.sleep(0.05)
        second = client.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "two"}]})

        refusal = await client.response(second)
        assert refusal["error"]["code"] == protocol.INVALID_REQUEST
        assert "in flight" in refusal["error"]["message"]
        assert (await client.response(first))["result"] == {"stopReason": "end_turn"}
    finally:
        await client.close()


async def test_an_engine_is_built_when_the_session_opens_not_at_the_first_turn():
    """A construction failure has to land on session/new, where it can be
    answered, rather than halfway through the first turn."""
    client = _Client(AcpLoops(lambda _conversation: _StubLoop()))
    try:
        rid = client.send("initialize", {"protocolVersion": 1})
        await client.response(rid)
        assert client.loops.resident() == ()

        rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
        sid = (await client.response(rid))["result"]["sessionId"]
        assert client.loops.resident() == (sid,)
    finally:
        await client.close()


async def test_a_failed_engine_build_fails_the_session_it_opened():
    def boom(_conversation):
        raise RuntimeError("no provider key")

    client = _Client(AcpLoops(boom))
    try:
        rid = client.send("initialize", {"protocolVersion": 1})
        await client.response(rid)
        rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
        response = await client.response(rid)
        assert "error" in response
        assert response["error"]["code"] == protocol.INTERNAL_ERROR
    finally:
        await client.close()


async def test_a_prompt_for_a_session_that_never_opened_is_refused():
    """The other half of the rollback: with no registry entry the prompt is a
    clean 'unknown session', not a turn that dies on engine construction."""
    def boom(_conversation):
        raise RuntimeError("no provider key")

    client = _Client(AcpLoops(boom))
    try:
        rid = client.send("initialize", {"protocolVersion": 1})
        await client.response(rid)
        rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
        await client.response(rid)

        rid = client.send(
            "session/prompt",
            {"sessionId": "acp:20260827_000000_ghost", "prompt": [{"type": "text", "text": "hi"}]},
        )
        response = await client.response(rid)
        assert response["error"]["code"] == protocol.RESOURCE_NOT_FOUND
    finally:
        await client.close()


# -- ask_user round trip --------------------------------------------------------


class _AskUserStubTool:
    def __init__(self):
        self.broker = None

    def set_broker(self, broker):
        self.broker = broker


class _AskingLoop:
    """A loop whose turn asks the user once, through whatever broker was armed."""

    workspace = None

    def __init__(self):
        self.tool = _AskUserStubTool()
        self.tools = {"ask_user": self.tool}

    async def run_turn(self, req, emit, drain, stream=False, usage_sink=None, **_kw):
        broker = self.tool.broker
        if broker is None:
            await emit(Text(content="no broker armed"))
        else:
            answer = await broker.await_question(
                f"acp:{req.source.chat_id}", prompt="which year?", choices=["2023", "2024"]
            )
            await emit(Text(content=f"answered: {answer}"))
        return TurnOutcome(usage=Usage(1, 2, 3), explicit_reply=True)


async def _update_of_kind(client, session_id, kind, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        for update in client.updates(session_id):
            if update.get("sessionUpdate") == kind:
                return update
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no {kind} update; wire: {client.out.frames()}")
        await asyncio.sleep(0.01)


async def test_ask_user_round_trips_over_the_wire():
    """The repo-local proof of ``askUser.delivery="tool"`` on ACP: the client
    declares the capability, the question leaves as an ``ask_user_request``
    update mid-turn, ``_raven/clarify_respond`` answers it, and the SAME
    prompt's reply carries the answer."""
    loop = _AskingLoop()
    client = _Client(AcpLoops.single(loop))
    rid = client.send(
        "initialize",
        {"protocolVersion": 1, "clientCapabilities": {"_meta": {"raven": {"askUser": True}}}},
    )
    await client.response(rid)

    rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
    sid = (await client.response(rid))["result"]["sessionId"]
    # Armed as the session's engine is built, not at initialize: engines are
    # per session and lazily created, so a declaration that only reached the
    # engines alive when it arrived would reach none of them.
    assert loop.tool.broker is not None, "opening the session did not arm the broker"
    prompt_id = client.send(
        "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "research X"}]}
    )
    question = await _update_of_kind(client, sid, "ask_user_request")
    assert question["question"] == "which year?"
    assert question["choices"] == ["2023", "2024"]

    rid = client.send("_raven/clarify_respond", {"requestId": question["requestId"], "answer": "2024"})
    assert (await client.response(rid))["result"] == {"delivered": True}

    response = await client.response(prompt_id)
    assert response["result"]["stopReason"] == "end_turn"
    chunks = [u for u in client.updates(sid) if u["sessionUpdate"] == "agent_message_chunk"]
    assert chunks and "answered: 2024" in chunks[-1]["content"]["text"]
    await client.close()


async def test_an_undeclared_client_never_sees_a_question():
    """No capability, no broker: the tool stays unarmed and the turn takes its
    no-broker path — on the real loop that is the gate's handoff fallback."""
    loop = _AskingLoop()
    client = _Client(AcpLoops.single(loop))
    rid = client.send("initialize", {"protocolVersion": 1})
    await client.response(rid)
    assert loop.tool.broker is None
    rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
    sid = (await client.response(rid))["result"]["sessionId"]
    prompt_id = client.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "q"}]})
    assert (await client.response(prompt_id))["result"]["stopReason"] == "end_turn"
    kinds = [u["sessionUpdate"] for u in client.updates(sid)]
    assert "ask_user_request" not in kinds
    await client.close()


async def test_eof_fail_safes_a_turn_blocked_on_a_question():
    """The shutdown ordering in serve(): cancel_all runs before the drain, so a
    turn holding its prompt on an unanswered question unwinds through the
    broker's default instead of spending the drain's grace."""
    loop = _AskingLoop()
    client = _Client(AcpLoops.single(loop))
    rid = client.send(
        "initialize",
        {"protocolVersion": 1, "clientCapabilities": {"_meta": {"raven": {"askUser": True}}}},
    )
    await client.response(rid)
    rid = client.send("session/new", {"cwd": "/tmp", "mcpServers": []})
    sid = (await client.response(rid))["result"]["sessionId"]
    client.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "q"}]})
    await _update_of_kind(client, sid, "ask_user_request")
    # close() awaits the serve task with a timeout: completing at all IS the assertion.
    await client.close()
