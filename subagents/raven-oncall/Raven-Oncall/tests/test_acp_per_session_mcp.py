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

The isolation half is asserted twice on purpose: once through the registry (see
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
from raven.acp.updates import UpdateTranslator
from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry
from raven.session.manager import SessionManager
from raven.tui_rpc.dispatcher import Dispatcher

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


class _Stack:
    """The RPC handlers the ACP layer calls, on a real dispatcher."""

    def __init__(self) -> None:
        self.dispatcher = Dispatcher()
        self.calls: list[tuple[str, dict]] = []
        self.stored: dict[str, list[dict]] = {}
        self._next_sub = 0
        self.dispatcher.register("turn.subscribe", self._subscribe)
        self.dispatcher.register("turn.unsubscribe", self._unsubscribe)
        self.dispatcher.register("turn.cancel", self._cancel)
        self.dispatcher.register("session.resume", self._resume)

    async def _subscribe(self, params: dict) -> dict:
        self.calls.append(("turn.subscribe", params))
        self._next_sub += 1
        return {"subscription_id": f"sub-{self._next_sub}"}

    async def _unsubscribe(self, params: dict) -> dict:
        self.calls.append(("turn.unsubscribe", params))
        return {"unsubscribed": True}

    async def _cancel(self, params: dict) -> dict:
        self.calls.append(("turn.cancel", params))
        return {"cancelled": False}

    async def _resume(self, params: dict) -> dict:
        self.calls.append(("session.resume", params))
        requested = params.get("session_id")
        return {"session_id": requested, "info": {}, "messages": self.stored.get(requested, [])}


class _Engine:
    """What the ACP layer reads off the engine: the session store and the tool registry."""

    def __init__(self, workspace) -> None:
        self.sessions = SessionManager(workspace)
        self.tools = ToolRegistry()
        self.tools.register(_Stub("read_file"))


class _Rig:
    def __init__(self, methods, stack, engine, tmp_path) -> None:
        self.methods = methods
        self.stack = stack
        self.engine = engine
        self.tmp_path = tmp_path

    async def call(self, method: str, params: dict | None = None, *, request_id: int | str = 1):
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        return await self.methods.handle(frame)

    async def handshake(self):
        return await self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})

    async def new_session(self, servers: list[dict] | None = None) -> dict:
        return await self.call("session/new", {"cwd": str(self.tmp_path / "project"), "mcpServers": servers or []})

    def offered_to(self, session_id: str) -> set[str]:
        with self.engine.tools.session_scope_for(session_id):
            return {d["function"]["name"] for d in self.engine.tools.get_definitions()}


def _stanza(name: str = "bridged", *, command: str = "/usr/bin/raven", args: list[str] | None = None, **extra) -> dict:
    entry = {"name": name, "command": command, "args": args if args is not None else ["mcp", "bridge", "/tmp/s.sock"]}
    entry.setdefault("env", [])
    entry.update(extra)
    return entry


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from raven.config import load_config

    config = load_config()
    monkeypatch.setattr(type(config), "workspace_path", property(lambda self: tmp_path / "ws"))
    monkeypatch.setattr("raven.config.load_config", lambda: config)

    stack = _Stack()
    engine = _Engine(tmp_path / "ws")
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)
    methods = AcpMethods(
        dispatcher=stack.dispatcher,
        translator=UpdateTranslator(emit=lambda frame: None),
        emit=lambda frame: None,
        agent_loop=engine,
    )
    return _Rig(methods, stack, engine, tmp_path)


class TestTheFieldIsAccepted:
    async def test_a_session_can_bring_a_stdio_server(self, rig) -> None:
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            response = await rig.new_session([_stanza()])

        assert "error" not in response, response
        assert response["result"]["sessionId"]
        assert [name for name, _cfg in upstream.connected] == ["bridged"]

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

    async def test_a_session_load_can_bring_them_too(self, rig) -> None:
        await rig.handshake()
        rig.stack.stored["acp:old"] = []
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            response = await rig.call(
                "session/load",
                {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "project"), "mcpServers": [_stanza()]},
            )

        assert "error" not in response, response
        assert rig.offered_to("acp:old") == {"read_file", "mcp_bridged_probe"}

    async def test_a_session_resume_can_bring_them_too(self, rig) -> None:
        """The field is on all three session requests. Resume used to ignore it,
        which is the one outcome the old refusal existed to prevent: a promptable
        session whose client believes it has tools."""
        await rig.handshake()
        rig.stack.stored["acp:old"] = []
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            response = await rig.call(
                "session/resume",
                {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "project"), "mcpServers": [_stanza()]},
            )

        assert "error" not in response, response
        assert rig.offered_to("acp:old") == {"read_file", "mcp_bridged_probe"}


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

    async def test_a_connection_with_no_registry_to_scope_it_drops_the_field(self, rig, tmp_path) -> None:
        """No agent loop means no turn could ever reach the tools, so the field
        does nothing -- and doing nothing must not fail the dispatch."""
        rig.methods._agent_loop = None
        await rig.handshake()

        response = await rig.new_session([_stanza()])

        assert "error" not in response, response
        assert response["result"]["sessionId"]


class TestOneSessionOnly:
    async def test_a_sibling_session_is_not_offered_them(self, rig) -> None:
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
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

        assert rig.engine.tools.tool_names == ["read_file"]
        assert rig.engine.tools.get("mcp_bridged_probe") is None


class TestTheReclaim:
    async def test_close_hides_and_disconnects(self, rig) -> None:
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        stack = upstream.stacks[0]
        await rig.call("session/close", {"sessionId": session_id})

        assert rig.offered_to(session_id) == {"read_file"}
        assert stack._exit_callbacks is not None and not stack._exit_callbacks

    async def test_delete_reclaims_too(self, rig) -> None:
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        await rig.call("session/delete", {"sessionId": session_id})

        assert rig.offered_to(session_id) == {"read_file"}

    async def test_a_dropped_connection_reclaims_the_rest(self, rig) -> None:
        """The backstop. A client that exits without closing anything still owns
        subprocesses, and nothing else would reap them."""
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]

        await rig.methods.unsubscribe_all()

        assert rig.methods._session_mcp == {}
        assert rig.offered_to(session_id) == {"read_file"}

    async def test_re_adopting_replaces_the_earlier_set(self, rig) -> None:
        await rig.handshake()
        rig.stack.stored["acp:old"] = []

        with patch(_PATCH, new=_Upstream(tools=("alpha",))):
            await rig.call(
                "session/load",
                {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "project"), "mcpServers": [_stanza()]},
            )
        with patch(_PATCH, new=_Upstream(tools=("beta",))):
            await rig.call(
                "session/load",
                {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "project"), "mcpServers": [_stanza()]},
            )

        assert rig.offered_to("acp:old") == {"read_file", "mcp_bridged_beta"}

    async def test_close_is_safe_for_a_session_that_brought_nothing(self, rig) -> None:
        await rig.handshake()
        session_id = (await rig.new_session([]))["result"]["sessionId"]

        response = await rig.call("session/close", {"sessionId": session_id})

        assert "error" not in response, response


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

    async def _load(self, rig, params: dict) -> dict:
        return await rig.call("session/load", {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "project"), **params})

    async def _first_dispatch(self, rig) -> tuple[list[str], Any]:
        await rig.handshake()
        rig.stack.stored["acp:old"] = []
        upstream = _Upstream(tools=("alpha",))
        with patch(_PATCH, new=upstream):
            response = await self._load(rig, {"mcpServers": [_stanza()]})
        assert "error" not in response, response
        assert rig.offered_to("acp:old") == {"read_file", "mcp_bridged_alpha"}
        return self._spy_on_close(upstream.stacks[0]), upstream

    async def test_an_explicit_empty_list_hides_the_earlier_tools(self, rig) -> None:
        closed, _upstream = await self._first_dispatch(rig)

        response = await self._load(rig, {"mcpServers": []})

        assert "error" not in response, response
        assert rig.offered_to("acp:old") == {"read_file"}
        assert closed == ["aclose"]
        assert "acp:old" not in rig.methods._session_mcp

    async def test_the_field_left_out_says_the_same_thing(self, rig) -> None:
        closed, _upstream = await self._first_dispatch(rig)

        response = await self._load(rig, {})

        assert "error" not in response, response
        assert rig.offered_to("acp:old") == {"read_file"}
        assert closed == ["aclose"]
        assert "acp:old" not in rig.methods._session_mcp

    async def test_a_resume_with_none_drops_them_too(self, rig) -> None:
        closed, _upstream = await self._first_dispatch(rig)

        response = await rig.call(
            "session/resume", {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "project"), "mcpServers": []}
        )

        assert "error" not in response, response
        assert rig.offered_to("acp:old") == {"read_file"}
        assert closed == ["aclose"]


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

    The ACP prompt handler cannot open it: it calls ``turn.send``, which submits
    onto the spine, and the turn then runs on a task that inherits no context
    from the handler. So ``AgentLoop.run_turn`` opens it, keyed off the same
    session id the ACP layer bound the tools under.
    """

    @staticmethod
    def _loop(registry):
        from raven.agent.loop.main import AgentLoop

        class _Loop:
            def __init__(self) -> None:
                self.tools = registry
                self.seen: dict[str, Any] = {}

            def binding_for_session(self, session_key: str):
                # ``run_turn`` binds the session's model in the same statement it
                # opens the tool scope in; the binding itself is not what is
                # under test here.
                from raven.providers.binding import active_binding

                return active_binding()

            async def _run_turn(self, req, emit, drain, **kwargs):
                self.seen["offered"] = {d["function"]["name"] for d in self.tools.get_definitions()}
                self.seen["kwargs"] = kwargs
                return "outcome"

        loop = _Loop()
        loop.run_turn = AgentLoop.run_turn.__get__(loop, _Loop)
        return loop

    @staticmethod
    def _request(conversation: str | None, *, chat_id: str = "c1"):
        from raven.spine.message import ChatType, Source
        from raven.spine.turn import Origin, TurnRequest

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
        """``conversation`` is what the ACP layer sets, but the spine allows it to
        be absent, and the fallback has to agree with the key the tools were
        bound under or the scope opens on nothing."""
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
