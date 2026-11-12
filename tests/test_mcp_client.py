"""MCP tool wrapper result verdicts (ok flag on failures)."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent

from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import MCPServerConfig
from raven.contracts.tool import ToolResult
from raven.mcp.client import MCPToolWrapper, _host_sourced, connect_mcp_server, open_mcp_transport


class _Session:
    def __init__(self, outcome: object | Exception) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict) -> object:
        self.calls.append((name, arguments))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _wrapper(session: _Session) -> MCPToolWrapper:
    tool_def = SimpleNamespace(
        name="read",
        description="read something",
        input_schema={"type": "object", "properties": {}},
    )
    return MCPToolWrapper(session, "server-x", tool_def, tool_timeout=30, taken=frozenset())


def _result(*, is_error: bool, text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)


async def test_an_is_error_result_carries_ok_false() -> None:
    session = _Session(_result(is_error=True, text="permission denied"))

    result = await _wrapper(session).execute(arguments={})

    assert isinstance(result, ToolResult)
    assert result.model_text == "permission denied"
    assert result.ok is False, "isError is the producer's verdict, not a text prefix"
    assert session.calls == [("read", {"arguments": {}})]


async def test_a_clean_result_stays_a_bare_string() -> None:
    session = _Session(_result(is_error=False, text="all good"))

    result = await _wrapper(session).execute(arguments={})

    assert result == "all good"
    assert isinstance(result, str)


async def test_a_timeout_reports_failure() -> None:
    session = _Session(asyncio.TimeoutError())

    result = await _wrapper(session).execute(arguments={})

    assert isinstance(result, ToolResult) and result.ok is False


async def test_a_generic_failure_reports_failure() -> None:
    session = _Session(RuntimeError("boom"))

    result = await _wrapper(session).execute(arguments={})

    assert isinstance(result, ToolResult) and result.ok is False


def test_the_wrapper_shows_a_call_as_its_server_tool_and_input() -> None:
    wrapper = _wrapper(_Session(None))

    assert wrapper.approval_kind == "mcp.call"
    assert wrapper.approval_evidence({"page": 3}) == {"server": "server-x", "tool": "read", "input": {"page": 3}}


# ---------------------------------------------------------------------------
# tools/list pagination
# ---------------------------------------------------------------------------


def _paged_session(pages: dict):
    """A ClientSession stand-in whose list_tools answers from ``pages`` by cursor."""

    asked: list = []

    class _FakeSession:
        def __init__(self, read, write) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def initialize(self):
            return SimpleNamespace(capabilities=SimpleNamespace(tools=True))

        async def list_tools(self, *, params=None):
            cursor = params.cursor if params else None
            asked.append(cursor)
            return pages[cursor]

    return _FakeSession, asked


def _tool_def(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=name, input_schema={"type": "object", "properties": {}})


@asynccontextmanager
async def _fake_transport(cfg, transport_type, executor, http_auth=None):
    yield object(), object()


async def _connect(monkeypatch, pages: dict) -> tuple:
    import mcp

    import raven.mcp.client as client_mod

    session_cls, asked = _paged_session(pages)
    monkeypatch.setattr(mcp, "ClientSession", session_cls)
    monkeypatch.setattr(client_mod, "open_mcp_transport", _fake_transport)

    cfg = MCPServerConfig(url="https://example.test/mcp")
    reg = ToolRegistry()
    async with AsyncExitStack() as stack:
        connected = await connect_mcp_server("srv", cfg, reg, stack)
    return connected, asked, reg


async def test_tools_list_pages_are_followed_until_the_cursor_ends(monkeypatch) -> None:
    pages = {
        None: SimpleNamespace(tools=[_tool_def("a"), _tool_def("b")], next_cursor="page-2"),
        "page-2": SimpleNamespace(tools=[_tool_def("c")], next_cursor=None),
    }

    connected, asked, reg = await _connect(monkeypatch, pages)

    assert connected.names == ["mcp_srv_a", "mcp_srv_b", "mcp_srv_c"]
    assert asked == [None, "page-2"]
    assert all(reg.has(name) for name in connected.names)


async def test_a_repeated_cursor_stops_paging(monkeypatch) -> None:
    pages = {
        None: SimpleNamespace(tools=[_tool_def("a")], next_cursor="same"),
        "same": SimpleNamespace(tools=[_tool_def("b")], next_cursor="same"),
    }

    connected, asked, _ = await _connect(monkeypatch, pages)

    assert connected.names == ["mcp_srv_a", "mcp_srv_b"]
    assert asked == [None, "same"]


async def test_a_streamable_http_server_is_reached_through_the_sdks_two_streams(monkeypatch) -> None:
    """mcp 2.x's ``streamable_http_client`` yields ``(read, write)``, not three.

    The fake keeps the SDK's real arity: an unpack of three raises before a
    session is built, which is how a ``streamableHttp`` server loses every tool
    while a fake that still yields three keeps the unit suite green.
    """
    import mcp
    import mcp.client.streamable_http

    entered: list[str] = []

    @asynccontextmanager
    async def two_stream_client(url, http_client):
        entered.append(url)
        yield object(), object()

    session_cls, _asked = _paged_session({None: SimpleNamespace(tools=[_tool_def("ping")], next_cursor=None)})
    monkeypatch.setattr(mcp, "ClientSession", session_cls)
    monkeypatch.setattr(mcp.client.streamable_http, "streamable_http_client", two_stream_client)

    cfg = MCPServerConfig(url="https://example.test/mcp")
    reg = ToolRegistry()
    async with AsyncExitStack() as stack:
        connected = await connect_mcp_server("srv", cfg, reg, stack)

    assert entered == ["https://example.test/mcp"]
    assert connected.names == ["mcp_srv_ping"]
    assert reg.has("mcp_srv_ping")


async def test_registry_respects_the_mcp_servers_declared_extra_property_policy() -> None:
    for closed in (True, False):
        session = _Session(_result(is_error=False, text="received"))
        tool_def = SimpleNamespace(
            name="read",
            description="Read a path.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": not closed,
            },
        )
        wrapper = MCPToolWrapper(session, "server-x", tool_def, tool_timeout=30, taken=frozenset())
        registry = ToolRegistry()
        registry.register(wrapper)
        args = {"path": "/synthetic", "show_hidden": True}
        result = await registry.execute(wrapper.name, args)
        if closed:
            assert "Invalid parameters" in result and "show_hidden is not allowed" in result
            assert session.calls == []
        else:
            assert result == "received"
            assert session.calls == [("read", args)]


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------


async def test_the_registry_probe_takes_a_host_servers_cwd_but_not_a_packages(monkeypatch) -> None:
    """A host server probes in its own cwd; a package server probes outside it."""
    seen: dict = {}

    async def _fake_launch(argv, env, cwd=None, **kwargs):
        seen["cwd"] = cwd
        seen["project"] = kwargs.get("project", True)
        return list(argv)

    @asynccontextmanager
    async def _fake_stdio(params, errlog=None):
        seen["params_cwd"] = params.cwd
        yield ("read", "write")

    monkeypatch.setattr("raven.utils.mirrors.package_launch_async", _fake_launch)
    monkeypatch.setattr("mcp.client.stdio.stdio_client", _fake_stdio)

    host = MCPServerConfig(type="stdio", command="npx", args=["server"], cwd="/proj")
    async with open_mcp_transport(host, "stdio"):
        pass

    assert seen["cwd"] == "/proj"
    assert seen["params_cwd"] == "/proj"
    assert seen["project"] is True, "the host's own directory is the host's own content"

    package = MCPServerConfig(type="stdio", command="npx", args=["server"], cwd="/pkg", source="package")
    seen.clear()
    async with open_mcp_transport(package, "stdio"):
        pass

    assert seen["cwd"] is None
    assert seen["params_cwd"] == "/pkg"
    assert seen["project"] is False, "a package root is third-party content: no project file is read"


def test_a_config_without_the_marker_is_the_hosts_own() -> None:
    """A stub that predates the source marker is the host's own, not a carried one."""
    assert _host_sourced(object()) is True
    assert _host_sourced(SimpleNamespace()) is True
    assert _host_sourced(SimpleNamespace(source="package")) is False
    assert _host_sourced(SimpleNamespace(source="playbook")) is False


def _node_denial():
    from raven.sandbox.denials import NetworkDenial

    return NetworkDenial("/usr/bin/node", "registry.example.com", 443, "transparent_tcp_policy_denied")


class _Sandbox:
    """A sandbox an MCP server runs in, and what it was refused while a call ran."""

    def __init__(self, denials: list) -> None:
        self.denials = denials
        self.reads: list[bool] = []
        self.granted: list = []

    async def denials_since(self, since: float, *, wait: bool = False) -> list:
        self.reads.append(wait)
        return list(self.denials)

    async def grant_network(self, grants) -> bool:
        self.granted.append(list(grants))
        return True

    async def sync_network(self) -> bool:
        return True


def _sandboxed(session: _Session, sandbox: _Sandbox) -> MCPToolWrapper:
    tool_def = SimpleNamespace(name="fetch", description="fetch", input_schema={"type": "object", "properties": {}})
    return MCPToolWrapper(session, "server-x", tool_def, tool_timeout=30, taken=frozenset(), executor=sandbox)


async def test_a_sandboxed_servers_refused_connection_is_named_with_its_result() -> None:
    from raven.permissions.turn import start_permission_turn

    start_permission_turn(None, conversation_id="c", turn_id="t")
    sandbox = _Sandbox([_node_denial()])
    result = await _sandboxed(_Session(_result(is_error=True, text="fetch failed")), sandbox).execute()
    assert isinstance(result, ToolResult) and result.ok is False
    assert result.model_text.startswith("fetch failed")
    assert (
        "[sandbox] The sandbox network policy blocked: /usr/bin/node -> registry.example.com:443." in result.model_text
    )
    assert sandbox.reads == [True]


async def test_a_clean_sandboxed_call_reads_its_denials_without_waiting() -> None:
    sandbox = _Sandbox([])
    result = await _sandboxed(_Session(_result(is_error=False, text="all good")), sandbox).execute()
    assert result == "all good" and sandbox.reads == [False]


async def test_a_timed_out_call_names_what_its_server_was_refused() -> None:
    from raven.permissions.turn import start_permission_turn

    start_permission_turn(None, conversation_id="c", turn_id="t")
    sandbox = _Sandbox([_node_denial()])
    result = await _sandboxed(_Session(asyncio.TimeoutError()), sandbox).execute()
    assert "timed out" in result.model_text and "registry.example.com:443" in result.model_text


async def test_a_stop_on_a_servers_refused_connection_ends_the_turn() -> None:
    from raven.config.schema import PermissionsConfig
    from raven.contracts.permissions import ApprovalChoice, ApprovalOutcome
    from raven.contracts.tool import Continuation
    from raven.permissions.builtin import BuiltinRulings
    from raven.permissions.gate import PermissionGate
    from raven.permissions.runtime import tool_body
    from raven.permissions.turn import start_permission_turn

    class Stop:
        async def await_approval(self, **kwargs):
            return ApprovalOutcome(ApprovalChoice.DENY_STOP)

    start_permission_turn(Stop(), conversation_id="c-stop", turn_id="t")
    gate = PermissionGate(config_source=lambda: PermissionsConfig(), builtin=BuiltinRulings())
    sandbox = _Sandbox([_node_denial()])
    with tool_body(gate):
        result = await _sandboxed(_Session(_result(is_error=True, text="fetch failed")), sandbox).execute()
    assert result.continuation is Continuation.ABORT_TURN


async def test_only_a_server_the_sandbox_started_has_its_refusals_read(monkeypatch) -> None:
    import raven.mcp.client as client_mod

    tool_def = SimpleNamespace(name="fetch", description="fetch", input_schema={"type": "object", "properties": {}})

    @asynccontextmanager
    async def fake_connection(cfg, transport_type, executor, http_auth=None):
        yield object(), SimpleNamespace(capabilities=None), [tool_def]

    monkeypatch.setattr(client_mod, "_mcp_server_connection", fake_connection)

    class _Spawning(_Sandbox):
        is_sandboxed = True
        supports_process_spawning = True

    sandbox = _Spawning([])
    for cfg, reads_from in (
        (MCPServerConfig(command="node", args=["server.js"]), sandbox),
        (MCPServerConfig(url="https://example.test/mcp"), None),
    ):
        reg = ToolRegistry()
        async with AsyncExitStack() as stack:
            connected = await connect_mcp_server("srv", cfg, reg, stack, executor=sandbox)
        (name,) = connected.names
        assert reg.get(name)._executor is reads_from
