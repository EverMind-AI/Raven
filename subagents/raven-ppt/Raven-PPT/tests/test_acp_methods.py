"""The ACP method surface, driven as frames.

Every case here is a frame in and a frame out, against a scripted engine -- no
subprocess, no network, no agent loop. What is being pinned is the protocol
contract: which methods answer, which refuse and with which code, and the rule
that a prompt is never answered with a JSON-RPC error whatever happened to the
turn.
"""

import asyncio
import zipfile
from pathlib import Path

import pytest

from raven.acp import materials, protocol
from raven.acp.capabilities import ClientCapabilities, agent_capabilities
from raven.acp.methods import UNIMPLEMENTED_METHODS, AcpMethodError, AcpMethods, sanitise_error_data
from raven.acp.redact import REPLACEMENT
from raven.acp.session import SessionTable
from raven.acp.spine import AcpOutlet

# Every ``await`` on a session future carries a deadline. A prompt that is never
# settled is exactly the regression these assert against, and without one the
# test wedges instead of failing -- which in CI reads as a hung job rather than as
# a broken invariant.
SETTLE_TIMEOUT_S = 2.0


def _pptx(path: Path, slides: int = 3) -> Path:
    """A file that passes the deck check: a zip carrying slide parts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1, slides + 1):
            archive.writestr(f"ppt/slides/slide{index}.xml", "<sld/>")
    return path


class _Scheduler:
    """Stands in for the spine Scheduler: records the request, then plays a turn.

    The turn is played on a task because that is how the real one behaves -- the
    handler suspends on the session's future while the lane runs, and a scheduler
    that settled inside ``submit`` would never exercise the suspension.
    """

    def __init__(self, session, outlet, *, reply: str = "ok", publish: Path | None = None, stop: str = "end_turn"):
        self._session = session
        self._outlet = outlet
        self._reply = reply
        self._publish = publish
        self._stop = stop
        self.requests: list = []
        self.cancelled: list[str] = []

    def submit(self, req):
        self.requests.append(req)
        asyncio.get_running_loop().create_task(self._play())
        return None

    def cancel_conversation(self, conversation_id: str) -> int:
        self.cancelled.append(conversation_id)
        return 1

    async def _play(self) -> None:
        if self._publish is not None:
            _pptx(self._publish)
        if self._reply:
            self._outlet.say(self._session.session_id, self._reply)
        self._session.settle(self._stop)


class _Engine:
    def __init__(self, session, outlet, **kwargs):
        self.outlet = outlet
        self.scheduler = _Scheduler(session, outlet, **kwargs)
        self.torn_down = False

    async def teardown(self) -> None:
        self.torn_down = True


class Harness:
    def __init__(self, tmp_path: Path, **engine_kwargs):
        self.frames: list[dict] = []
        self.sessions = SessionTable()
        self.outlet = AcpOutlet("acp", self.frames.append, self.sessions)
        self.engines: list[_Engine] = []
        self.engine_kwargs = engine_kwargs
        self.fail_build: str | None = None
        self.jobs_root = tmp_path / "jobs"
        self.cwd = tmp_path / "client"
        self.cwd.mkdir(parents=True)
        self.methods = AcpMethods(
            emit=self.frames.append,
            sessions=self.sessions,
            engine_factory=self._factory,
            jobs_root=self.jobs_root,
        )

    async def _factory(self, session):
        if self.fail_build is not None:
            raise RuntimeError(self.fail_build)
        engine = _Engine(session, self.outlet, **self.engine_kwargs)
        self.engines.append(engine)
        return engine

    async def call(self, method: str, params: dict | None = None, request_id: int | str = 1):
        frame: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        return await self.methods.handle(frame)

    async def notify(self, method: str, params: dict | None = None):
        frame: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        return await self.methods.handle(frame)

    async def handshake(self) -> None:
        await self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})

    async def new_session(self, **params) -> str:
        await self.handshake()
        result = await self.call("session/new", {"cwd": str(self.cwd), "mcpServers": [], **params})
        return result["result"]["sessionId"]

    def updates(self) -> list[dict]:
        return [f["params"]["update"] for f in self.frames if f.get("method") == "session/update"]

    def said(self) -> str:
        return "".join(
            u["content"]["text"]
            for u in self.updates()
            if u["sessionUpdate"] == "agent_message_chunk" and u["content"].get("type") == "text"
        )


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


# -- initialize ---------------------------------------------------------------


async def test_initialize_answers_the_version_it_serves(harness):
    result = await harness.call("initialize", {"protocolVersion": 1})
    assert result["result"]["protocolVersion"] == protocol.PROTOCOL_VERSION
    assert result["result"]["authMethods"] == []
    assert result["result"]["agentInfo"]["name"] == "raven-ppt"


@pytest.mark.parametrize("requested", ["1", 1.0, 7, None, "not a number", True])
async def test_initialize_never_fails_over_the_version_field(harness, requested):
    """A client that cannot complete initialize has no surface left to be told
    why, so the handshake reads intent instead of validating."""
    result = await harness.call("initialize", {"protocolVersion": requested})
    assert "error" not in result
    assert result["result"]["protocolVersion"] == protocol.PROTOCOL_VERSION


def test_every_declared_capability_is_one_that_is_served():
    """The declaration is read by the other side and acted on: a false promise has
    a client route work into a method that answers with an error, or silently
    disable its own tools."""
    caps = agent_capabilities()
    # True since the transcript on disk is replayed as session/update frames;
    # `session/load` therefore has to answer, which the next assertion pins.
    assert caps["loadSession"] is True
    assert "session/load" not in UNIMPLEMENTED_METHODS
    # Nothing consumes an inline image or audio on the prompt side.
    assert caps["promptCapabilities"]["image"] is False
    assert caps["promptCapabilities"]["audio"] is False
    assert caps["promptCapabilities"]["embeddedContext"] is True
    # MCP is per engine, not per session.
    assert caps["mcpCapabilities"] == {"http": False, "sse": False}
    # ``resume`` travels with ``loadSession``: the consuming raven reads this key
    # for statefulness and will not look up a stored id without it, so either one
    # missing makes every task a fresh session. ``list`` / ``delete`` absent
    # because each declared one is a method that must then work.
    assert caps["sessionCapabilities"] == {"close": {}, "resume": {}}
    assert "session/resume" not in UNIMPLEMENTED_METHODS
    # And every capability named here must have no method that answers -32601.
    declared = {f"session/{name}" for name in caps["sessionCapabilities"]}
    assert declared.isdisjoint(UNIMPLEMENTED_METHODS)


async def test_nothing_is_answered_before_the_handshake(harness):
    result = await harness.call("session/new", {"cwd": str(harness.cwd)})
    assert result["error"]["code"] == protocol.INVALID_REQUEST
    assert result["error"]["data"] == {"method": "session/new"}


async def test_authenticate_says_none_is_needed(harness):
    """Not method-not-found, which a client would read as a version mismatch."""
    await harness.handshake()
    result = await harness.call("authenticate", {})
    assert result["error"]["code"] == protocol.INVALID_PARAMS
    assert result["error"]["data"] == {"authMethods": []}


@pytest.mark.parametrize("method", sorted(UNIMPLEMENTED_METHODS))
async def test_an_unsupported_method_is_refused_not_dropped(harness, method):
    """Answered rather than ignored: a client waiting on a promise nothing will
    resolve has nothing to show and nothing to retry."""
    await harness.handshake()
    result = await harness.call(method, {"sessionId": "acp:x"})
    assert result["error"]["code"] == protocol.METHOD_NOT_FOUND


async def test_an_unknown_method_is_answered(harness):
    await harness.handshake()
    result = await harness.call("nonsense/thing")
    assert result["error"]["code"] == protocol.METHOD_NOT_FOUND


# -- frame shape --------------------------------------------------------------


async def test_positional_params_are_refused(harness):
    """Legal JSON-RPC, unusable here: ACP is by-name throughout."""
    result = await harness.methods.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": [1]})
    assert result["error"]["code"] == protocol.INVALID_PARAMS


async def test_a_non_string_method_is_refused(harness):
    result = await harness.methods.handle({"jsonrpc": "2.0", "id": 1, "method": 7})
    assert result["error"]["code"] == protocol.INVALID_REQUEST


async def test_a_notification_is_never_answered(harness):
    assert await harness.notify("session/new", {"cwd": "relative"}) is None


async def test_a_request_with_a_falsy_id_is_still_answered(harness):
    """JSON-RPC says a notification is a frame with no ``id`` member, so id 0 is a
    request -- and one that goes unanswered is a hang with no diagnostic."""
    result = await harness.call("initialize", {}, request_id=0)
    assert result is not None and result["id"] == 0


async def test_a_string_id_is_answered_on_the_same_id(harness):
    result = await harness.call("initialize", {}, request_id="req-1")
    assert result["id"] == "req-1"


# -- session/load -------------------------------------------------------------


def _transcript(root: Path, session_id: str, channel: str = "acp") -> None:
    """A stored conversation, written the way SessionManager stores one."""
    from raven.session.manager import SessionManager

    stored = SessionManager(root).get_or_create(session_id)
    stored.add_message("user", "build me a deck about convolution")
    stored.add_message("assistant", "eighteen pages, published")
    SessionManager(root).save(stored)


async def test_a_returning_session_id_keeps_its_job_and_its_history(harness):
    """The symptom this answers: every call to the same instance was a fresh
    conversation, because the id is the raven session key and a new key reads an
    empty transcript beside the previous one instead of continuing it."""
    first = await harness.new_session()
    root = harness.jobs_root / first.split(":", 1)[1]
    assert root.is_dir()

    again = await harness.call("session/new", {"cwd": str(harness.cwd), "sessionId": first})
    assert again["result"]["sessionId"] == first, "a held session id was minted over"
    assert harness.jobs_root / again["result"]["sessionId"].split(":", 1)[1] == root

    # And an id this machine does not hold is not an error here: resuming is an
    # offer, so the client gets a working new session rather than a refusal.
    fresh = await harness.call("session/new", {"cwd": str(harness.cwd), "sessionId": "acp:20260101_000000_abcdef"})
    assert fresh["result"]["sessionId"] != first


@pytest.mark.parametrize(
    "session_id",
    [
        "acp:../../etc",
        "acp:..",
        "acp:sub/dir",
        "acp:",
        "other:20260101_000000_abcdef",
        "no-separator",
        12,
    ],
)
async def test_an_id_that_reaches_outside_the_jobs_root_is_not_resumed(harness, session_id):
    """The id arrives from the far side of a socket and becomes a path. Anything
    that is not one segment of this channel's own jobs directory gets a new
    session instead, and `session/load` refuses it outright."""
    await harness.handshake()
    minted = await harness.call("session/new", {"cwd": str(harness.cwd), "sessionId": session_id})
    assert minted["result"]["sessionId"].startswith("acp:")
    assert minted["result"]["sessionId"] != session_id

    for method in ("session/load", "session/resume"):
        refused = await harness.call(method, {"cwd": str(harness.cwd), "sessionId": session_id})
        assert refused["error"]["code"] == protocol.RESOURCE_NOT_FOUND, method


async def test_loading_a_session_replays_its_turns_oldest_first(harness):
    """The half the person sees. The model gets its history from disk by key; the
    client gets it as the update frames the spec has a loaded session emit."""
    session_id = await harness.new_session()
    root = harness.jobs_root / session_id.split(":", 1)[1]
    _transcript(root, session_id)
    await harness.methods._sessions.release(session_id)
    harness.frames.clear()

    result = await harness.call("session/load", {"cwd": str(harness.cwd), "sessionId": session_id})

    assert "error" not in result
    replayed = [
        (u["sessionUpdate"], u["content"]["text"])
        for u in harness.updates()
        if u["sessionUpdate"] in {"user_message_chunk", "agent_message_chunk"}
    ]
    assert replayed == [
        ("user_message_chunk", "build me a deck about convolution"),
        ("agent_message_chunk", "eighteen pages, published"),
    ]


async def test_a_replayed_turn_is_scrubbed_like_a_live_one(harness):
    """A reopen republishes the transcript, so a credential a past turn quoted
    would be re-published by every reopen from here on."""
    session_id = await harness.new_session()
    root = harness.jobs_root / session_id.split(":", 1)[1]
    from raven.session.manager import SessionManager

    stored = SessionManager(root).get_or_create(session_id)
    stored.add_message("user", "fetch it with curl -H 'Authorization: Bearer sk-proj-abcdefghijklmnop'")
    stored.add_message("assistant", "done; the key AKIAIOSFODNN7EXAMPLE is in ~/.aws/credentials")
    SessionManager(root).save(stored)
    await harness.methods._sessions.release(session_id)
    harness.frames.clear()

    result = await harness.call("session/load", {"cwd": str(harness.cwd), "sessionId": session_id})

    assert "error" not in result
    replayed = " ".join(
        u["content"]["text"]
        for u in harness.updates()
        if u["sessionUpdate"] in {"user_message_chunk", "agent_message_chunk"}
    )
    assert "sk-proj-abcdefghijklmnop" not in replayed
    assert "AKIAIOSFODNN7EXAMPLE" not in replayed
    # The shape survives, so the reader can still tell what was run and that a
    # credential file was read -- only the values are gone.
    assert "Authorization: Bearer [redacted]" in replayed
    assert "~/.aws/credentials" in replayed


async def test_a_session_never_started_here_is_refused_rather_than_minted(harness):
    """A client asked for one conversation. Handing it a different empty one under
    the same promise is what the spec's own error table exists for."""
    await harness.handshake()
    result = await harness.call("session/load", {"cwd": str(harness.cwd), "sessionId": "acp:20260101_000000_ffffff"})
    assert result["error"]["code"] == protocol.RESOURCE_NOT_FOUND


async def test_loading_a_session_that_is_already_open_does_not_build_a_second_engine(harness):
    session_id = await harness.new_session()
    built = len(harness.engines)

    result = await harness.call("session/load", {"cwd": str(harness.cwd), "sessionId": session_id})

    assert "error" not in result
    assert len(harness.engines) == built, "loading an open session built a second engine on one job"


async def test_resume_restores_the_session_without_replaying_it(harness):
    """The one difference from ``session/load``, and the reason both exist: the
    client still has the transcript on screen, so sending it again would paint
    every turn twice. What the two establish is otherwise identical."""
    session_id = await harness.new_session()
    root = harness.jobs_root / session_id.split(":", 1)[1]
    _transcript(root, session_id)
    await harness.methods._sessions.release(session_id)
    harness.frames.clear()

    result = await harness.call("session/resume", {"cwd": str(harness.cwd), "sessionId": session_id})

    assert "error" not in result
    # Same state as a load: the session is back on the table, on its own job.
    restored = harness.methods._sessions.get(session_id)
    assert restored is not None and restored.root == root
    assert restored.engine is not None, "a resumed session must be promptable"
    # And nothing was repainted.
    assert harness.updates() == [], "resume replayed a transcript the client already has"
    # The history the model reads is the transcript's, which is the whole point.
    from raven.session.manager import SessionManager

    assert len(SessionManager(root).get_or_create(session_id).messages) == 2


async def test_resume_and_load_reach_the_same_state(harness):
    """Both go through one recovery path, so a fix to either cannot miss the
    other. Asserted on the state rather than on the code so the sharing is what
    is pinned, not the shape of it."""
    loaded_id = await harness.new_session()
    loaded_root = harness.jobs_root / loaded_id.split(":", 1)[1]
    await harness.methods._sessions.release(loaded_id)
    await harness.call("session/load", {"cwd": str(harness.cwd), "sessionId": loaded_id})
    loaded = harness.methods._sessions.get(loaded_id)

    resumed_id = await harness.new_session()
    resumed_root = harness.jobs_root / resumed_id.split(":", 1)[1]
    await harness.methods._sessions.release(resumed_id)
    await harness.call("session/resume", {"cwd": str(harness.cwd), "sessionId": resumed_id})
    resumed = harness.methods._sessions.get(resumed_id)

    assert (loaded.root, loaded.cwd) == (loaded_root, str(harness.cwd))
    assert (resumed.root, resumed.cwd) == (resumed_root, str(harness.cwd))
    assert (loaded.engine is not None) == (resumed.engine is not None) is True


async def test_resuming_an_open_session_does_not_build_a_second_engine(harness):
    session_id = await harness.new_session()
    built = len(harness.engines)

    result = await harness.call("session/resume", {"cwd": str(harness.cwd), "sessionId": session_id})

    assert "error" not in result
    assert len(harness.engines) == built, "resume built a second engine on one job"


async def test_a_returning_id_still_open_is_answered_with_the_session_it_names(harness, tmp_path):
    """``SessionTable.add`` overwrites, and the table is the only handle on an
    engine: a replacement entry would leave the former one running on the same job
    with no route to a teardown -- not ``session/close``, which looks the id up,
    and not the connection teardown, which walks the table. ``session/new`` answers
    a returning id the way ``session/load`` does, which is what keeps the two
    entry points from disagreeing about one open session."""
    session_id = await harness.new_session()
    first = harness.sessions.get(session_id)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    again = await harness.call("session/new", {"cwd": str(elsewhere), "sessionId": session_id})

    assert again["result"]["sessionId"] == session_id
    assert len(harness.engines) == 1, "session/new built a second engine on one job"
    assert harness.sessions.get(session_id) is first, "the table entry was replaced"
    # The delivery destination is the client's and this call is where it says so.
    assert first.cwd == str(elsewhere)

    await harness.call("session/close", {"sessionId": session_id})
    assert harness.engines[0].torn_down, "the engine the first session/new built was never released"


async def test_a_reopened_session_still_holds_the_material_it_staged(harness, tmp_path):
    """The bookkeeping ``stage`` keeps does not survive the process and the job
    root does. Unrecovered, the first follow-up prompt claims nothing is staged,
    and a second source of a name already used overwrites the copy that has it."""
    first = tmp_path / "one" / "report.txt"
    first.parent.mkdir()
    first.write_text("first source", encoding="utf-8")
    second = tmp_path / "two" / "report.txt"
    second.parent.mkdir()
    second.write_text("second source", encoding="utf-8")

    session_id = await harness.new_session()
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": str(first)}]})
    await harness.methods._sessions.release(session_id)
    await harness.call("session/resume", {"cwd": str(harness.cwd), "sessionId": session_id})
    session = harness.sessions.get(session_id)
    assert [target.name for _, target in session.staged] == ["report.txt"]
    assert session.taken == {"report.txt"}

    # A follow-up that names nothing new is still working from staged material.
    await harness.call(
        "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "now add a summary page"}]}
    )
    follow_up = harness.engines[-1].scheduler.requests[0].text
    assert "No material staged for this run" not in follow_up
    assert str(session.materials / "report.txt") in follow_up

    # And a different source of the same basename is suffixed, not written over.
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": str(second)}]})
    assert (session.materials / "report.txt").read_text(encoding="utf-8") == "first source"
    assert (session.materials / "report-2.txt").read_text(encoding="utf-8") == "second source"


async def test_a_reopened_session_does_not_stage_a_source_it_already_holds(harness, tmp_path):
    """The other half of the same invariant, and the reason the recovered pairing
    cannot be compared by path: a follow-up repeats its declared block, and where
    each copy was copied from is recorded nowhere on disk. Staged again, the prompt
    would list one document twice and the deck would be grounded twice in it."""
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    task = '```raven-ppt\n{"materials": ["%s"]}\n```' % notes

    session_id = await harness.new_session()
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": task}]})
    await harness.methods._sessions.release(session_id)
    await harness.call("session/resume", {"cwd": str(harness.cwd), "sessionId": session_id})
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": task}]})

    session = harness.sessions.get(session_id)
    assert sorted(path.name for path in session.materials.iterdir()) == ["notes.md"]
    assert len(session.staged) == 1


async def test_a_repeat_of_two_same_named_sources_stages_neither_again(harness, tmp_path):
    """The same invariant where one basename holds more than one copy. Comparing a
    returning source against the copy of its exact name alone leaves the second of
    the two matched against the first's copy, so the directory grows a third file
    and the prompt lists one document twice."""
    first = tmp_path / "one" / "report.txt"
    first.parent.mkdir()
    first.write_text("first source", encoding="utf-8")
    second = tmp_path / "two" / "report.txt"
    second.parent.mkdir()
    second.write_text("second source", encoding="utf-8")
    task = '```raven-ppt\n{"materials": ["%s", "%s"]}\n```' % (first, second)

    session_id = await harness.new_session()
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": task}]})
    staged_names = sorted(path.name for path in harness.sessions.get(session_id).materials.iterdir())
    assert staged_names == ["report-2.txt", "report.txt"]

    await harness.methods._sessions.release(session_id)
    await harness.call("session/resume", {"cwd": str(harness.cwd), "sessionId": session_id})
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": task}]})

    session = harness.sessions.get(session_id)
    assert sorted(path.name for path in session.materials.iterdir()) == staged_names
    assert len(session.staged) == 2


# -- session/new --------------------------------------------------------------


async def test_session_new_mints_a_session_with_its_job_directory(harness):
    session_id = await harness.new_session()
    session = harness.sessions.get(session_id)
    assert session_id.startswith("acp:")
    assert session.root == harness.jobs_root / session_id.split(":", 1)[1]
    assert session.materials.is_dir() and session.out.is_dir()
    assert session.cwd == str(harness.cwd)


@pytest.mark.parametrize("cwd", [None, "", "relative/path", "/definitely/not/here"])
async def test_an_unusable_cwd_is_refused(harness, cwd):
    await harness.handshake()
    params = {"mcpServers": []} if cwd is None else {"cwd": cwd, "mcpServers": []}
    result = await harness.call("session/new", params)
    assert result["error"]["code"] == protocol.INVALID_PARAMS
    assert result["error"]["data"]["field"] == "cwd"


async def test_a_stdio_mcp_server_is_accepted_now(harness):
    """The field used to be refused outright. What it does instead is in
    ``test_acp_per_session_mcp.py``; this pins that it is no longer an error."""
    await harness.handshake()
    result = await harness.call("session/new", {"cwd": str(harness.cwd), "mcpServers": [{"name": "x", "command": "y"}]})
    assert "error" not in result, result


async def test_a_transport_this_agent_cannot_honour_is_dropped(harness):
    """``mcpCapabilities`` says http and sse do not come, and honouring one
    anyway would put its credentials in this process.

    Dropped rather than refused: the servers a dispatcher attaches are a
    capability on top of the task, so a stanza this build cannot serve costs the
    session those tools and must not cost the session -- failing ``session/new``
    means the sub-agent never runs at all.
    """
    await harness.handshake()
    result = await harness.call(
        "session/new",
        {"cwd": str(harness.cwd), "mcpServers": [{"name": "x", "type": "http", "url": "https://x/mcp"}]},
    )
    assert "error" not in result, result
    assert result["result"]["sessionId"]


async def test_an_empty_mcp_server_list_is_the_normal_value(harness):
    assert await harness.new_session()


async def test_a_session_whose_engine_failed_is_not_left_behind(harness):
    """It could never run a turn, so a later prompt must not find it."""
    await harness.handshake()
    harness.fail_build = "no provider key"
    result = await harness.call("session/new", {"cwd": str(harness.cwd), "mcpServers": []})
    assert result["error"]["code"] == protocol.INTERNAL_ERROR
    assert "no provider key" in result["error"]["data"]["reason"]
    assert harness.sessions.all() == []


# -- session/prompt: the turn ------------------------------------------------


async def test_an_unknown_session_is_the_one_error_a_prompt_may_get(harness):
    await harness.handshake()
    result = await harness.call("session/prompt", {"sessionId": "acp:gone", "prompt": [{"type": "text", "text": "x"}]})
    assert result["error"]["code"] == protocol.RESOURCE_NOT_FOUND


async def test_an_empty_prompt_ends_the_turn_without_running_one(harness):
    session_id = await harness.new_session()
    result = await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": ""}]})
    assert result["result"] == {"stopReason": "end_turn"}
    assert harness.engines[0].scheduler.requests == []


async def test_a_malformed_prompt_field_is_refused(harness):
    session_id = await harness.new_session()
    result = await harness.call("session/prompt", {"sessionId": session_id, "prompt": "just a string"})
    assert result["error"]["code"] == protocol.INVALID_PARAMS


async def test_a_prompt_with_no_material_runs_and_is_told_to_gather(harness):
    """Both entry points let this run. The refusal that used to stand here made a
    request the launcher answers with a deck end its turn over ACP instead."""
    session_id = await harness.new_session()
    result = await harness.call(
        "session/prompt",
        {"sessionId": session_id, "prompt": [{"type": "text", "text": "make a 5-page guide to using GitLab"}]},
    )
    assert result["result"] == {"stopReason": "end_turn"}
    assert harness.engines[0].scheduler.requests, "the turn never reached the agent"
    sent = harness.engines[0].scheduler.requests[0].text
    assert "No material staged for this run" in sent
    for tool in ("web_search", "web_fetch", "ppt_fetch"):
        assert tool in sent
    assert "Use only files under" not in sent


async def test_a_declared_material_is_staged_and_named_in_the_prompt(harness, tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    task = 'here is the material\n```raven-ppt\n{"materials": ["%s"]}\n```' % notes
    result = await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": task}]})
    assert result["result"] == {"stopReason": "end_turn"}
    session = harness.sessions.get(session_id)
    assert (session.materials / "notes.md").read_text(encoding="utf-8") == "facts"
    sent = harness.engines[0].scheduler.requests[0].text
    assert "# Material staged for this run" in sent
    assert str(session.materials / "notes.md") in sent
    assert f"Compile the deck under {session.out}/" in sent


async def test_a_declared_template_is_refused_rather_than_quietly_ignored(harness, tmp_path):
    """The launcher refuses this declaration, so this must too: a deck delivered
    without the house file the caller named would read as if it had been used."""
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    house = _pptx(tmp_path / "house.pptx", slides=1)
    session_id = await harness.new_session()
    task = '```raven-ppt\n{"materials": ["%s"], "template": "%s"}\n```' % (notes, house)
    result = await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": task}]})
    assert result["result"] == {"stopReason": "end_turn"}
    assert "ppt_template" in harness.said()
    assert harness.engines[0].scheduler.requests == []


async def test_a_pptx_named_as_material_is_staged_for_ppt_template_to_bind(harness, tmp_path):
    """The channel that replaced it, and the reason the refusal above costs
    nothing: a deck file arrives as material and the tool binds it."""
    house = _pptx(tmp_path / "house.pptx", slides=1)
    session_id = await harness.new_session()
    task = '```raven-ppt\n{"materials": ["%s"]}\n```' % house
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": task}]})
    session = harness.sessions.get(session_id)
    assert (session.materials / "house.pptx").is_file()
    assert "house.pptx" in harness.engines[0].scheduler.requests[0].text


async def test_a_path_named_only_in_prose_is_still_picked_up(harness, tmp_path):
    notes = tmp_path / "prose.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    await harness.call(
        "session/prompt",
        {"sessionId": session_id, "prompt": [{"type": "text", "text": f"use {notes} please"}]},
    )
    assert (harness.sessions.get(session_id).materials / "prose.md").is_file()


async def test_a_second_turn_adds_material_without_restaging_the_first(harness, tmp_path):
    one = tmp_path / "one.md"
    one.write_text("1", encoding="utf-8")
    two = tmp_path / "two.md"
    two.write_text("2", encoding="utf-8")
    session_id = await harness.new_session()
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": str(one)}]})
    # The second prompt names BOTH -- which is what a follow-up turn looks like
    # when the dispatching agent repeats its fenced block. Without the already-held
    # filter the repeat is staged again as ``one-2.md``, and the prompt then lists
    # one document twice, so the deck is grounded twice in it.
    await harness.call(
        "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": f"{one} and {two}"}]}
    )
    session = harness.sessions.get(session_id)
    assert sorted(p.name for p in session.materials.iterdir()) == ["one.md", "two.md"]
    assert len(session.staged) == 2


async def test_a_source_that_cannot_be_staged_stops_the_turn(harness, tmp_path):
    session_id = await harness.new_session()
    missing = tmp_path / "gone.md"
    task = '```raven-ppt\n{"materials": ["%s"]}\n```' % missing
    result = await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": task}]})
    assert result["result"] == {"stopReason": "end_turn"}
    assert "could not be staged" in harness.said()
    assert harness.engines[0].scheduler.requests == []


async def test_a_second_prompt_while_one_is_in_flight_is_refused(harness, tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    session = harness.sessions.get(session_id)
    session.staged.append((str(notes), notes))
    session.begin_turn()
    result = await harness.call(
        "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": str(notes)}]}
    )
    assert result["error"]["code"] == protocol.INVALID_REQUEST
    assert result["error"]["data"] == {"sessionId": session_id}


# -- session/prompt: the deck ------------------------------------------------


async def test_a_published_deck_is_reported_as_text_and_as_a_resource(tmp_path):
    """The text lines are not cosmetic: a raven host builds the sub-agent's reply
    out of agent_message_chunk text and nothing else."""
    harness = Harness(tmp_path)
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    session = harness.sessions.get(session_id)
    deck = session.out / "quarterly.pptx"
    harness.engines[0].scheduler._publish = deck
    harness.engines[0].scheduler._reply = f"Done. MEDIA: {deck}"

    result = await harness.call(
        "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": str(notes)}]}
    )
    assert result["result"] == {"stopReason": "end_turn"}
    said = harness.said()
    assert "Published a 3-slide deck." in said
    assert f"Deck: {deck}" in said
    # Delivered next to the client, which is what the session's cwd is for.
    delivered = harness.cwd / "quarterly.pptx"
    assert delivered.is_file()
    assert f"MEDIA: {delivered}" in said
    links = [u["content"] for u in harness.updates() if u["content"].get("type") == "resource_link"]
    assert links == [
        {
            "type": "resource_link",
            "uri": delivered.as_uri(),
            "name": "quarterly.pptx",
            "mimeType": ("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        }
    ]


async def test_a_claimed_deck_that_does_not_exist_is_called_out(tmp_path):
    harness = Harness(tmp_path)
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    harness.engines[0].scheduler._reply = "All done. MEDIA: /nowhere/deck.pptx"
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": str(notes)}]})
    assert "No verifiable deck was published" in harness.said()


async def test_a_turn_that_claimed_nothing_stays_quiet(tmp_path):
    """A follow-up turn that only answered a question did not fail, so there is
    nothing for this layer to add."""
    harness = Harness(tmp_path)
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    harness.engines[0].scheduler._reply = "Slide 4 already has that title."
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": str(notes)}]})
    assert harness.said() == "Slide 4 already has that title."


async def test_an_earlier_turns_deck_is_not_reported_again(tmp_path):
    """A session's job directory accumulates every turn's decks; a turn that
    published nothing must not hand back the previous one as its own result."""
    harness = Harness(tmp_path)
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    session = harness.sessions.get(session_id)
    scheduler = harness.engines[0].scheduler
    scheduler._publish = session.out / "first.pptx"
    scheduler._reply = "MEDIA: first.pptx"
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": str(notes)}]})
    assert "Published a 3-slide deck." in harness.said()

    harness.frames.clear()
    scheduler._publish = None
    scheduler._reply = "Nothing to change."
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "again"}]})
    assert harness.said() == "Nothing to change."


async def test_a_truncated_deck_is_not_a_deck(tmp_path):
    harness = Harness(tmp_path)
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    session = harness.sessions.get(session_id)

    async def _play():
        # A failed render leaves a file behind that is not an archive.
        (session.out).mkdir(parents=True, exist_ok=True)
        (session.out / "broken.pptx").write_bytes(b"not a zip")
        harness.outlet.say(session_id, "MEDIA: broken.pptx")
        session.settle("end_turn")

    harness.engines[0].scheduler._play = _play
    await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": str(notes)}]})
    assert "No verifiable deck was published" in harness.said()


async def test_a_cancelled_turn_reports_no_deck(tmp_path):
    """Every stop reason other than ``end_turn`` leaves what is on disk partial: a
    cancel lands mid-render, and announcing that file as "published" would hand the
    caller a deck the agent never finished reviewing."""
    harness = Harness(tmp_path)
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    session = harness.sessions.get(session_id)
    scheduler = harness.engines[0].scheduler
    scheduler._publish = session.out / "half.pptx"
    scheduler._reply = f"MEDIA: {session.out / 'half.pptx'}"
    scheduler._stop = "cancelled"

    result = await harness.call(
        "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": str(notes)}]}
    )
    assert result["result"] == {"stopReason": "cancelled"}
    assert "Published" not in harness.said()
    assert not (harness.cwd / "half.pptx").exists()


# -- cancel and close --------------------------------------------------------


async def test_cancel_stops_the_lane_and_answers_the_prompt(harness, tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    session = harness.sessions.get(session_id)
    session.staged.append((str(notes), notes))
    future = session.begin_turn()
    await harness.notify("session/cancel", {"sessionId": session_id})
    assert await asyncio.wait_for(future, SETTLE_TIMEOUT_S) == "cancelled"
    assert harness.engines[0].scheduler.cancelled == [session_id]


async def test_cancel_answers_the_prompt_even_with_nothing_to_cancel(harness):
    """A cancel arriving between begin_turn and the scheduler accepting the turn
    finds nothing to stop, and the prompt still has to be answered."""
    session_id = await harness.new_session()
    session = harness.sessions.get(session_id)
    future = session.begin_turn()
    harness.engines[0].scheduler.cancel_conversation = lambda cid: 0
    await harness.notify("session/cancel", {"sessionId": session_id})
    assert await asyncio.wait_for(future, SETTLE_TIMEOUT_S) == "cancelled"


async def test_cancel_for_an_unknown_session_is_tidy_not_broken(harness):
    await harness.handshake()
    assert await harness.notify("session/cancel", {"sessionId": "acp:gone"}) is None


async def test_close_releases_the_session_and_its_engine(harness):
    session_id = await harness.new_session()
    result = await harness.call("session/close", {"sessionId": session_id})
    assert result["result"] == {}
    assert harness.sessions.get(session_id) is None
    assert harness.engines[0].torn_down is True


async def test_closing_a_session_settles_its_pending_prompt(harness):
    """Otherwise the handler suspended on that future waits out the shutdown grace
    period with its engine already gone."""
    session_id = await harness.new_session()
    future = harness.sessions.get(session_id).begin_turn()
    await harness.call("session/close", {"sessionId": session_id})
    assert await asyncio.wait_for(future, SETTLE_TIMEOUT_S) == "cancelled"


async def test_closing_an_unknown_session_is_refused(harness):
    await harness.handshake()
    result = await harness.call("session/close", {"sessionId": "acp:gone"})
    assert result["error"]["code"] == protocol.RESOURCE_NOT_FOUND


async def test_releasing_twice_does_not_tear_down_twice(harness):
    session_id = await harness.new_session()
    await harness.sessions.release(session_id)
    harness.engines[0].torn_down = False
    await harness.sessions.release(session_id)
    assert harness.engines[0].torn_down is False


# -- prompt content blocks ---------------------------------------------------


async def test_a_resource_link_lands_in_the_text_as_a_path(harness, tmp_path):
    notes = tmp_path / "linked.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    await harness.call(
        "session/prompt",
        {
            "sessionId": session_id,
            "prompt": [
                {"type": "text", "text": "use this"},
                {"type": "resource_link", "uri": notes.as_uri(), "name": "linked.md"},
            ],
        },
    )
    # Named in the prompt, so the staging scan picks it up like any other path.
    assert (harness.sessions.get(session_id).materials / "linked.md").is_file()


async def test_an_embedded_text_resource_is_inlined(harness, tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    await harness.call(
        "session/prompt",
        {
            "sessionId": session_id,
            "prompt": [
                {"type": "text", "text": str(notes)},
                {"type": "resource", "resource": {"uri": "file:///abs/inline.md", "text": "inlined facts"}},
            ],
        },
    )
    assert "inlined facts" in harness.engines[0].scheduler.requests[0].text


async def test_a_binary_resource_is_named_not_decoded(harness, tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    await harness.call(
        "session/prompt",
        {
            "sessionId": session_id,
            "prompt": [
                {"type": "text", "text": str(notes)},
                {"type": "resource", "resource": {"uri": "file:///abs/x.bin", "blob": "AAAA"}},
            ],
        },
    )
    assert "binary resource, not inlined" in harness.engines[0].scheduler.requests[0].text


async def test_a_block_this_agent_cannot_read_says_so_rather_than_vanishing(harness, tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    session_id = await harness.new_session()
    await harness.call(
        "session/prompt",
        {
            "sessionId": session_id,
            "prompt": [
                {"type": "text", "text": str(notes)},
                {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"},
                "not an object",
            ],
        },
    )
    sent = harness.engines[0].scheduler.requests[0].text
    assert "cannot read" in sent
    assert "audio attachment" in sent


async def test_a_remote_file_uri_is_not_turned_into_a_local_path(harness):
    """``file://host/share`` names somebody else's machine; making it local would
    point the agent at the wrong file rather than at none."""
    from raven.acp.methods import _file_uri_to_path

    assert _file_uri_to_path("file://elsewhere/share/x.md") is None
    assert _file_uri_to_path("file://localhost/tmp/x.md") == "/tmp/x.md"
    assert _file_uri_to_path("file:///tmp/my%20notes.md") == "/tmp/my notes.md"
    assert _file_uri_to_path("https://example.com/x.md") is None


# -- the handler never takes the connection down ----------------------------


async def test_a_handler_bug_becomes_an_error_frame(harness, monkeypatch):
    """Without this the read loop dies on one bad request and the client sees the
    agent vanish mid-turn, which is indistinguishable from a crash."""
    await harness.handshake()

    def _boom(_params):
        raise ValueError("unexpected")

    monkeypatch.setattr(harness.methods, "_validated_mcp_servers", _boom)
    result = await harness.call("session/new", {"cwd": str(harness.cwd)})
    assert result["error"]["code"] == protocol.INTERNAL_ERROR
    assert result["error"]["data"] == {"reason": "unexpected"}


# -- credentials do not leave the process ------------------------------------


async def test_an_error_data_never_carries_a_traceback(harness, monkeypatch):
    """A traceback tail names the filesystem it ran on and sometimes argument
    values, and an error frame is kept in the client's transcript."""
    await harness.handshake()

    def _boom(_params):
        raise AcpMethodError(
            protocol.INTERNAL_ERROR,
            "the build failed",
            {"traceback_tail": '  File "/home/me/.config/raven.toml", line 3', "stage": "render"},
        )

    monkeypatch.setattr(harness.methods, "_validated_mcp_servers", _boom)
    result = await harness.call("session/new", {"cwd": str(harness.cwd)})

    assert result["error"]["data"] == {"stage": "render"}


async def test_an_error_whose_data_is_only_private_carries_none_at_all(harness, monkeypatch):
    """Absent rather than an empty object: ``data`` is optional, and ``{}`` reads
    as "there is detail and it is empty"."""
    await harness.handshake()

    def _boom(_params):
        raise AcpMethodError(protocol.INTERNAL_ERROR, "the build failed", {"stack": "..."})

    monkeypatch.setattr(harness.methods, "_validated_mcp_servers", _boom)
    result = await harness.call("session/new", {"cwd": str(harness.cwd)})

    assert "data" not in result["error"]


async def test_an_error_message_and_its_data_are_both_redacted(harness, monkeypatch):
    await harness.handshake()

    def _boom(_params):
        raise AcpMethodError(
            protocol.INVALID_REQUEST,
            "GET /v1 refused api_key=sk-proj-abcdefghijklmnop",
            {"env": {"OPENAI_API_KEY": "sk-proj-abcdefghijklmnop"}},
        )

    monkeypatch.setattr(harness.methods, "_validated_mcp_servers", _boom)
    result = await harness.call("session/new", {"cwd": str(harness.cwd)})

    assert "sk-proj-abcdefghijklmnop" not in str(result)
    assert REPLACEMENT in result["error"]["message"]
    # The key stays: the reader needs to know which credential was set.
    assert result["error"]["data"]["env"] == {"OPENAI_API_KEY": REPLACEMENT}


async def test_a_handler_bugs_reason_is_redacted(harness, monkeypatch):
    await harness.handshake()

    def _boom(_params):
        raise ValueError("cannot reach https://user:hunter2secret@db:5432/app")

    monkeypatch.setattr(harness.methods, "_validated_mcp_servers", _boom)
    result = await harness.call("session/new", {"cwd": str(harness.cwd)})

    assert "hunter2secret" not in str(result)
    assert result["error"]["data"]["reason"] == f"cannot reach https://user:{REPLACEMENT}@db:5432/app"


async def test_an_engine_build_failures_reason_is_redacted(harness):
    await harness.handshake()
    harness.fail_build = "no provider key; tried api_key=sk-proj-abcdefghijklmnop"

    result = await harness.call("session/new", {"cwd": str(harness.cwd), "mcpServers": []})

    assert "sk-proj-abcdefghijklmnop" not in str(result)
    assert REPLACEMENT in result["error"]["data"]["reason"]


async def test_a_staging_failure_is_said_redacted(harness, monkeypatch):
    """This one never passes the dispatcher's error path: a prompt is answered
    with message content, so it has to be redacted where it is said."""
    session_id = await harness.new_session()

    def _boom(_session, _text):
        raise materials.StagingError("cannot stage /run/secrets/token=ghp_ABCDEFGHIJKLMNOPQRSTUV")

    monkeypatch.setattr(harness.methods, "_stage_for", _boom)
    result = await harness.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "x"}]})

    assert result["result"] == {"stopReason": "end_turn"}
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUV" not in harness.said()
    assert REPLACEMENT in harness.said()
    # The path stays: that the agent read a credential file is the one thing the
    # reader most needs to see.
    assert "/run/secrets/" in harness.said()


def test_a_non_mapping_data_is_still_walked():
    """``data`` is typed as any JSON value, and a list of arguments is a shape a
    handler may reasonably send."""
    assert sanitise_error_data(["--token", "ghp_ABCDEFGHIJKLMNOPQRSTUV"]) == ["--token", REPLACEMENT]
    assert sanitise_error_data(None) is None


async def test_a_response_frame_is_not_mistaken_for_a_request(harness):
    assert await harness.methods.handle({"jsonrpc": "2.0", "id": 9, "result": {}}) is None


def test_only_the_stop_reasons_the_schema_names_are_used():
    """A translator that latched a reason outside the enum is caught here rather
    than by a client."""
    assert {"end_turn", "cancelled"} <= protocol.STOP_REASONS


# -- the outbound half -------------------------------------------------------


def _wired(tmp_path):
    """A method surface with the outbound half attached, as `serve` builds it."""
    from raven.acp.outbound import OutboundRequests
    from raven.acp.questions import AcpQuestions

    harness = Harness(tmp_path)
    frames: list[dict] = []
    outbound = OutboundRequests(frames.append)
    questions = AcpQuestions(outbound=outbound, sessions=harness.sessions, emit=frames.append)
    harness.methods = AcpMethods(
        emit=harness.frames.append,
        sessions=harness.sessions,
        engine_factory=harness._factory,
        jobs_root=harness.jobs_root,
        outbound=outbound,
        questions=questions,
    )
    return harness, outbound, questions, frames


async def test_a_response_frame_is_matched_against_what_this_agent_asked(tmp_path):
    """The routing the surface did not have. Without it every answer a client
    sends is logged and dropped, and every question this agent asks times out."""
    harness, outbound, _questions, frames = _wired(tmp_path)
    await harness.handshake()

    call = asyncio.create_task(outbound.call("elicitation/create", {"message": "which theme"}))
    await asyncio.sleep(0)
    request_id = frames[0]["id"]

    answered = await harness.methods.handle(
        {"jsonrpc": "2.0", "id": request_id, "result": {"action": "accept", "content": {"answer": "teal"}}}
    )

    assert answered is None, "a response is not a request and gets no reply"
    assert await asyncio.wait_for(call, timeout=1) == {"action": "accept", "content": {"answer": "teal"}}


async def test_a_response_to_nothing_is_still_answered_with_silence(tmp_path):
    harness, _outbound, _questions, _frames = _wired(tmp_path)
    await harness.handshake()

    assert await harness.methods.handle({"jsonrpc": "2.0", "id": 99, "result": {}}) is None


async def test_the_handshake_hands_the_client_s_capabilities_to_the_questions(tmp_path):
    """Which route a question takes is decided by this, so a surface that never
    learned what the client declared would ask through the wrong one."""
    harness, _outbound, questions, _frames = _wired(tmp_path)
    assert questions.client.elicitation_form is False

    await harness.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"elicitation": {"form": {}}}})

    assert questions.client.elicitation_form is True


async def test_cancelling_a_session_takes_back_the_question_it_was_holding(tmp_path):
    harness, _outbound, questions, _frames = _wired(tmp_path)
    session_id = await harness.new_session()
    cancelled: list[str] = []
    questions.cancel = lambda sid: cancelled.append(sid) or 0  # type: ignore[method-assign]

    await harness.notify("session/cancel", {"sessionId": session_id})

    assert cancelled == [session_id]


async def test_closing_a_session_takes_back_the_question_it_was_holding(tmp_path):
    """The other half of the lifecycle. An ask runs on its own task, so closing
    without this leaves the client a form for the rest of the ask's deadline and
    an answer aimed at a broker that went away with the engine."""
    harness, outbound, questions, frames = _wired(tmp_path)
    session_id = await harness.new_session()
    questions.set_client(ClientCapabilities.from_params({"clientCapabilities": {"elicitation": {"form": {}}}}))
    broker = _Broker()
    questions.handle(
        broker,
        "clarify.request",
        {"request_id": "q-1", "conversation_id": session_id, "question": "which theme", "choices": ["teal"]},
    )
    await asyncio.sleep(0)
    assert outbound.in_flight == 1

    await harness.call("session/close", {"sessionId": session_id})
    # No drain here, and that is the point: `drain` cancels what is left and would
    # perform the retraction itself, so a test that drained would pass with the
    # close doing nothing. A real client never calls it. The yields are only for
    # the cancelled task to reach its own except branch.
    for _ in range(10):
        await asyncio.sleep(0)

    assert outbound.in_flight == 0, "the request was left in flight after the session went"
    assert [f for f in frames if f.get("method") == protocol.CANCEL_REQUEST_METHOD], "no retraction reached the client"
    assert broker.answers == [("q-1", "")], "the tool call was left blocked"


class _Broker:
    def __init__(self) -> None:
        self.answers: list[tuple[str, str]] = []

    def reply(self, request_id: str, answer: str) -> bool:
        self.answers.append((request_id, answer))
        return True
