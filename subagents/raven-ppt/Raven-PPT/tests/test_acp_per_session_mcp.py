"""The MCP servers a session brings to this fork over ACP (raven/acp/methods.py).

This agent used to refuse ``mcpServers`` outright, and the refusal's stated
reason was half right: MCP is connected once per engine, and nothing scoped a
server to one session. The second half is what these pin down.

What arrives is not a server definition in any interesting sense -- it is a stdio
stanza pointing at an endpoint the dispatcher already opened, holding no
credentials. So the tests here are about the two things this agent still owns:
connecting what it was handed, and making it reachable from that session's turns
and nowhere else. Plus the reclaim, because a connection nothing closes is a
subprocess nothing kills.

This surface builds one engine per session, so the registries are already apart
and cross-session isolation is not what needs proving here -- the
``test_tool_registry_session_overlay.py`` concurrency cases hold two scopes open
on one shared registry, which is the stricter statement. What is proved here is
the wiring: the stanza reaches a connect, the tools land on the session that
brought them, and the reclaim runs on both ways out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from raven.acp import protocol
from raven.acp.capabilities import agent_capabilities
from raven.acp.methods import AcpMethods
from raven.acp.session import SessionTable
from raven.acp.spine import AcpOutlet
from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry

_PATCH = "raven.agent.tools.mcp.connect_mcp_servers"


class _Stub(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "stub"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ran"


class _Upstream:
    """A stand-in for ``connect_mcp_servers`` that records what it was asked to connect."""

    def __init__(self, *, tools: tuple[str, ...] = ("probe",), fail: Exception | None = None) -> None:
        self.tools = tools
        self.fail = fail
        self.connected: list[tuple[str, Any]] = []
        self.stacks: list[Any] = []

    async def __call__(self, servers, registry, stack, executor=None):
        self.stacks.append(stack)
        for name, cfg in servers.items():
            self.connected.append((name, cfg))
        if self.fail is not None:
            raise self.fail
        for name in servers:
            for tool in self.tools:
                registry.register(_Stub(f"mcp_{name}_{tool}"))


class _Loop:
    """The half of an AgentLoop this surface reads: its tool registry."""

    def __init__(self) -> None:
        self.tools = ToolRegistry()
        self.tools.register(_Stub("read_file"))


class _Engine:
    """A scripted engine that carries a real registry, so binding has somewhere to land."""

    def __init__(self) -> None:
        self.agent_loop = _Loop()
        self.scheduler = _Scheduler()
        self.torn_down = False

    async def teardown(self) -> None:
        self.torn_down = True


class _Scheduler:
    def submit(self, req):
        return None

    def cancel_conversation(self, conversation_id: str) -> int:
        return 0


class _Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.frames: list[dict] = []
        self.sessions = SessionTable()
        self.outlet = AcpOutlet("acp", self.frames.append, self.sessions)
        self.engines: list[_Engine] = []
        self.cwd = tmp_path / "client"
        self.cwd.mkdir(parents=True)
        self.methods = AcpMethods(
            emit=self.frames.append,
            sessions=self.sessions,
            engine_factory=self._factory,
            jobs_root=tmp_path / "jobs",
        )

    async def _factory(self, session):
        engine = _Engine()
        self.engines.append(engine)
        return engine

    async def call(self, method: str, params: dict | None = None, request_id: int | str = 1):
        frame: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        return await self.methods.handle(frame)

    async def handshake(self) -> None:
        await self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})

    async def new_session(self, servers: list[dict] | None = None) -> dict:
        return await self.call("session/new", {"cwd": str(self.cwd), "mcpServers": servers or []})

    def offered_to(self, session_id: str) -> set[str]:
        session = self.sessions.get(session_id)
        registry = session.engine.agent_loop.tools
        with registry.session_scope_for(session_id):
            return {d["function"]["name"] for d in registry.get_definitions()}


def _stanza(name: str = "bridged", *, command: str = "/usr/bin/raven", args: list[str] | None = None, **extra) -> dict:
    entry = {"name": name, "command": command, "args": args if args is not None else ["mcp", "bridge", "/tmp/s.sock"]}
    entry.setdefault("env", [])
    entry.update(extra)
    return entry


@pytest.fixture
def rig(tmp_path):
    return _Rig(tmp_path)


class TestTheFieldIsAccepted:
    async def test_a_session_can_bring_a_stdio_server(self, rig) -> None:
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            response = await rig.new_session([_stanza()])

        assert "error" not in response, response
        assert [name for name, _cfg in upstream.connected] == ["bridged"]
        assert rig.offered_to(response["result"]["sessionId"]) == {"read_file", "mcp_bridged_probe"}

    async def test_the_stanza_is_connected_verbatim(self, rig) -> None:
        """The command is the dispatcher's bridge process and the args are its
        endpoint. Rewriting either would point the connection at nothing."""
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            await rig.new_session(
                [
                    _stanza(
                        command="/opt/raven/bin/raven",
                        args=["mcp", "bridge", "/tmp/mcp-a1b2.sock"],
                        env=[{"name": "RAVEN_MCP_DEBUG", "value": "1"}],
                    )
                ]
            )

        (_name, cfg) = upstream.connected[0]
        assert cfg.type == "stdio"
        assert cfg.command == "/opt/raven/bin/raven"
        assert cfg.args == ["mcp", "bridge", "/tmp/mcp-a1b2.sock"]
        assert cfg.env == {"RAVEN_MCP_DEBUG": "1"}
        assert cfg.url == ""

    async def test_env_as_a_mapping_says_the_same_thing(self, rig) -> None:
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            await rig.new_session([_stanza(env={"TOKEN": "abc"})])

        assert upstream.connected[0][1].env == {"TOKEN": "abc"}

    async def test_an_empty_list_is_still_the_normal_value(self, rig) -> None:
        await rig.handshake()

        response = await rig.new_session([])

        assert "error" not in response, response
        assert rig.offered_to(response["result"]["sessionId"]) == {"read_file"}


class TestWhatIsDropped:
    """A stanza this build cannot serve costs the session those tools, never the
    session. The servers a dispatcher attaches are a capability on top of the
    task, so failing ``session/new`` over one means the sub-agent never runs at
    all -- the worse answer by far.
    """

    async def test_an_http_stanza_is_dropped_because_the_capability_said_so(self, rig) -> None:
        await rig.handshake()

        response = await rig.new_session([{"name": "remote", "type": "http", "url": "https://x/mcp"}])

        assert "error" not in response, response
        assert rig.offered_to(response["result"]["sessionId"]) == {"read_file"}

    async def test_a_stanza_without_a_command_is_dropped(self, rig) -> None:
        await rig.handshake()

        response = await rig.new_session([{"name": "broken"}])

        assert "error" not in response, response
        assert rig.offered_to(response["result"]["sessionId"]) == {"read_file"}

    async def test_one_bad_stanza_does_not_take_the_good_one_with_it(self, rig) -> None:
        """Dropped per entry: a dispatcher that attached two servers and got one
        stanza wrong must still deliver the other."""
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            response = await rig.new_session([{"name": "broken"}, _stanza("good")])

        assert "error" not in response, response
        assert "mcp_good_probe" in rig.offered_to(response["result"]["sessionId"])

    async def test_a_malformed_stanza_still_gets_its_engine(self, rig) -> None:
        """It used to cost the engine, because the stanza was refused and the
        session with it. Dropping instead means the dispatch proceeds: the engine
        is built and the session is registered, without the server nobody could
        parse. The task is what the caller asked for; the server was an
        attachment to it."""
        await rig.handshake()

        response = await rig.new_session([{"name": "broken"}])

        assert "error" not in response, response
        assert rig.engines, "the dispatch must still get its engine"
        assert rig.offered_to(response["result"]["sessionId"]) == {"read_file"}


class TestOneSessionOnly:
    async def test_a_sibling_session_is_not_offered_them(self, rig) -> None:
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            brought = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        bare = (await rig.new_session([]))["result"]["sessionId"]

        assert rig.offered_to(brought) == {"read_file", "mcp_bridged_probe"}
        assert rig.offered_to(bare) == {"read_file"}

    async def test_two_sessions_bringing_the_same_server_name_stay_apart(self, rig) -> None:
        """The playbook case: two nodes on one fork, each handed its own bridge."""
        await rig.handshake()

        with patch(_PATCH, new=_Upstream(tools=("alpha",))):
            first = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        with patch(_PATCH, new=_Upstream(tools=("beta",))):
            second = (await rig.new_session([_stanza()]))["result"]["sessionId"]

        assert rig.offered_to(first) == {"read_file", "mcp_bridged_alpha"}
        assert rig.offered_to(second) == {"read_file", "mcp_bridged_beta"}

    async def test_nothing_is_registered_for_the_life_of_the_registry(self, rig) -> None:
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]

        registry = rig.sessions.get(session_id).engine.agent_loop.tools
        assert registry.tool_names == ["read_file"]
        assert registry.get("mcp_bridged_probe") is None


class TestTheReclaim:
    async def test_close_hides_and_disconnects(self, rig) -> None:
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        engine = rig.engines[0]
        stack = upstream.stacks[0]
        await rig.call("session/close", {"sessionId": session_id})

        assert not stack._exit_callbacks
        assert engine.torn_down
        # The registry outlives the engine object in this test, so the binding
        # being gone is observable rather than merely implied by the teardown.
        assert engine.agent_loop.tools._session_tools == {}

    async def test_a_dropped_connection_reclaims_the_rest(self, rig) -> None:
        """The backstop. A client that exits without closing anything still owns
        subprocesses, and nothing else would reap them."""
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        session = rig.sessions.get(session_id)
        stack = upstream.stacks[0]

        await rig.sessions.aclose()

        assert not stack._exit_callbacks
        assert session.mcp is None

    async def test_release_is_idempotent(self, rig) -> None:
        """``session/close`` then the connection close is the ordinary sequence,
        and running the reclaim twice must not raise."""
        from raven.acp.session import release_session_mcp

        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        session = rig.sessions.get(session_id)

        await rig.call("session/close", {"sessionId": session_id})
        await release_session_mcp(session)

        assert session.mcp is None

    async def test_close_is_safe_for_a_session_that_brought_nothing(self, rig) -> None:
        await rig.handshake()
        session_id = (await rig.new_session([]))["result"]["sessionId"]

        response = await rig.call("session/close", {"sessionId": session_id})

        assert "error" not in response, response


class TestTheStanzaIsAReplacement:
    """``session/load`` and ``session/resume`` carry it too, and by then the
    dispatch that opened the previous endpoints is gone."""

    async def test_an_empty_stanza_on_reopen_releases_what_it_replaces(self, rig) -> None:
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        session = rig.sessions.get(session_id)
        stack = upstream.stacks[0]
        assert rig.offered_to(session_id) == {"read_file", "mcp_bridged_probe"}

        response = await rig.call(
            "session/load", {"sessionId": session_id, "cwd": str(rig.cwd), "mcpServers": []}
        )

        assert "error" not in response, response
        # Left bound, the next turn calls a tool whose bridge the host has closed.
        assert rig.offered_to(session_id) == {"read_file"}
        assert not stack._exit_callbacks
        assert session.mcp is None

    async def test_a_new_stanza_on_reopen_replaces_the_old_connections(self, rig) -> None:
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]
            first = upstream.stacks[0]
            response = await rig.call(
                "session/resume", {"sessionId": session_id, "cwd": str(rig.cwd), "mcpServers": [_stanza()]}
            )

        assert "error" not in response, response
        assert not first._exit_callbacks
        assert rig.sessions.get(session_id).mcp is upstream.stacks[-1]
        assert rig.offered_to(session_id) == {"read_file", "mcp_bridged_probe"}


class TestAServerThatWillNotConnect:
    async def test_the_session_is_still_minted(self, rig) -> None:
        """One unreachable server must not turn into a sub-agent that cannot
        answer at all -- the dispatcher already tells the reader it was degraded."""
        await rig.handshake()

        with patch(_PATCH, new=_Upstream(fail=RuntimeError("no such socket"))):
            response = await rig.new_session([_stanza()])

        assert "error" not in response, response
        assert rig.offered_to(response["result"]["sessionId"]) == {"read_file"}


class TestTheDeclaration:
    def test_the_capability_is_declared(self) -> None:
        """A build that answers the field with -32602 is otherwise
        indistinguishable from this one, so the host reads this before sending."""
        assert protocol.SESSION_MCP_CAPABILITY in agent_capabilities()["_meta"]

    def test_and_the_transports_it_does_not_take_are_still_false(self) -> None:
        assert agent_capabilities()["mcpCapabilities"] == {"http": False, "sse": False}


class TestTheScopeIsOpenedWhereTheTurnRuns:
    """The third leg. Binding the tools and declaring the capability are worth
    nothing if the scope is opened in the wrong place.

    The ACP prompt handler cannot open it: it submits onto the scheduler, and the
    turn then runs on a task that inherits no context from the handler. So
    ``AgentLoop.run_turn`` opens it, keyed off the same session id the ACP layer
    bound the tools under.
    """

    @staticmethod
    def _loop(registry):
        from raven.agent.loop.main import AgentLoop

        class _Stubbed:
            def __init__(self) -> None:
                self.tools = registry
                self.seen: dict[str, Any] = {}
                self._turns_in_flight = 0

            def _adopt_pending_provider(self) -> None:
                return None

            async def _run_turn(self, req, emit, drain, **kwargs):
                self.seen["offered"] = {d["function"]["name"] for d in self.tools.get_definitions()}
                self.seen["kwargs"] = kwargs
                return "outcome"

        loop = _Stubbed()
        loop.run_turn = AgentLoop.run_turn.__get__(loop, _Stubbed)
        return loop

    @staticmethod
    def _request(conversation: str | None, *, chat_id: str = "c1"):
        from raven.spine import ChatType, Origin, Source, TurnRequest

        return TurnRequest(
            origin=Origin.USER,
            source=Source(channel="acp", chat_id=chat_id, sender_id="u", chat_type=ChatType.DM),
            text="hello",
            conversation=conversation,
        )

    async def test_the_turn_runs_with_its_own_session_s_tools_visible(self) -> None:
        registry = ToolRegistry()
        registry.register(_Stub("read_file"))
        registry.bind_session_tools("acp:one", {"mcp_a_probe": _Stub("mcp_a_probe")})
        loop = self._loop(registry)

        outcome = await loop.run_turn(self._request("acp:one"), None, None, stream=False)

        assert outcome == "outcome"
        assert loop.seen["offered"] == {"read_file", "mcp_a_probe"}

    async def test_and_a_sibling_session_s_turn_does_not(self) -> None:
        registry = ToolRegistry()
        registry.register(_Stub("read_file"))
        registry.bind_session_tools("acp:one", {"mcp_a_probe": _Stub("mcp_a_probe")})
        loop = self._loop(registry)

        await loop.run_turn(self._request("acp:two"), None, None)

        assert loop.seen["offered"] == {"read_file"}

    async def test_the_scope_closes_when_the_turn_returns(self) -> None:
        registry = ToolRegistry()
        registry.bind_session_tools("acp:one", {"mcp_a_probe": _Stub("mcp_a_probe")})
        loop = self._loop(registry)

        await loop.run_turn(self._request("acp:one"), None, None)

        assert registry.get("mcp_a_probe") is None

    async def test_the_session_key_falls_back_to_channel_and_chat_id(self) -> None:
        registry = ToolRegistry()
        registry.bind_session_tools("acp:c9", {"mcp_a_probe": _Stub("mcp_a_probe")})
        loop = self._loop(registry)

        await loop.run_turn(self._request(None, chat_id="c9"), None, None)

        assert loop.seen["offered"] == {"mcp_a_probe"}

    async def test_every_keyword_reaches_the_turn_unchanged(self) -> None:
        """The wrapper is a pass-through; a dropped sink is a silent behaviour change."""
        registry = ToolRegistry()
        loop = self._loop(registry)
        usage: dict[str, Any] = {}
        text: dict[str, Any] = {}

        await loop.run_turn(
            self._request("acp:one"),
            None,
            None,
            stream=False,
            inline_tool_stream=True,
            usage_sink=usage,
            text_sink=text,
        )

        assert loop.seen["kwargs"] == {
            "stream": False,
            "inline_tool_stream": True,
            "usage_sink": usage,
            "text_sink": text,
        }

    async def test_the_in_flight_count_still_balances(self) -> None:
        """The scope was added inside the count's try/finally; a mis-nesting here
        would leave a parked ``/model`` switch adopted at the wrong moment."""
        registry = ToolRegistry()
        loop = self._loop(registry)

        await loop.run_turn(self._request("acp:one"), None, None)

        assert loop._turns_in_flight == 0
