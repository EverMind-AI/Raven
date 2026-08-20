"""Addressing a server by name: ``session_of`` and the capability gate.

The seam the resource and prompt meta-tools stand on. They take ``server`` as an
argument, so unlike a tool wrapper they have no session of their own to use --
they hold a name and nothing else.

What makes that safe is where the session is written, not a check at read time:
only a committing attempt stores one, under the same epoch check that accepts
its tool registrations, and the teardown clears it in the same breath as closing
the stack. These pin both halves, because getting either wrong hands a caller a
session whose transport is gone.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import MCPServerConfig
from raven.mcp.client import Connected
from raven.mcp.manager import MCPConnectionManager
from raven.mcp.naming import MCPToolRef

_PATCH = "raven.mcp.manager.connect_mcp_server"


class _Caps:
    """Stand-in for ``ServerCapabilities``. None means "not offered"."""

    def __init__(self, *, resources=None, prompts=None, tools=object()) -> None:
        self.resources = resources
        self.prompts = prompts
        self.tools = tools


class _FakeTool(Tool):
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


def _cfg(url: str = "https://svc.test/mcp", **kw) -> MCPServerConfig:
    return MCPServerConfig(url=url, **kw)


def _connect(*, session, capabilities=None, tools=("a",)):
    """A stub handshake that registers ``tools`` and hands back ``session``."""

    async def fake(name, cfg, registry, stack, executor=None, http_auth=None):
        names = []
        for t in tools:
            full = f"mcp_{name}_{t}"
            registry.register(_FakeTool(full), origin=MCPToolRef(name=full, server=name, tool=t))
            names.append(full)
        return Connected(names=names, session=session, capabilities=capabilities or _Caps())

    return fake


class TestSessionIsAddressableByName:
    async def test_a_connected_server_hands_out_its_session(self):
        marker = object()
        mgr = MCPConnectionManager(ToolRegistry())
        with patch(_PATCH, new=_connect(session=marker)):
            await mgr.apply_config({"svc": _cfg()})
        assert mgr.session_of("svc") is marker

    async def test_an_unknown_server_has_none(self):
        mgr = MCPConnectionManager(ToolRegistry())
        assert mgr.session_of("nobody") is None

    async def test_a_detached_server_has_none(self):
        # Cleared with the stack that owns it: the session is dead the moment the
        # stack closes, so keeping the reference would hand out a corpse.
        mgr = MCPConnectionManager(ToolRegistry())
        with patch(_PATCH, new=_connect(session=object())):
            await mgr.apply_config({"svc": _cfg()})
        await mgr.disconnect("svc", drop=False)
        assert mgr.session_of("svc") is None

    async def test_a_server_removed_from_config_has_none(self):
        mgr = MCPConnectionManager(ToolRegistry())
        with patch(_PATCH, new=_connect(session=object())):
            await mgr.apply_config({"svc": _cfg()})
            await mgr.apply_config({})
        assert mgr.session_of("svc") is None

    async def test_a_failed_connect_publishes_no_session(self):
        async def boom(name, cfg, registry, stack, executor=None, http_auth=None):
            raise RuntimeError("nope")

        mgr = MCPConnectionManager(ToolRegistry())
        with patch(_PATCH, new=boom):
            await mgr.apply_config({"svc": _cfg()})
        assert mgr.status()[0]["state"] == "error"
        assert mgr.session_of("svc") is None

    async def test_a_losing_attempt_never_publishes_its_session(self):
        """The reason ``session_of`` needs no generation of its own.

        Two attempts on one server produce two sessions. The loser's transport
        is closed on rollback, so if it could publish, a caller addressing the
        server by name would reach a session whose stack is gone -- and unlike a
        tool wrapper there is no registry entry to be taken back.
        """
        winner, loser = object(), object()
        gate = asyncio.Event()
        first = True

        async def racing(name, cfg, registry, stack, executor=None, http_auth=None):
            nonlocal first
            mine_is_first = first
            first = False
            if mine_is_first:
                await gate.wait()  # the loser finishes last
            full = f"mcp_{name}_a"
            registry.register(_FakeTool(full), origin=MCPToolRef(name=full, server=name, tool="a"))
            return Connected(names=[full], session=loser if mine_is_first else winner, capabilities=_Caps())

        mgr = MCPConnectionManager(ToolRegistry())
        with patch(_PATCH, new=racing):
            slow = asyncio.create_task(mgr.connect("svc", _cfg()))
            await asyncio.sleep(0)
            await mgr.apply_config({"svc": _cfg(url="https://changed.test/mcp")})
            gate.set()
            await slow

        assert mgr.session_of("svc") is winner, "the loser overwrote the committed session"


class TestTheCapabilityGate:
    async def test_a_tools_only_server_offers_neither(self):
        mgr = MCPConnectionManager(ToolRegistry())
        with patch(_PATCH, new=_connect(session=object(), capabilities=_Caps())):
            await mgr.apply_config({"svc": _cfg()})
        assert mgr.servers_offering("resources") == []
        assert mgr.servers_offering("prompts") == []

    async def test_the_two_primitives_are_gated_apart(self):
        # ServerCapabilities carries them as separate fields, so a server that
        # serves resources must not make the prompt tools appear.
        mgr = MCPConnectionManager(ToolRegistry())
        with patch(_PATCH, new=_connect(session=object(), capabilities=_Caps(resources=object()))):
            await mgr.apply_config({"svc": _cfg()})
        assert mgr.servers_offering("resources") == ["svc"]
        assert mgr.servers_offering("prompts") == []

    async def test_only_connected_servers_count(self):
        mgr = MCPConnectionManager(ToolRegistry())
        with patch(_PATCH, new=_connect(session=object(), capabilities=_Caps(resources=object()))):
            await mgr.apply_config({"svc": _cfg()})
        await mgr.disconnect("svc", drop=False)
        assert mgr.servers_offering("resources") == []

    async def test_a_server_that_never_connected_counts_for_nothing(self):
        async def boom(name, cfg, registry, stack, executor=None, http_auth=None):
            raise RuntimeError("nope")

        mgr = MCPConnectionManager(ToolRegistry())
        with patch(_PATCH, new=boom):
            await mgr.apply_config({"svc": _cfg()})
        assert mgr.servers_offering("resources") == []

    async def test_the_answer_is_sorted(self):
        # Stable order, so a gated registration does not reshuffle the tool array
        # between applies for no reason.
        mgr = MCPConnectionManager(ToolRegistry())
        with patch(_PATCH, new=_connect(session=object(), capabilities=_Caps(resources=object()))):
            await mgr.apply_config({"zeta": _cfg(), "alpha": _cfg(url="https://a.test/mcp")})
        assert mgr.servers_offering("resources") == ["alpha", "zeta"]
