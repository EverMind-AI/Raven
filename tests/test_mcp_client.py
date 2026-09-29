"""MCP tool wrapper result verdicts (ok flag on failures)."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import MCPServerConfig
from raven.contracts.tool import ToolResult
from raven.mcp.client import MCPToolWrapper, connect_mcp_server


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
        inputSchema={"type": "object", "properties": {}},
    )
    return MCPToolWrapper(session, "server-x", tool_def, tool_timeout=30, taken=frozenset())


def _result(*, is_error: bool, text: str) -> SimpleNamespace:
    from mcp.types import TextContent

    return SimpleNamespace(isError=is_error, content=[TextContent(type="text", text=text)])


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

        async def list_tools(self, cursor=None):
            asked.append(cursor)
            return pages[cursor]

    return _FakeSession, asked


def _tool_def(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=name, inputSchema={"type": "object", "properties": {}})


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
        None: SimpleNamespace(tools=[_tool_def("a"), _tool_def("b")], nextCursor="page-2"),
        "page-2": SimpleNamespace(tools=[_tool_def("c")], nextCursor=None),
    }

    connected, asked, reg = await _connect(monkeypatch, pages)

    assert connected.names == ["mcp_srv_a", "mcp_srv_b", "mcp_srv_c"]
    assert asked == [None, "page-2"]
    assert all(reg.has(name) for name in connected.names)


async def test_a_repeated_cursor_stops_paging(monkeypatch) -> None:
    pages = {
        None: SimpleNamespace(tools=[_tool_def("a")], nextCursor="same"),
        "same": SimpleNamespace(tools=[_tool_def("b")], nextCursor="same"),
    }

    connected, asked, _ = await _connect(monkeypatch, pages)

    assert connected.names == ["mcp_srv_a", "mcp_srv_b"]
    assert asked == [None, "same"]
