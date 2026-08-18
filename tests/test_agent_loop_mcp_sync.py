"""The seam between a real ``AgentLoop`` and the MCP connection manager.

These use a real loop instance, not a fake one. The whole ``plug.*`` surface
calls ``loop.sync_mcp`` / ``loop.mcp_manager`` / ``loop._mcp_executor``, and a
test that stands those up itself proves nothing about whether the loop has them
-- which is exactly how an earlier revision shipped an RPC surface where every
one of those calls was an AttributeError.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from raven.agent.loop.main import AgentLoop
from raven.agent.tools.base import Tool
from raven.config.schema import MCPServerConfig
from raven.providers.base import LLMProvider

_PATCH = "raven.agent.tools.mcp_manager.connect_mcp_server"


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(self, messages, **kwargs):  # pragma: no cover - no turn is run here
        raise AssertionError("no completion in these tests")

    def get_default_model(self) -> str:
        return "stub"


class _FakeTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def _fake_connect(tool_names: list[str]):
    async def fake(name, cfg, registry, stack, executor=None, http_auth=None):
        out = []
        for t in tool_names:
            full = f"mcp_{name}_{t}"
            registry.register(_FakeTool(full))
            out.append(full)
        return out

    return fake


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _loop(workspace: Path, servers: dict | None = None, **kw) -> AgentLoop:
    return AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=1,
        mcp_servers=servers or {},
        **kw,
    )


def test_the_loop_exposes_exactly_what_the_plug_handlers_call(workspace) -> None:
    """`plug.*` reaches for these three by name; nothing else asserts they exist."""
    loop = _loop(workspace)

    assert callable(loop.sync_mcp)
    assert callable(loop._mcp_executor)
    manager = loop.mcp_manager
    for method in ("status", "connect", "disconnect", "sync", "aclose", "tool_map"):
        assert callable(getattr(manager, method)), method
    # plug.auth passes the executor provider straight through to connect().
    assert loop.mcp_manager is manager, "the manager must be stable across calls"


async def test_sync_mcp_attaches_a_server_while_the_loop_runs(workspace) -> None:
    loop = _loop(workspace)

    with patch(_PATCH, new=_fake_connect(["search"])):
        result = await loop.sync_mcp({"svc": MCPServerConfig(url="https://svc.test/mcp")})

    assert result["tools_changed"] is True
    assert loop.tools.has("mcp_svc_search")
    (snap,) = loop.mcp_manager.status()
    assert snap["state"] == "connected"


async def test_sync_mcp_detaches_a_server_the_config_dropped(workspace) -> None:
    loop = _loop(workspace)

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.sync_mcp({"svc": MCPServerConfig(url="https://svc.test/mcp")})
        await loop.sync_mcp({})

    assert not loop.tools.has("mcp_svc_search")
    assert loop.mcp_manager.status() == []


async def test_a_disabled_server_is_not_connected(workspace) -> None:
    """`enabled` has to be honoured by the path the loop actually runs.

    Writing the flag and never reading it means the market's toggle looks like it
    worked and changes nothing, now or after a restart.
    """
    cfg = MCPServerConfig(url="https://svc.test/mcp", enabled=False)
    loop = _loop(workspace, {"svc": cfg})

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop._connect_mcp()

    assert not loop.tools.has("mcp_svc_search")
    assert loop.mcp_manager.status() == []


async def test_disabling_a_connected_server_disconnects_it(workspace) -> None:
    loop = _loop(workspace)

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.sync_mcp({"svc": MCPServerConfig(url="https://svc.test/mcp")})
        assert loop.tools.has("mcp_svc_search")
        await loop.sync_mcp({"svc": MCPServerConfig(url="https://svc.test/mcp", enabled=False)})

    assert not loop.tools.has("mcp_svc_search")
    (snap,) = loop.mcp_manager.status()
    assert snap["state"] == "disconnected"
    assert snap["enabled"] is False


async def test_lazy_connect_still_runs_once(workspace) -> None:
    calls: list[str] = []

    def _counting(tool_names):
        inner = _fake_connect(tool_names)

        async def fake(name, *a, **k):
            calls.append(name)
            return await inner(name, *a, **k)

        return fake

    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})
    with patch(_PATCH, new=_counting(["search"])):
        await loop._connect_mcp()
        await loop._connect_mcp()

    assert calls == ["svc"]
    assert loop.tools.has("mcp_svc_search")


async def test_the_disabled_tools_blacklist_survives_a_connect(workspace) -> None:
    """An MCP server can register a blacklisted name, so the blacklist has to be
    re-applied after every connect -- not only after the first one."""
    loop = _loop(workspace, disabled_tools=["mcp_svc_search"])

    with patch(_PATCH, new=_fake_connect(["search", "fetch"])):
        await loop.sync_mcp({"svc": MCPServerConfig(url="https://svc.test/mcp")})

    assert not loop.tools.has("mcp_svc_search")
    assert loop.tools.has("mcp_svc_fetch")
    (snap,) = loop.mcp_manager.status()
    # The record tracks what survived the blacklist, so a later disconnect stays
    # precise instead of unregistering a name it never owned.
    assert snap["tool_count"] == 1


async def test_close_mcp_detaches_everything(workspace) -> None:
    loop = _loop(workspace)

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.sync_mcp({"svc": MCPServerConfig(url="https://svc.test/mcp")})
    await loop.close_mcp()

    assert not loop.tools.has("mcp_svc_search")


async def test_the_loop_hands_the_manager_its_executor_provider(workspace) -> None:
    """A stdio server inside a sandbox needs the executor; dropping the provider
    would connect it outside the sandbox instead, silently."""
    loop = _loop(workspace)
    asked: list[bool] = []
    real = loop._mcp_executor

    async def _recording():
        asked.append(True)
        return await real()

    loop._mcp_executor = _recording

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.sync_mcp({"svc": MCPServerConfig(url="https://svc.test/mcp")})

    assert asked == [True]


async def test_state_changes_reach_the_event_sink(workspace) -> None:
    """The client listens for `mcp.status`; nothing used to send it.

    Without this an OAuth connect cannot be completed from a browser at all: the
    authorization URL only reaches the gateway host's own `webbrowser.open`, and
    the page has no way to learn the connect finished.
    """
    loop = _loop(workspace)
    seen: list[tuple] = []

    async def _sink(method, params):
        seen.append((method, params))

    loop.set_mcp_event_sink(_sink)

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.sync_mcp({"svc": MCPServerConfig(url="https://svc.test/mcp")})
    import asyncio

    await asyncio.sleep(0)  # the bridge is fire-and-forget

    methods = [m for m, _ in seen]
    assert methods.count("mcp.status") >= 2, methods  # connecting, then connected
    states = [p["state"] for m, p in seen if m == "mcp.status"]
    assert states[0] == "connecting" and states[-1] == "connected", states
    assert seen[-1][1]["name"] == "svc"
    assert seen[-1][1]["tool_count"] == 1


async def test_the_oauth_flow_reaches_the_same_sink(workspace) -> None:
    """`oauth.pending` carries the URL the user has to visit, so it has to leave
    the process the same way a status change does."""
    loop = _loop(workspace)
    seen: list[tuple] = []

    async def _sink(method, params):
        seen.append((method, params))

    loop.set_mcp_event_sink(_sink)
    manager = loop.mcp_manager
    assert manager.on_oauth_event is not None
    manager.on_oauth_event("oauth.pending", {"server": "svc", "url": "https://idp.example/authorize?x=1"})
    import asyncio

    await asyncio.sleep(0)

    assert seen == [("oauth.pending", {"server": "svc", "url": "https://idp.example/authorize?x=1"})]


async def test_a_turn_stops_waiting_for_a_server_that_is_waiting_on_a_person(workspace) -> None:
    """The bound a turn puts on the first connect.

    A server parked at the browser-authorization step blocks on a human for up
    to the OAuth flow's timeout, and the manager connects servers one after
    another -- so without this bound one unanswered park held every later
    server and the user's message behind them.
    """
    import asyncio

    released = asyncio.Event()

    async def parked(name, cfg, registry, stack, executor=None, http_auth=None):
        await released.wait()
        registry.register(_FakeTool(f"mcp_{name}_late"))
        return [f"mcp_{name}_late"]

    loop = _loop(workspace, {"parks": MCPServerConfig(url="https://parks.test/mcp")})

    with patch(_PATCH, new=parked):
        await asyncio.wait_for(loop._connect_mcp(wait=0.05), timeout=5)
        # The turn is free to go, and the connect was not cancelled to get there.
        assert not loop.tools.has("mcp_parks_late")
        assert loop._mcp_connecting is True

        released.set()
        for _ in range(200):
            if loop.tools.has("mcp_parks_late"):
                break
            await asyncio.sleep(0.01)

    # It finished on its own afterwards, so its tools are there for the next turn.
    assert loop.tools.has("mcp_parks_late")
    assert loop._mcp_connected is True


async def test_an_unbounded_caller_still_waits_for_the_whole_sync(workspace) -> None:
    """`run()` has nothing else to do and keeps the old behaviour, which is also
    what keeps a SandboxInitError reaching its handler there."""
    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop._connect_mcp()

    assert loop.tools.has("mcp_svc_search")
    assert loop._mcp_connected is True
    assert loop._mcp_connecting is False


# ── An unauthorized plugin says so in the turn's context ───────────


async def test_an_auth_required_server_surfaces_in_the_tool_notices(workspace) -> None:
    """A server whose tools are absent because it awaits authorization must be
    named in the runtime context, or the model reads the gap as a missing
    capability and tells the user it cannot be done."""
    from raven.agent.tools.mcp_oauth import OAuthWaitTimeoutError

    async def refused(name, cfg, registry, stack, executor=None, http_auth=None):
        raise OAuthWaitTimeoutError("nobody clicked")

    loop = _loop(workspace, {"asana": MCPServerConfig(url="https://asana.test/mcp", auth="oauth")})
    with patch(_PATCH, new=refused):
        await loop._connect_mcp()

    assert loop.mcp_manager.status()[0]["state"] == "auth_required"
    notes = loop._mcp_tool_notices()
    assert len(notes) == 1
    assert "asana" in notes[0]
    assert "plugin panel" in notes[0]


async def test_healthy_and_absent_managers_produce_no_notices(workspace) -> None:
    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})
    assert loop._mcp_tool_notices() == []

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop._connect_mcp()
    assert loop._mcp_tool_notices() == []


async def test_tool_notices_ride_the_runtime_context_block(workspace) -> None:
    from datetime import datetime

    from raven.context_engine.segments import render

    block = render.build_runtime_context(
        lambda: datetime(2026, 8, 17, 12, 0),
        "tui",
        "abc",
        tool_notices=["MCP plugin 'asana' is installed but awaiting authorization."],
    )
    assert block.startswith(render.RUNTIME_CONTEXT_TAG)
    assert "awaiting authorization" in block

    plain = render.build_runtime_context(lambda: datetime(2026, 8, 17, 12, 0), "tui", "abc")
    assert "awaiting authorization" not in plain


async def test_the_assembler_asks_the_loop_for_notices_each_turn(workspace) -> None:
    from raven.context_engine.assembler import ContextAssembler
    from raven.context_engine.base import AssemblyContext
    from raven.memory_engine.base import TokenBudget

    notes = ["MCP plugin 'x' is installed but awaiting authorization."]
    asm = ContextAssembler([], get_tool_definitions=lambda: [], get_tool_notices=lambda: notes)
    ctx = AssemblyContext(
        session_key="tui:t",
        current_message="hi",
        media=None,
        channel="tui",
        chat_id="t",
        session_messages=[],
        budget=TokenBudget(
            context_length=8192,
            reserved_output=1024,
            reserved_tools=0,
            reserved_system=0,
            available_history=4096,
        ),
    )
    user = asm._build_user(ctx)
    assert "awaiting authorization" in user["content"]

    notes.clear()
    assert "awaiting authorization" not in asm._build_user(ctx)["content"]
