"""The MCP servers a session brings to this fork over ACP (raven/acp/methods.py).

This agent used to refuse ``mcpServers`` outright, and the refusal's stated
reason was half right: MCP is connected once per process, and nothing scoped a
server to one session. The second half is what these pin down.

What arrives is not a server definition in any interesting sense -- it is a stdio
stanza pointing at an endpoint the dispatcher already opened, holding no
credentials. So the tests here are about the two things this agent still owns:
connecting what it was handed, and making it reachable from that session's turns
and nowhere else. Plus the reclaim, because a connection nothing closes is a
subprocess nothing kills.

The reclaim is the one place this surface differs from its siblings. It has no
``session/close`` and no ``session/delete`` -- a session lives as long as the
connection does -- so there are exactly two release points, a re-adopt and
``AcpMethods.aclose`` on the way out, and ``raven/acp/server.py`` is what calls
the second. Both are asserted below.

The isolation half is asserted twice: once through the registry (see
``test_tool_registry_session_overlay.py``, which holds two scopes open at the
same time) and once here, through the ACP surface that binds them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from raven.acp import protocol
from raven.acp.capabilities import agent_capabilities
from raven.acp.methods import AcpMethods
from raven.acp.spine import AcpSessions
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


class _StubHandle:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    async def result(self):
        return None


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


class _Engine:
    """The part of an agent loop this layer touches: its tool registry."""

    def __init__(self, tools: Any) -> None:
        self.tools = tools


class _Rig:
    """AcpMethods against a scripted submit and real tool registries.

    ``shared`` picks which engine shape is under test. One registry for every
    session is ``AcpLoops.single`` -- the shape where the session scoping is the
    only thing keeping two sessions apart, which is why it stays the default
    here. ``shared=False`` is the deployed shape, one engine per session, where
    the scoping rides on top of a structural separation.
    """

    def __init__(self, session_manager=None, *, shared: bool = True, engineless: bool = False) -> None:
        self.frames: list[dict] = []
        self.sessions = AcpSessions()
        self.tools = ToolRegistry()
        self.tools.register(_Stub("read_file"))
        self.shared = shared
        self.engineless = engineless
        self.engines: dict[str, Any] = {}
        self.built: list[str] = []
        self.submitted: list = []
        self.methods = AcpMethods(
            submit=self._submit,
            sessions=self.sessions,
            emit=self.frames.append,
            session_manager=session_manager,
            on_session_open=self._open,
            peek_session_loop=self.engines.get,
        )

    async def _open(self, conversation: str) -> Any:
        self.built.append(conversation)
        engine = self.engines.get(conversation)
        if engine is None:
            if self.engineless:
                engine = object()
            elif self.shared:
                engine = _Engine(self.tools)
            else:
                registry = ToolRegistry()
                registry.register(_Stub("read_file"))
                engine = _Engine(registry)
            self.engines[conversation] = engine
        return engine

    def evict(self, conversation: str) -> None:
        """What ``AcpLoops`` does to an idle session over its cap: the engine and
        the registry that held its bindings are dropped, and the next turn gets a
        newly built one."""
        self.engines.pop(conversation, None)

    def registry_for(self, conversation: str) -> Any:
        return self.engines[conversation].tools

    def _submit(self, req):
        self.submitted.append(req)
        return _StubHandle()

    async def call(self, method: str, params: dict | None = None, *, request_id: int | str = 1):
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        return await self.methods.handle(frame)

    async def handshake(self):
        return await self.call("initialize", {"protocolVersion": 1})

    async def new_session(self, servers: list[dict] | None = None) -> dict:
        return await self.call("session/new", {"cwd": "/tmp", "mcpServers": servers or []})

    def offered_to(self, session_id: str) -> set[str]:
        registry = self.engines[session_id].tools if session_id in self.engines else self.tools
        with registry.session_scope_for(session_id):
            return {d["function"]["name"] for d in registry.get_definitions()}


def _stanza(name: str = "bridged", *, command: str = "/usr/bin/raven", args: list[str] | None = None, **extra) -> dict:
    entry = {"name": name, "command": command, "args": args if args is not None else ["mcp", "bridge", "/tmp/s.sock"]}
    entry.setdefault("env", [])
    entry.update(extra)
    return entry


@pytest.fixture
def rig():
    return _Rig()


@pytest.fixture
def stored_rig(tmp_path):
    """A rig whose session manager already holds one ``acp:`` conversation.

    ``session/load`` and ``session/resume`` both refuse an id this channel did
    not mint, so a stored session is what makes those two reachable at all.
    """
    from raven.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    session = manager.get_or_create("acp:old")
    manager.save(session)
    return _Rig(session_manager=manager)


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

    async def test_a_session_load_can_bring_them_too(self, stored_rig) -> None:
        await stored_rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            response = await stored_rig.call(
                "session/load", {"sessionId": "acp:old", "cwd": "/tmp", "mcpServers": [_stanza()]}
            )

        assert "error" not in response, response
        assert stored_rig.offered_to("acp:old") == {"read_file", "mcp_bridged_probe"}

    async def test_a_session_resume_can_bring_them_too(self, stored_rig) -> None:
        """The field is on all three session requests. Resume used to ignore it,
        which is the one outcome the old refusal existed to prevent: a promptable
        session whose client believes it has tools."""
        await stored_rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            response = await stored_rig.call(
                "session/resume", {"sessionId": "acp:old", "cwd": "/tmp", "mcpServers": [_stanza()]}
            )

        assert "error" not in response, response
        assert stored_rig.offered_to("acp:old") == {"read_file", "mcp_bridged_probe"}


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

    async def test_an_engine_with_no_registry_to_scope_it_drops_the_field(self) -> None:
        """An engine that cannot scope them means no turn could ever reach the
        tools, so the field does nothing. It is dropped and the session is minted:
        the session is the dispatch, and the servers were an attachment to it.
        """
        rig = _Rig(engineless=True)
        await rig.handshake()

        response = await rig.new_session([_stanza()])

        assert "error" not in response, response
        assert rig.sessions.sessions() != (), "the session is the dispatch and must survive"


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

    async def test_nothing_is_registered_process_wide(self, rig) -> None:
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            await rig.new_session([_stanza()])

        assert rig.tools.names() == ["read_file"]
        assert rig.tools.get("mcp_bridged_probe") is None


class TestTheReclaim:
    async def test_aclose_hides_and_disconnects_every_session_s(self, rig) -> None:
        """This surface's only reclaim point. Each connection is a subprocess of
        ours, and with no ``session/close`` nothing else would kill them."""
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            first = (await rig.new_session([_stanza("one")]))["result"]["sessionId"]
            second = (await rig.new_session([_stanza("two")]))["result"]["sessionId"]

        await rig.methods.aclose()

        assert rig.methods._session_mcp == {}
        assert all(not stack._exit_callbacks for stack in upstream.stacks)
        assert rig.offered_to(first) == {"read_file"}
        assert rig.offered_to(second) == {"read_file"}

    async def test_aclose_is_idempotent(self, rig) -> None:
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            await rig.new_session([_stanza()])

        await rig.methods.aclose()
        await rig.methods.aclose()

        assert rig.methods._session_mcp == {}

    async def test_re_adopting_replaces_the_earlier_set(self, stored_rig) -> None:
        """The other release point: ``session/resume`` on a session that already
        brought servers means the new set, and the old connections are nobody's."""
        await stored_rig.handshake()
        first = _Upstream(tools=("alpha",))
        second = _Upstream(tools=("beta",))

        with patch(_PATCH, new=first):
            await stored_rig.call("session/resume", {"sessionId": "acp:old", "mcpServers": [_stanza()]})
        with patch(_PATCH, new=second):
            await stored_rig.call("session/resume", {"sessionId": "acp:old", "mcpServers": [_stanza()]})

        assert stored_rig.offered_to("acp:old") == {"read_file", "mcp_bridged_beta"}
        assert not first.stacks[0]._exit_callbacks

    async def test_the_server_calls_aclose_on_the_way_out(self) -> None:
        """The wiring, not the method: a reclaim nothing calls is not a reclaim.

        ``serve`` is driven to EOF with a stub loop, and the registry it hands the
        methods layer is the stub's own -- which is also what makes the
        ``mcpServers`` field acceptable in the first place.
        """
        import asyncio
        import io

        from raven.acp.loops import AcpLoops
        from raven.acp.server import serve

        tools = ToolRegistry()
        tools.register(_Stub("read_file"))

        class _StubLoop:
            def __init__(self, _conversation: str | None = None) -> None:
                self.tools = tools
                self.sessions = None
                self.workspace = None

        reader = asyncio.StreamReader()
        for frame in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": "/tmp", "mcpServers": [_stanza()]},
            },
        ):
            reader.feed_data(protocol.encode(frame))
        reader.feed_eof()

        upstream = _Upstream()
        with patch(_PATCH, new=upstream):
            # One engine for every session (``AcpLoops.single``): the shape where
            # the session scoping is the only thing holding two sessions apart,
            # and the one whose registry this test can reach from out here.
            await serve(reader, io.BytesIO(), loops=AcpLoops.single(_StubLoop()))

        assert upstream.stacks, "the session's servers were never connected"
        assert not upstream.stacks[0]._exit_callbacks, "serve() exited without reclaiming them"
        assert tools._session_tools == {}


class TestAnEmptyReplacementIsStillAReplacement:
    """An empty set is one of the values a replacement can take, not a no-op.

    ``session/load`` and ``session/resume`` carry the dispatch's whole server
    list, so a session reopened with none is being told it has none -- and the
    dispatcher has already closed the endpoints the earlier set pointed at.
    Leaving them bound would offer the next turn a tool whose socket is gone.

    Both shapes say it: an explicit empty list, and the field left out. They meet
    in ``_validated_mcp_servers``, which maps either to an empty mapping.
    """

    @staticmethod
    def _spy_on_close(stack) -> list[str]:
        """Record that ``aclose`` ran, not merely that the reference went away."""
        closed: list[str] = []
        inherited = stack.aclose

        async def _aclose() -> None:
            closed.append("aclose")
            await inherited()

        stack.aclose = _aclose
        return closed

    async def _first_dispatch(self, stored_rig) -> list[str]:
        await stored_rig.handshake()
        upstream = _Upstream(tools=("alpha",))
        with patch(_PATCH, new=upstream):
            response = await stored_rig.call("session/resume", {"sessionId": "acp:old", "mcpServers": [_stanza()]})
        assert "error" not in response, response
        assert stored_rig.offered_to("acp:old") == {"read_file", "mcp_bridged_alpha"}
        return self._spy_on_close(upstream.stacks[0])

    async def test_an_explicit_empty_list_hides_the_earlier_tools(self, stored_rig) -> None:
        closed = await self._first_dispatch(stored_rig)

        response = await stored_rig.call("session/resume", {"sessionId": "acp:old", "mcpServers": []})

        assert "error" not in response, response
        assert stored_rig.offered_to("acp:old") == {"read_file"}
        assert closed == ["aclose"]
        assert "acp:old" not in stored_rig.methods._session_mcp

    async def test_the_field_left_out_says_the_same_thing(self, stored_rig) -> None:
        closed = await self._first_dispatch(stored_rig)

        response = await stored_rig.call("session/resume", {"sessionId": "acp:old"})

        assert "error" not in response, response
        assert stored_rig.offered_to("acp:old") == {"read_file"}
        assert closed == ["aclose"]
        assert "acp:old" not in stored_rig.methods._session_mcp

    async def test_a_load_with_none_drops_them_too(self, stored_rig) -> None:
        closed = await self._first_dispatch(stored_rig)

        response = await stored_rig.call("session/load", {"sessionId": "acp:old", "cwd": "/tmp", "mcpServers": []})

        assert "error" not in response, response
        assert stored_rig.offered_to("acp:old") == {"read_file"}
        assert closed == ["aclose"]


class TestOneEnginePerSession:
    """The deployed shape: ``AcpLoops`` builds an engine per conversation, so a
    session's servers are scoped in the registry that engine owns."""

    async def test_the_tools_land_in_that_sessions_own_engine(self) -> None:
        rig = _Rig(shared=False)
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            brought = (await rig.new_session([_stanza("one")]))["result"]["sessionId"]
        bare = (await rig.new_session([]))["result"]["sessionId"]

        assert "mcp_one_probe" in rig.offered_to(brought)
        assert rig.offered_to(bare) == {"read_file"}
        # Not merely invisible to the sibling -- absent from its registry, which
        # is the separation the per-session engines add underneath the scoping.
        assert "mcp_one_probe" not in set(rig.registry_for(bare).names())

    async def test_an_evicted_engine_does_not_cost_the_session_its_tools(self) -> None:
        """``AcpLoops`` drops the engine of an *idle* session to stay under its
        cap and builds a new one on the session's next turn. The connections are
        held per session and survive that; the binding lived in the registry that
        went with the old engine, so it has to be made again.

        Without the rebind the tools are gone between two turns of a stateful
        session that did nothing wrong, and the turn reports no tool rather than
        an error -- which reads as the server having nothing to offer.
        """
        rig = _Rig(shared=False)
        await rig.handshake()

        upstream = _Upstream()
        with patch(_PATCH, new=upstream):
            session_id = (await rig.new_session([_stanza("one")]))["result"]["sessionId"]
        assert "mcp_one_probe" in rig.offered_to(session_id)

        rig.evict(session_id)
        rebuilt = await rig.methods._open_session(session_id)
        rig.methods.rebind_session_mcp(rebuilt, session_id)

        assert "mcp_one_probe" in rig.offered_to(session_id)
        # Rebound, not reconnected: a second connect would spawn a second bridge
        # against an endpoint the dispatcher opened once.
        assert len(upstream.stacks) == 1

    async def test_a_session_that_brought_none_is_not_rebound(self) -> None:
        """Which is every session on a connection whose dispatcher configured no
        per-session servers, so the hook has to be free on that path."""
        rig = _Rig(shared=False)
        await rig.handshake()
        session_id = (await rig.new_session([]))["result"]["sessionId"]

        rig.evict(session_id)
        rebuilt = await rig.methods._open_session(session_id)
        rig.methods.rebind_session_mcp(rebuilt, session_id)

        assert rig.offered_to(session_id) == {"read_file"}


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

    def test_the_ask_user_extension_is_still_declared_beside_it(self) -> None:
        """Same ``_meta`` object; adding one key must not have replaced the other."""
        assert agent_capabilities()["_meta"]["raven"] == {"askUser": True}

    def test_and_the_transports_it_does_not_take_are_still_false(self) -> None:
        assert agent_capabilities()["mcpCapabilities"] == {"http": False, "sse": False}


class TestTheScopeIsOpenedWhereTheTurnRuns:
    """The third leg. Binding the tools and declaring the capability are worth
    nothing if the scope is opened in the wrong place.

    The ACP prompt handler cannot open it: it calls ``submit``, and the turn then
    runs on a task that inherits no context from the handler. So
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

        await loop.run_turn(self._request("acp:one"), None, None, stream=False, usage_sink=usage, text_sink=text)

        assert loop.seen["kwargs"] == {"stream": False, "usage_sink": usage, "text_sink": text}
