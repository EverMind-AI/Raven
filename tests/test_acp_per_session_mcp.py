"""The MCP servers a session brings to raven-as-a-sub-agent.

Raven used to refuse ``mcpServers`` outright, which made it the one ACP agent of
the four that could not be handed a server -- and the refusal's stated reason was
half right: MCP is connected once per process, and nothing scoped a server to one
session. The second half is what these pin down.

What arrives is not a server definition in any interesting sense: it is a stdio
stanza pointing at an endpoint the dispatcher opened, holding no credentials, and
the socket's mode is the whole of the boundary. So the tests here are about the
two things raven still owns -- connecting what it was handed, and making it
reachable from that session's turns and nowhere else -- plus the reclaim, because
a connection nothing closes is a subprocess nothing kills.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from raven.acp.methods import AcpMethods
from raven.acp.updates import UpdateTranslator
from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry
from raven.mcp.naming import MCPToolRef
from raven.rpc.dispatcher import Dispatcher
from raven.session.manager import SessionManager
from tests.acp_schema import validate_def

_PATCH = "raven.mcp.manager.connect_mcp_server"


class _Caps:
    def __init__(self) -> None:
        self.resources = None
        self.prompts = None
        self.tools = object()


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
    """A stand-in for ``connect_mcp_server`` that records what it was asked to connect.

    Registers with an origin, the way a real connect does: the manager reads its
    own teardown list back out of the registry's origin index, so a stub without
    one is invisible to the disconnect these tests assert.
    """

    def __init__(self, *, tools: tuple[str, ...] = ("probe",), fail: Exception | None = None) -> None:
        self.tools = tools
        self.fail = fail
        self.connected: list[tuple[str, Any]] = []

    async def __call__(self, name, cfg, registry, stack, executor=None, http_auth=None):
        from raven.mcp.client import Connected

        self.connected.append((name, cfg))
        if self.fail is not None:
            raise self.fail
        names = []
        for tool in self.tools:
            full = f"mcp_{name}_{tool}"
            registry.register(_Stub(full), origin=MCPToolRef(name=full, server=name, tool=tool))
            names.append(full)
        return Connected(names=names, session=object(), capabilities=_Caps())


class _Stack:
    """A real dispatcher carrying the handlers the ACP layer calls."""

    def __init__(self) -> None:
        self.dispatcher = Dispatcher()
        self.calls: list[tuple[str, dict]] = []
        self.subscriptions = iter(f"sub-{n}" for n in range(1, 100))
        self.stored: dict[str, list[dict]] = {}
        self.dispatcher.register("turn.subscribe", self._subscribe)
        self.dispatcher.register("turn.unsubscribe", self._unsubscribe)
        self.dispatcher.register("turn.cancel", self._cancel)
        self.dispatcher.register("session.resume", self._resume)

    async def _subscribe(self, params: dict) -> dict:
        self.calls.append(("turn.subscribe", params))
        return {"subscription_id": next(self.subscriptions)}

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
        params = {"cwd": str(self.tmp_path / "project"), "mcpServers": servers or []}
        # Held to the published schema, so a stanza shape these tests accept is
        # one a real client can actually send.
        validate_def("NewSessionRequest", params)
        return await self.call("session/new", params)

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
    monkeypatch.setattr("raven.acp.methods.load_config", lambda: config, raising=False)

    stack = _Stack()
    engine = _Engine(tmp_path / "ws")
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

    async def test_an_empty_list_is_still_the_normal_value(self, rig) -> None:
        await rig.handshake()

        response = await rig.new_session([])

        assert "error" not in response, response
        assert rig.offered_to(response["result"]["sessionId"]) == {"read_file"}

    async def test_a_session_load_can_bring_them_too(self, rig) -> None:
        """The schema requires the field on ``session/load`` as well, and a client
        that reopens a session has the same servers to offer it."""
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


class TestOnlyThatSessionCanSeeThem:
    async def test_the_tools_are_reachable_from_that_sessions_turn(self, rig) -> None:
        await rig.handshake()

        with patch(_PATCH, new=_Upstream(tools=("probe", "other"))):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]

        assert rig.offered_to(session_id) == {"read_file", "mcp_bridged_probe", "mcp_bridged_other"}

    async def test_and_from_nowhere_else_on_the_same_connection(self, rig) -> None:
        """The isolation the refusal used to stand in for: one registry serves
        every session here, so registering them would hand them to the next one."""
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            first = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        second = (await rig.new_session([]))["result"]["sessionId"]

        assert rig.offered_to(second) == {"read_file"}
        assert rig.engine.tools.get("mcp_bridged_probe") is None, "a session tool must not be process-wide"
        assert rig.offered_to(first) == {"read_file", "mcp_bridged_probe"}

    async def test_two_sessions_bringing_the_same_server_name_get_their_own(self, rig) -> None:
        await rig.handshake()
        upstream = _Upstream()

        with patch(_PATCH, new=upstream):
            first = (await rig.new_session([_stanza(args=["mcp", "bridge", "/tmp/one.sock"])]))["result"]["sessionId"]
            second = (await rig.new_session([_stanza(args=["mcp", "bridge", "/tmp/two.sock"])]))["result"]["sessionId"]

        assert first != second
        assert [cfg.args[-1] for _name, cfg in upstream.connected] == ["/tmp/one.sock", "/tmp/two.sock"]
        assert rig.offered_to(first) == {"read_file", "mcp_bridged_probe"}
        assert rig.offered_to(second) == {"read_file", "mcp_bridged_probe"}


class TestTheyAreReclaimed:
    async def test_session_close_disconnects_and_hides_them(self, rig) -> None:
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        manager = rig.methods._session_mcp[session_id]
        await rig.call("session/close", {"sessionId": session_id})

        assert rig.offered_to(session_id) == {"read_file"}
        assert manager.status() == [], "the connection outlived the session that opened it"
        assert session_id not in rig.methods._session_mcp

    async def test_session_delete_does_too(self, rig) -> None:
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        manager = rig.methods._session_mcp[session_id]
        response = await rig.call("session/delete", {"sessionId": session_id})

        assert "error" not in response, response
        assert manager.status() == []
        assert rig.offered_to(session_id) == {"read_file"}

    async def test_the_connection_teardown_reclaims_what_no_session_closed(self, rig) -> None:
        """A client that closes its window closes no session first, so this sweep
        is the only thing between it and a leaked subprocess."""
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            session_id = (await rig.new_session([_stanza()]))["result"]["sessionId"]
        manager = rig.methods._session_mcp[session_id]
        await rig.methods.unsubscribe_all()

        assert manager.status() == []
        assert rig.methods._session_mcp == {}
        assert rig.offered_to(session_id) == {"read_file"}

    async def test_reloading_a_session_replaces_its_servers(self, rig) -> None:
        await rig.handshake()
        rig.stack.stored["acp:old"] = []
        params = {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "project")}

        with patch(_PATCH, new=_Upstream(tools=("first",))):
            await rig.call("session/load", {**params, "mcpServers": [_stanza()]})
        first_manager = rig.methods._session_mcp["acp:old"]
        with patch(_PATCH, new=_Upstream(tools=("second",))):
            await rig.call("session/load", {**params, "mcpServers": [_stanza()]})

        assert first_manager.status() == [], "the first load's connection was left running"
        assert rig.offered_to("acp:old") == {"read_file", "mcp_bridged_second"}

    @pytest.mark.parametrize("method", ["session/load", "session/resume"])
    async def test_reopening_a_session_with_an_empty_list_releases_them(self, rig, monkeypatch, method) -> None:
        """An empty replacement is a replacement, not a request to keep what was there.

        A stateful dispatch reopens the session it left, and the list it carries
        is the current dispatch's, not a delta -- so a turn that was granted no
        servers sends an empty one. The dispatcher closed the previous dispatch's
        endpoints when it ended, which makes leaving the old set bound the worse
        of the two failures available: the session is offered tools whose sockets
        are gone, so the model picks one and the call fails, rather than the model
        never being offered it.
        """
        await rig.handshake()
        rig.stack.stored["acp:old"] = []
        params = {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "project")}

        with patch(_PATCH, new=_Upstream(tools=("first",))):
            await rig.call(method, {**params, "mcpServers": [_stanza()]})
        first_manager = rig.methods._session_mcp["acp:old"]
        assert rig.offered_to("acp:old") == {"read_file", "mcp_bridged_first"}
        # Named rather than inferred from ``status()``: the reclaim is what closes
        # the bridge subprocess, and a manager emptied some other way would leave
        # one running with nothing pointing at it.
        closed: list[str] = []
        real_aclose = first_manager.aclose

        async def spy_aclose() -> None:
            closed.append("acp:old")
            await real_aclose()

        monkeypatch.setattr(first_manager, "aclose", spy_aclose)

        response = await rig.call(method, {**params, "mcpServers": []})

        assert "error" not in response, response
        assert rig.offered_to("acp:old") == {"read_file"}, "the released set is still offered to the next turn"
        assert closed == ["acp:old"], "the previous connection was never closed"
        assert first_manager.status() == []
        assert "acp:old" not in rig.methods._session_mcp, "the previous manager is still held"

    async def test_reopening_a_session_without_the_field_releases_them_too(self, rig) -> None:
        """Omission is not a third state here.

        ``mcpServers`` is required on these requests and an empty array is its
        normal value, so a client that leaves it out has said no more than one
        that sends ``[]`` -- and the same endpoints are gone either way. Keeping
        the old set for the omitting client would make the staler of the two
        outcomes the one raven picks when it was told least.
        """
        await rig.handshake()
        rig.stack.stored["acp:old"] = []
        params = {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "project")}

        with patch(_PATCH, new=_Upstream(tools=("first",))):
            await rig.call("session/load", {**params, "mcpServers": [_stanza()]})
        first_manager = rig.methods._session_mcp["acp:old"]

        response = await rig.call("session/load", params)

        assert "error" not in response, response
        assert rig.offered_to("acp:old") == {"read_file"}
        assert first_manager.status() == []
        assert "acp:old" not in rig.methods._session_mcp


class TestWhatIsDroppedAndWhatIsNot:
    """A stanza this build cannot serve costs the session those tools, never the
    session. The servers are a capability a dispatcher attached on top of the
    task; answering ``session/new`` with an error instead fails the whole
    dispatch -- the sub-agent never runs -- over an optional attachment.
    """

    async def test_an_http_stanza_is_dropped(self, rig) -> None:
        """``mcpCapabilities`` says http is not offered. Honouring one anyway
        would put a server definition, and its credentials, in this process."""
        await rig.handshake()

        response = await rig.call(
            "session/new",
            {
                "cwd": str(rig.tmp_path / "project"),
                "mcpServers": [{"type": "http", "name": "remote", "url": "https://svc.test/mcp", "headers": []}],
            },
        )

        assert "error" not in response, response
        assert rig.offered_to(response["result"]["sessionId"]) == {"read_file"}

    async def test_a_stanza_with_no_command_is_dropped(self, rig) -> None:
        await rig.handshake()

        response = await rig.call(
            "session/new",
            {"cwd": str(rig.tmp_path / "project"), "mcpServers": [{"name": "bridged", "args": [], "env": []}]},
        )

        assert "error" not in response, response
        assert rig.offered_to(response["result"]["sessionId"]) == {"read_file"}

    async def test_one_bad_stanza_does_not_take_the_good_one_with_it(self, rig) -> None:
        """Dropped per entry, not per field: a dispatcher that attached two
        servers and got one stanza wrong must still deliver the other."""
        await rig.handshake()

        with patch(_PATCH, new=_Upstream()):
            # `rig.call` rather than `new_session`: the payload is deliberately
            # invalid ACP, which the outgoing schema check would refuse before
            # this build ever saw it.
            response = await rig.call(
                "session/new",
                {"cwd": str(rig.tmp_path / "project"), "mcpServers": [{"name": "broken"}, _stanza("good")]},
            )

        assert "error" not in response, response
        assert "mcp_good_probe" in rig.offered_to(response["result"]["sessionId"])

    async def test_a_bad_stanza_still_mints_the_session(self, rig) -> None:
        """The session is the dispatch. It is built whatever the attachment did."""
        await rig.handshake()

        response = await rig.call(
            "session/new", {"cwd": str(rig.tmp_path / "project"), "mcpServers": [{"name": "bridged"}]}
        )

        assert "error" not in response, response
        assert rig.offered_to(response["result"]["sessionId"]) == {"read_file"}

    async def test_a_connection_with_no_registry_drops_the_field(self, rig) -> None:
        """An engine that never built a loop cannot scope anything, so the field
        does nothing -- but doing nothing must not fail the dispatch."""
        await rig.handshake()
        rig.methods._agent_loop = None

        response = await rig.new_session([_stanza()])

        assert "error" not in response, response
        assert rig.offered_to(response["result"]["sessionId"]) == {"read_file"}

    async def test_a_server_that_will_not_connect_costs_the_session_its_tools_only(self, rig) -> None:
        """One unreachable endpoint must not cost the dispatch its whole
        sub-agent: the dispatcher already reports which servers were degraded."""
        await rig.handshake()

        with patch(_PATCH, new=_Upstream(fail=RuntimeError("no such socket"))):
            response = await rig.new_session([_stanza()])

        assert "error" not in response, response
        session_id = response["result"]["sessionId"]
        assert rig.offered_to(session_id) == {"read_file"}
