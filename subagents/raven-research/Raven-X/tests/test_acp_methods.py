"""Frame routing: tolerant inbound, and a stopReason on every prompt exit."""

import asyncio

from raven.acp import protocol
from raven.acp.capabilities import AGENT_NAME
from raven.acp.methods import AcpMethods, sanitise_error_data
from raven.acp.spine import AcpSessions


class _StubHandle:
    """A TurnHandle stand-in; ``unwind`` (if given) gates ``result()`` so a
    test can hold the turn half-unwound while other frames arrive."""

    def __init__(self, unwind: "asyncio.Future | None" = None):
        self.cancelled = False
        self._unwind = unwind

    def cancel(self):
        self.cancelled = True

    async def result(self):
        if self._unwind is not None:
            await self._unwind
        return None


class _Harness:
    """AcpMethods against a scripted submit."""

    def __init__(
        self,
        on_submit=None,
        session_manager=None,
        question_broker=None,
        arm_ask_user=None,
        on_session_open=None,
    ):
        self.frames: list[dict] = []
        self.sessions = AcpSessions()
        self.submitted: list = []
        self.opened: list[str] = []
        self._on_submit = on_submit
        self._on_session_open = on_session_open
        self.methods = AcpMethods(
            submit=self._submit,
            sessions=self.sessions,
            emit=self.frames.append,
            session_manager=session_manager,
            question_broker=question_broker,
            arm_ask_user=arm_ask_user,
            on_session_open=self._open,
        )

    async def _open(self, session_id):
        self.opened.append(session_id)
        if self._on_session_open is not None:
            return self._on_session_open(session_id)
        return None

    def _submit(self, req):
        self.submitted.append(req)
        if self._on_submit is not None:
            return self._on_submit(req)
        return _StubHandle()

    async def call(self, method, params=None, *, request_id=1):
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        return await self.methods.handle(frame)

    async def start(self):
        await self.call("initialize", {"protocolVersion": 1})

    async def new_session(self) -> str:
        response = await self.call("session/new", {"cwd": "/tmp", "mcpServers": []})
        return response["result"]["sessionId"]


# -- handshake ----------------------------------------------------------------


async def test_initialize_shape_and_dialect_safety():
    h = _Harness()
    response = await h.call("initialize", {"protocolVersion": 1})
    result = response["result"]
    assert result["protocolVersion"] == 1
    assert result["authMethods"] == []
    info = result["agentInfo"]
    assert info["name"] == AGENT_NAME
    # The consuming raven picks its dialect by substring match on this name; a
    # key substring would route our frames through a third-party dialect.
    assert "claude" not in info["name"].lower()
    assert "codex" not in info["name"].lower()
    caps = result["agentCapabilities"]
    assert caps["promptCapabilities"] == {"image": False, "audio": False, "embeddedContext": False}
    # Decision A (plan §6): the consuming raven's _handshake derives
    # statefulness and loadability as below (raven/agent/acp/capabilities.py
    # in the main repo); both must come out True or every task opens a fresh
    # session, silently. This is the one regression gate for that.
    can_resume = "resume" in caps.get("sessionCapabilities", {})
    can_load = bool(caps.get("loadSession"))
    assert can_resume is True
    assert can_load is True
    # Declared keys are promises: nothing beyond what is actually served.
    assert set(caps["sessionCapabilities"]) == {"resume"}


async def test_nothing_before_initialize():
    h = _Harness()
    response = await h.call("session/new", {"cwd": "/tmp"})
    assert response["error"]["code"] == protocol.INVALID_REQUEST


async def test_unknown_and_unimplemented_methods_are_answered():
    h = _Harness()
    await h.start()
    assert (await h.call("no/such"))["error"]["code"] == protocol.METHOD_NOT_FOUND
    assert (await h.call("session/list"))["error"]["code"] == protocol.METHOD_NOT_FOUND
    assert (await h.call("authenticate"))["error"]["code"] == protocol.INVALID_PARAMS


async def test_frames_with_falsy_ids_are_requests_and_bad_shapes_are_answered():
    h = _Harness()
    await h.start()
    # id: 0 is a request (presence, not truthiness) and must be answered.
    response = await h.methods.handle({"jsonrpc": "2.0", "id": 0, "method": "no/such"})
    assert response["id"] == 0
    # A notification never gets a reply, even on failure.
    assert await h.methods.handle({"jsonrpc": "2.0", "method": "no/such"}) is None
    assert (await h.methods.handle({"jsonrpc": "2.0", "id": 2, "method": 7}))["error"][
        "code"
    ] == protocol.INVALID_REQUEST
    response = await h.methods.handle({"jsonrpc": "2.0", "id": 3, "method": "session/new", "params": [1]})
    assert response["error"]["code"] == protocol.INVALID_PARAMS
    # A stray response frame is nobody's business and stays silent.
    assert await h.methods.handle({"jsonrpc": "2.0", "id": 9, "result": {}}) is None


# -- sessions -----------------------------------------------------------------


async def test_session_new_mints_channel_prefixed_id_and_ignores_cwd():
    h = _Harness()
    await h.start()
    sid = await h.new_session()
    assert sid.startswith("acp:")
    assert h.sessions.get(sid) is not None


async def test_session_new_refuses_per_session_mcp():
    h = _Harness()
    await h.start()
    response = await h.call("session/new", {"cwd": "/tmp", "mcpServers": [{"name": "x"}]})
    assert response["error"]["code"] == protocol.INVALID_PARAMS


async def test_unknown_session_is_resource_not_found():
    h = _Harness()
    await h.start()
    response = await h.call("session/prompt", {"sessionId": "acp:nope", "prompt": []})
    assert response["error"]["code"] == protocol.RESOURCE_NOT_FOUND


# -- prompt -------------------------------------------------------------------


def _text_prompt(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


async def test_prompt_submits_a_user_turn_and_answers_its_stop_reason():
    h = _Harness()
    await h.start()
    sid = await h.new_session()

    async def _drive():
        await asyncio.sleep(0.01)
        h.sessions.settle(sid, "end_turn")

    driver = asyncio.ensure_future(_drive())
    response = await h.call("session/prompt", {"sessionId": sid, "prompt": _text_prompt("hi")})
    await driver
    assert response["result"] == {"stopReason": "end_turn"}
    (req,) = h.submitted
    assert req.origin.value == "user"
    assert req.source.channel == "acp"
    assert f"acp:{req.source.chat_id}" == sid
    assert req.text == "hi"
    # The turn slot is dropped on the way out.
    assert not h.sessions.pending(sid)


async def test_empty_prompt_is_end_turn_with_an_explanatory_chunk():
    h = _Harness()
    await h.start()
    sid = await h.new_session()
    response = await h.call("session/prompt", {"sessionId": sid, "prompt": []})
    assert response["result"] == {"stopReason": "end_turn"}
    assert h.submitted == []
    # The chunkless-turn invariant: the consuming raven raises
    # AcpEmptyTurnError on a turn with no agent_message_chunk.
    chunks = [
        f["params"]["update"]
        for f in h.frames
        if f.get("method") == "session/update" and f["params"]["sessionId"] == sid
    ]
    assert [c["sessionUpdate"] for c in chunks] == ["agent_message_chunk"]
    assert "no turn was run" in chunks[0]["content"]["text"]


async def test_second_prompt_while_one_runs_is_refused():
    h = _Harness()
    await h.start()
    sid = await h.new_session()
    first = asyncio.ensure_future(h.call("session/prompt", {"sessionId": sid, "prompt": _text_prompt("a")}))
    await asyncio.sleep(0.01)
    second = await h.call("session/prompt", {"sessionId": sid, "prompt": _text_prompt("b")}, request_id=2)
    assert second["error"]["code"] == protocol.INVALID_REQUEST
    h.sessions.settle(sid, "end_turn")
    assert (await first)["result"] == {"stopReason": "end_turn"}


async def test_failed_submit_still_answers_with_a_stop_reason():
    def _boom(req):
        raise RuntimeError("scheduler is draining")

    h = _Harness(on_submit=_boom)
    await h.start()
    sid = await h.new_session()
    response = await h.call("session/prompt", {"sessionId": sid, "prompt": _text_prompt("hi")})
    # Never a JSON-RPC error: the reason lands as content, the prompt ends.
    assert response["result"] == {"stopReason": "end_turn"}
    said = h.frames[-1]["params"]["update"]
    assert said["sessionUpdate"] == "agent_message_chunk"
    assert "could not start" in said["content"]["text"]
    assert not h.sessions.pending(sid)


# -- cancel -------------------------------------------------------------------


async def test_cancel_cancels_the_handle_and_settles_the_prompt():
    h = _Harness()
    await h.start()
    sid = await h.new_session()
    prompt = asyncio.ensure_future(h.call("session/prompt", {"sessionId": sid, "prompt": _text_prompt("a")}))
    await asyncio.sleep(0.01)
    assert await h.methods.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}) is None
    response = await prompt
    assert response["result"] == {"stopReason": "cancelled"}
    handle = h.submitted and h.sessions.get(sid)
    assert handle is not None


async def test_cancel_waits_out_the_unwind_before_its_backstop_settle():
    """The prompt-hijack regression, at the methods layer: the backstop settle
    is addressed to the future cancel captured, so a next prompt armed while
    the cancelled turn was still unwinding cannot be answered by it."""
    unwind: asyncio.Future = asyncio.get_running_loop().create_future()
    h = _Harness(on_submit=lambda req: _StubHandle(unwind=unwind))
    await h.start()
    sid = await h.new_session()

    prompt_a = asyncio.ensure_future(h.call("session/prompt", {"sessionId": sid, "prompt": _text_prompt("a")}))
    await asyncio.sleep(0.01)
    cancel = asyncio.ensure_future(
        h.methods.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
    )
    await asyncio.sleep(0.01)
    assert not cancel.done(), "cancel must wait for the turn's unwind"

    # Play the sink: the cancelled turn's TurnFailed settles prompt A while
    # cancel is still waiting, then the next prompt arms.
    assert h.sessions.settle(sid, "cancelled") is True
    assert (await prompt_a)["result"] == {"stopReason": "cancelled"}
    prompt_b = asyncio.ensure_future(h.call("session/prompt", {"sessionId": sid, "prompt": _text_prompt("b")}))
    await asyncio.sleep(0.01)
    assert h.sessions.pending(sid)

    unwind.set_result(None)
    await cancel
    assert h.sessions.pending(sid), "the backstop settled a prompt it never saw"

    h.sessions.settle(sid, "end_turn")
    assert (await prompt_b)["result"] == {"stopReason": "end_turn"}


async def test_cancel_backstop_answers_a_turn_that_never_started():
    """A turn dropped from the lane queue emits no terminating event, so the
    sink never settles its prompt; cancel's own settle must."""
    h = _Harness()  # the stub's result() resolves at once, with no sink event
    await h.start()
    sid = await h.new_session()
    prompt = asyncio.ensure_future(h.call("session/prompt", {"sessionId": sid, "prompt": _text_prompt("a")}))
    await asyncio.sleep(0.01)
    await h.methods.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
    assert (await prompt)["result"] == {"stopReason": "cancelled"}


async def test_cancel_resolves_a_pending_ask_user_question_first():
    """A turn blocked on the broker: ``session/cancel`` fail-safes the pending
    question to its default BEFORE cancelling the handle, so the turn's wait
    releases whatever shape the handle's cancel takes - a bare cancel used to
    be absorbed by the broker's fail-safe and the "cancelled" turn ran on."""
    from raven.acp.questions import build_question_broker

    broker = build_question_broker(lambda _f: None)
    h = _Harness(question_broker=broker)
    await h.start()
    sid = await h.new_session()
    prompt = asyncio.ensure_future(h.call("session/prompt", {"sessionId": sid, "prompt": _text_prompt("a")}))
    await asyncio.sleep(0.01)
    question = asyncio.ensure_future(broker.await_question(sid, prompt="which year?", default="unanswered"))
    await asyncio.sleep(0.01)

    await h.methods.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
    assert await question == "unanswered"
    assert (await prompt)["result"] == {"stopReason": "cancelled"}


async def test_cancel_for_an_unknown_session_is_silent():
    h = _Harness()
    await h.start()
    assert (
        await h.methods.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "acp:gone"}})
        is None
    )


# -- prompt content -----------------------------------------------------------


async def test_prompt_blocks_are_flattened_and_degraded_blocks_named():
    h = _Harness()
    await h.start()
    sid = await h.new_session()

    async def _drive():
        await asyncio.sleep(0.01)
        h.sessions.settle(sid, "end_turn")

    blocks = [
        {"type": "text", "text": "question"},
        {"type": "resource_link", "uri": "file:///work/a.py", "name": "a.py"},
        {"type": "resource", "resource": {"uri": "file:///work/b.md", "text": "body"}},
        {"type": "resource", "resource": {"uri": "file:///work/c.bin", "blob": "AAAA"}},
        {"type": "image", "data": "AAAA", "mimeType": "image/png"},
        {"type": "audio", "data": "AAAA"},
        {"type": "mystery"},
        "not a block",
    ]
    driver = asyncio.ensure_future(_drive())
    await h.call("session/prompt", {"sessionId": sid, "prompt": blocks})
    await driver
    (req,) = h.submitted
    assert "question" in req.text
    assert "[a.py: /work/a.py]" in req.text
    assert "[/work/b.md]\nbody" in req.text
    assert "binary resource, not inlined" in req.text
    assert "image attachment" in req.text
    assert "audio attachment" in req.text


async def test_error_data_never_leaks_tracebacks_or_secrets():
    cleaned = sanitise_error_data({"traceback_tail": "File /home", "reason": "api_key=sk-abcdefghijklmnop1234"})
    assert "traceback_tail" not in cleaned
    assert "sk-abcdefghijklmnop1234" not in cleaned["reason"]


# -- session/load and session/resume (Phase 2) ---------------------------------


def _stored_manager(tmp_path, key: str):
    from raven.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    session = manager.get_or_create(key)
    session.add_message("user", "original question")
    session.add_message("assistant", "clarifying question?")
    manager.save(session)
    return manager


async def test_session_load_replays_the_transcript_then_returns(tmp_path):
    key = "acp:20260825_000000_stored"
    h = _Harness(session_manager=_stored_manager(tmp_path, key))
    await h.start()
    response = await h.call("session/load", {"sessionId": key, "cwd": "/tmp", "mcpServers": []})
    assert response["result"] == {}
    updates = [f["params"]["update"] for f in h.frames if f.get("method") == "session/update"]
    assert [u["sessionUpdate"] for u in updates] == ["user_message_chunk", "agent_message_chunk"]
    assert all(f["params"]["sessionId"] == key for f in h.frames if f.get("method") == "session/update")
    # The loaded session is promptable: it is in the live registry.
    assert h.sessions.get(key) is not None


async def test_session_resume_establishes_state_without_replaying(tmp_path):
    key = "acp:20260825_000000_stored"
    h = _Harness(session_manager=_stored_manager(tmp_path, key))
    await h.start()
    response = await h.call("session/resume", {"sessionId": key})
    assert response["result"] == {}
    assert [f for f in h.frames if f.get("method") == "session/update"] == []
    assert h.sessions.get(key) is not None


async def test_loaded_session_accepts_a_prompt_on_the_same_lane(tmp_path):
    key = "acp:20260825_000000_stored"
    h = _Harness(session_manager=_stored_manager(tmp_path, key))
    await h.start()
    await h.call("session/load", {"sessionId": key, "mcpServers": []})

    async def _drive():
        await asyncio.sleep(0.01)
        h.sessions.settle(key, "end_turn")

    driver = asyncio.ensure_future(_drive())
    response = await h.call("session/prompt", {"sessionId": key, "prompt": _text_prompt("follow-up")})
    await driver
    assert response["result"] == {"stopReason": "end_turn"}
    (req,) = h.submitted
    # The turn runs on the stored session's own key, which is what makes the
    # engine's session manager continue its history.
    assert f"acp:{req.source.chat_id}" == key


async def test_load_of_an_unknown_session_is_resource_not_found(tmp_path):
    from raven.session.manager import SessionManager

    h = _Harness(session_manager=SessionManager(tmp_path))
    await h.start()
    response = await h.call("session/load", {"sessionId": "acp:never_existed"})
    assert response["error"]["code"] == protocol.RESOURCE_NOT_FOUND


async def test_load_never_serves_another_channels_session(tmp_path):
    # The engine's manager also files cli:... conversations; serving one to an
    # ACP client would read a terminal transcript out through this surface.
    key = "cli:20260825_000000_terminal"
    h = _Harness(session_manager=_stored_manager(tmp_path, key))
    await h.start()
    response = await h.call("session/load", {"sessionId": key})
    assert response["error"]["code"] == protocol.RESOURCE_NOT_FOUND


async def test_load_without_an_engine_is_resource_not_found():
    h = _Harness(session_manager=None)
    await h.start()
    response = await h.call("session/load", {"sessionId": "acp:x"})
    assert response["error"]["code"] == protocol.RESOURCE_NOT_FOUND


async def test_load_refuses_per_session_mcp_and_requires_an_id(tmp_path):
    from raven.session.manager import SessionManager

    h = _Harness(session_manager=SessionManager(tmp_path))
    await h.start()
    assert (await h.call("session/load", {"sessionId": "acp:x", "mcpServers": [{"name": "s"}]}))["error"][
        "code"
    ] == protocol.INVALID_PARAMS
    assert (await h.call("session/load", {}))["error"]["code"] == protocol.INVALID_PARAMS


# -- ask_user round trip --------------------------------------------------------


async def test_initialize_arms_ask_user_from_the_client_declaration():
    """The methods layer knows the capability, the server knows the tool; the
    callback is the seam between them, and it follows the record in BOTH
    directions on a re-initialize."""
    armed: list[bool] = []
    h = _Harness(arm_ask_user=armed.append)
    await h.call("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {"_meta": {"raven": {"askUser": True}}},
    })
    await h.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    assert armed == [True, False]


async def test_the_declaration_is_read_defensively():
    """Absent, wrong-typed, or falsy shapes all read False: a wrong True sends a
    question to a client with no UI, silently answered by the fail-safe."""
    from raven.acp.capabilities import ClientCapabilities

    def declared(caps):
        return ClientCapabilities.from_params({"clientCapabilities": caps}).ask_user

    assert declared({"_meta": {"raven": {"askUser": True}}}) is True
    assert declared({"_meta": {"raven": {"askUser": False}}}) is False
    assert declared({"_meta": {"raven": "askUser"}}) is False
    assert declared({"_meta": "raven"}) is False
    assert declared({}) is False
    assert declared(None) is False


async def test_the_agent_advertises_the_extension_capability():
    """The mirror of the client declaration: how the consuming raven knows that
    offering its question UI is worthwhile. Under _meta so it can never collide
    with spec surface."""
    h = _Harness()
    response = await h.call("initialize", {"protocolVersion": 1})
    caps = response["result"]["agentCapabilities"]
    assert caps["_meta"]["raven"]["askUser"] is True


async def test_clarify_respond_resolves_a_pending_question():
    from raven.acp.questions import CLARIFY_RESPOND_METHOD, build_question_broker

    frames: list[dict] = []
    broker = build_question_broker(frames.append)
    h = _Harness(question_broker=broker)
    await h.start()

    question = asyncio.ensure_future(
        broker.await_question("acp:chat1", prompt="which year?", choices=["2023", "2024"])
    )
    await asyncio.sleep(0)
    request_id = frames[0]["params"]["update"]["requestId"]
    response = await h.call(CLARIFY_RESPOND_METHOD, {"requestId": request_id, "answer": "2024"})
    assert response["result"] == {"delivered": True}
    assert await question == "2024"
    # A second answer to the same handle is stale, not an error.
    response = await h.call(CLARIFY_RESPOND_METHOD, {"requestId": request_id, "answer": "2023"})
    assert response["result"] == {"delivered": False}


async def test_clarify_respond_is_tolerant_of_stale_and_missing_handles():
    from raven.acp.questions import CLARIFY_RESPOND_METHOD, build_question_broker

    h = _Harness(question_broker=build_question_broker(lambda _f: None))
    await h.start()
    for params in ({"requestId": "gone", "answer": "x"}, {"answer": "x"}, {}):
        response = await h.call(CLARIFY_RESPOND_METHOD, params)
        assert response["result"] == {"delivered": False}

    # And with no broker wired at all: same shape, not a crash.
    bare = _Harness()
    await bare.start()
    response = await bare.call(CLARIFY_RESPOND_METHOD, {"requestId": "r", "answer": "x"})
    assert response["result"] == {"delivered": False}


async def test_clarify_respond_is_gated_on_initialize():
    from raven.acp.questions import CLARIFY_RESPOND_METHOD

    h = _Harness()
    response = await h.call(CLARIFY_RESPOND_METHOD, {"requestId": "r", "answer": "x"})
    assert response["error"]["code"] == protocol.INVALID_REQUEST


# -- opening a session builds its engine ----------------------------------------


async def test_every_route_into_the_registry_opens_the_session(tmp_path):
    """new / load / resume are three ways in; an engine built on only one of them
    leaves the other two failing halfway through their first turn."""
    from raven.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    key = "acp:20260827_000000_stored"
    stored = manager.get_or_create(key)
    stored.add_message("user", "earlier")
    manager.save(stored)

    h = _Harness(session_manager=manager)
    await h.start()
    minted = await h.new_session()
    await h.call("session/load", {"sessionId": key})
    await h.call("session/resume", {"sessionId": key})

    assert h.opened == [minted, key, key]


async def test_a_session_whose_engine_cannot_be_built_is_not_left_registered():
    def boom(_session_id):
        raise RuntimeError("no provider key")

    h = _Harness(on_session_open=boom)
    await h.start()
    response = await h.call("session/new", {"cwd": "/tmp"})

    assert response["error"]["code"] == protocol.INTERNAL_ERROR
    assert h.sessions.sessions() == ()
