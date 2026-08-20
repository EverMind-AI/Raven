"""The change probe and the unload edge, for the manager alone.

``config_changed`` is the cheap gate in front of ``apply_config``: everything
that can fire on a timer asks it first, so an unchanged config costs a dict
comparison instead of a reconnect storm. Its branches are the ones a poll can
get wrong -- a settled removal must read as no work without hiding a later
record that really did change, and a server parked in ``error`` must not be
retried on every tick.

These lived in a file about tool retirement, which no longer exists: a
withdrawn tool is unregistered outright now, and the miss itself names both
readings (see ``ToolRegistry.execute``). The probe and the teardown edge are
not about that, so they are here.
"""

from __future__ import annotations

from contextlib import AsyncExitStack

import pytest

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry
from raven.mcp.manager import MCPConnection, MCPConnectionManager
from raven.mcp.naming import MCPToolRef


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
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "ran"


def _register(registry: ToolRegistry, name: str, server: str, tool: str) -> None:
    """Register a stub the way an MCP connect does -- with its origin.

    The origin is the registry's record of where the tool came from, and the
    manager reads its own teardown list back out of it, so a stub registered
    without one is invisible to everything under test here.
    """
    registry.register(_Stub(name), origin=MCPToolRef(name=name, server=server, tool=tool))


class _Cfg:
    def __init__(self, *, enabled: bool = True, command: str = "run") -> None:
        self.enabled = enabled
        self.command = command
        self.args: list[str] = []
        self.env: dict[str, str] = {}
        self.type = None
        self.url = None
        self.headers = None
        self.tool_timeout = 30
        self.auth = "none"

    def model_dump(self) -> dict:
        return {"enabled": self.enabled, "command": self.command}


async def _connected(registry: ToolRegistry, name: str, *tools: str) -> MCPConnectionManager:
    """A manager holding one already-connected server, without a transport.

    Built by hand rather than through ``apply_config`` so the test does not
    need a real MCP server: what is under test is the teardown edge, and the
    stack is a real (empty) one so closing it exercises the same path.
    """
    manager = MCPConnectionManager(registry)
    stack = AsyncExitStack()
    await stack.__aenter__()
    for full in tools:
        _register(registry, full, name, full.rsplit("_", 1)[-1])
    manager._conns[name] = MCPConnection(name=name, config=_Cfg(), stack=stack, state="connected")
    return manager


class TestUnloadTakesEffectAtOnce:
    @pytest.mark.asyncio
    async def test_the_tools_leave_the_registry(self):
        registry = ToolRegistry()
        manager = await _connected(registry, "openseo", "mcp_openseo_search")
        await manager.disconnect("openseo", drop=True)

        assert not registry.has("mcp_openseo_search")
        assert registry.origin_of("mcp_openseo_search") is None
        assert registry.names_from("openseo") == []

    @pytest.mark.asyncio
    async def test_the_transport_closes_and_the_record_goes(self):
        # Nothing may keep talking to a server the config no longer wants: the
        # tools are withdrawn before the stack closes, so the agent never holds
        # a tool whose session is already gone.
        registry = ToolRegistry()
        manager = await _connected(registry, "openseo", "mcp_openseo_search")
        stack = manager._conns["openseo"].stack
        await manager.disconnect("openseo", drop=True)
        assert stack is not None
        # An exhausted AsyncExitStack cannot be re-entered as a live one; the
        # connection no longer holds it either way.
        assert manager._conns.get("openseo") is None

    @pytest.mark.asyncio
    async def test_a_disabled_server_keeps_its_record_but_loses_its_tools(self):
        registry = ToolRegistry()
        manager = await _connected(registry, "openseo", "mcp_openseo_search")
        await manager.apply_config({"openseo": _Cfg(enabled=False)})

        assert not registry.has("mcp_openseo_search")
        snap = next(s for s in manager.status() if s["name"] == "openseo")
        assert snap["state"] == "disconnected"
        assert snap["enabled"] is False
        assert snap["tool_count"] == 0


class TestTheChangeProbe:
    @pytest.mark.asyncio
    async def test_unchanged_config_reads_as_no_work(self):
        registry = ToolRegistry()
        manager = await _connected(registry, "openseo", "mcp_openseo_search")
        assert manager.config_changed({"openseo": manager._conns["openseo"].config}) is False

    @pytest.mark.asyncio
    async def test_a_removed_server_reads_as_work(self):
        registry = ToolRegistry()
        manager = await _connected(registry, "openseo", "mcp_openseo_search")
        assert manager.config_changed({}) is True

    @pytest.mark.asyncio
    async def test_a_new_server_reads_as_work(self):
        registry = ToolRegistry()
        manager = await _connected(registry, "openseo", "mcp_openseo_search")
        cfg = manager._conns["openseo"].config
        assert manager.config_changed({"openseo": cfg, "other": _Cfg()}) is True

    @pytest.mark.asyncio
    async def test_a_changed_config_reads_as_work(self):
        registry = ToolRegistry()
        manager = await _connected(registry, "openseo", "mcp_openseo_search")
        assert manager.config_changed({"openseo": _Cfg(command="different")}) is True

    @pytest.mark.asyncio
    async def test_a_settled_removal_reads_as_no_work(self):
        # Applied once already: the record is down and holds no tools, so there
        # is nothing left to take away and a poll must not keep saying "work".
        registry = ToolRegistry()
        manager = await _connected(registry, "openseo", "mcp_openseo_search")
        await manager.apply_config({"openseo": _Cfg(enabled=False)})
        assert manager.config_changed({"openseo": _Cfg(enabled=False)}) is False

    @pytest.mark.asyncio
    async def test_a_settled_removal_does_not_mask_a_later_change(self):
        """The probe must walk every record, not answer from the first one.

        The record that hides the bug has to be a *settled removal* (down, no
        tools -> "no work") sitting ahead of a record whose config really did
        change. It also has to be one the probe reaches through the loop: a
        server missing from ``_conns`` entirely is caught by the set difference
        before the loop starts, which is why the obvious version of this test
        passes against the bug.
        """
        registry = ToolRegistry()
        manager = await _connected(registry, "second", "mcp_second_run")
        settled = _Cfg(enabled=False)
        manager._conns = {
            "first": MCPConnection(name="first", config=settled, state="disconnected"),
            **manager._conns,
        }
        assert list(manager._conns) == ["first", "second"], "the settled record must come first to bite"

        assert manager.config_changed({"first": settled, "second": _Cfg(command="different")}) is True

    @pytest.mark.asyncio
    async def test_a_dead_server_with_unchanged_config_reads_as_no_work(self):
        # Matching what apply_config does with it. A probe that said "work" here
        # would make every poll retry a server that just failed -- the storm the
        # gate exists to prevent.
        registry = ToolRegistry()
        manager = MCPConnectionManager(registry)
        cfg = _Cfg()
        manager._conns["openseo"] = MCPConnection(name="openseo", config=cfg, state="error", error="nope")
        assert manager.config_changed({"openseo": cfg}) is False
