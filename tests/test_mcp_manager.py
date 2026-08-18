"""Unit tests for MCPConnectionManager (per-server hot connect/disconnect)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from raven.agent.tools.base import Tool
from raven.agent.tools.mcp_manager import MCPConnectionManager
from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import MCPServerConfig
from raven.sandbox import SandboxInitError


class FakeTool(Tool):
    def __init__(self, name: str):
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


def _cfg(url: str = "https://example.test/mcp", **kw) -> MCPServerConfig:
    return MCPServerConfig(url=url, **kw)


def _fake_connect(tool_names: list[str]):
    """Patch target for connect_mcp_server that registers fake tools."""

    async def fake(name, cfg, registry, stack, executor=None, http_auth=None):
        registered = []
        for t in tool_names:
            full = f"mcp_{name}_{t}"
            registry.register(FakeTool(full))
            registered.append(full)
        return registered

    return fake


_PATCH = "raven.agent.tools.mcp_manager.connect_mcp_server"


async def test_sync_connects_and_reports():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    with patch(_PATCH, new=_fake_connect(["a", "b"])):
        result = await mgr.sync({"srv": _cfg()})
    assert result == {"reloaded": 1, "tools_changed": True}
    assert reg.has("mcp_srv_a") and reg.has("mcp_srv_b")
    (snap,) = mgr.status()
    assert snap["state"] == "connected"
    assert snap["tool_count"] == 2
    assert mgr.tool_map()["mcp_srv_a"] == "srv"


async def test_sync_unchanged_config_is_noop():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    cfg = _cfg()
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.sync({"srv": cfg})
        result = await mgr.sync({"srv": cfg.model_copy()})
    assert result == {"reloaded": 0, "tools_changed": False}


async def test_sync_removed_server_unregisters_tools():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.sync({"srv": _cfg()})
        result = await mgr.sync({})
    assert result == {"reloaded": 1, "tools_changed": True}
    assert not reg.has("mcp_srv_a")
    assert mgr.status() == []


async def test_sync_disabled_server_keeps_record():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.sync({"srv": _cfg()})
        await mgr.sync({"srv": _cfg(enabled=False)})
    assert not reg.has("mcp_srv_a")
    (snap,) = mgr.status()
    assert snap["state"] == "disconnected"
    assert snap["enabled"] is False
    # Re-enabling reconnects.
    with patch(_PATCH, new=_fake_connect(["a"])):
        result = await mgr.sync({"srv": _cfg()})
    assert result["tools_changed"] is True
    assert reg.has("mcp_srv_a")


async def test_sync_changed_config_reconnects():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.sync({"srv": _cfg()})
    with patch(_PATCH, new=_fake_connect(["a", "b"])):
        result = await mgr.sync({"srv": _cfg(url="https://other.test/mcp")})
    assert result["reloaded"] == 1
    assert reg.has("mcp_srv_b")


async def test_connect_failure_lands_in_error_state_and_is_not_retried_by_sync():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    calls = []

    async def boom(name, cfg, registry, stack, executor=None, http_auth=None):
        calls.append(name)
        raise RuntimeError("nope")

    cfg = _cfg()
    with patch(_PATCH, new=boom):
        result = await mgr.sync({"srv": cfg})
        (snap,) = mgr.status()
        assert snap["state"] == "error"
        assert "nope" in snap["error"]
        assert result["tools_changed"] is False
        # The 5s poll with unchanged config must not retry a dead server.
        await mgr.sync({"srv": cfg.model_copy()})
    assert calls == ["srv"]


async def test_connect_retries_error_state():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)

    async def boom(name, cfg, registry, stack, executor=None, http_auth=None):
        raise RuntimeError("nope")

    cfg = _cfg()
    with patch(_PATCH, new=boom):
        await mgr.sync({"srv": cfg})
    with patch(_PATCH, new=_fake_connect(["a"])):
        snap = await mgr.connect("srv", cfg)
    assert snap["state"] == "connected"
    assert reg.has("mcp_srv_a")


async def test_sandbox_init_error_propagates():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)

    async def guard(name, cfg, registry, stack, executor=None, http_auth=None):
        raise SandboxInitError("no spawning")

    with patch(_PATCH, new=guard):
        with pytest.raises(SandboxInitError):
            await mgr.sync({"srv": _cfg(command="mcp-server", url="")})


async def test_disconnect_drop_forgets_record():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.sync({"srv": _cfg()})
    await mgr.disconnect("srv", drop=True)
    assert mgr.status() == []
    assert not reg.has("mcp_srv_a")


async def test_state_change_callback_fires():
    reg = ToolRegistry()
    seen: list[tuple[str, str]] = []
    mgr = MCPConnectionManager(reg, on_state_change=lambda s: seen.append((s["name"], s["state"])))
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.sync({"srv": _cfg()})
    await mgr.aclose()
    assert ("srv", "connecting") in seen
    assert ("srv", "connected") in seen


async def test_post_connect_blacklist_shrinks_tool_names():
    reg = ToolRegistry()

    def blacklist():
        reg.unregister("mcp_srv_b")

    mgr = MCPConnectionManager(reg, post_connect=blacklist)
    with patch(_PATCH, new=_fake_connect(["a", "b"])):
        await mgr.sync({"srv": _cfg()})
    (snap,) = mgr.status()
    assert snap["tool_count"] == 1
    assert mgr.tool_map() == {"mcp_srv_a": "srv"}


async def test_a_second_connect_does_not_start_a_second_handshake():
    """A retry button pressed twice must cost one transport, not two.

    Each in-flight handshake holds a subprocess (or an HTTP session) for up to the
    handshake bound, and both would race to commit into the same registry slot.
    """
    import asyncio as _asyncio

    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    release = _asyncio.Event()
    starts: list[str] = []

    async def slow(name, cfg, registry, stack, executor=None, http_auth=None):
        starts.append(name)
        await release.wait()
        registry.register(FakeTool(f"mcp_{name}_a"))
        return [f"mcp_{name}_a"]

    with patch(_PATCH, new=slow):
        first = _asyncio.create_task(mgr.connect("srv", _cfg()))
        await _asyncio.sleep(0)  # let the first attempt reach the handshake
        second = await mgr.connect("srv", _cfg())
        assert second["state"] == "connecting"
        release.set()
        await first

    assert starts == ["srv"], "the second connect started another handshake"


async def test_a_stale_attempt_does_not_strip_the_winners_tools():
    """Two attempts on one server register the same tool names.

    The registry keys by name, so a loser that unregisters "its" names after the
    winner committed would leave the winner reporting a tool the registry can no
    longer dispatch.
    """
    import asyncio as _asyncio

    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    gate = _asyncio.Event()
    first = True

    async def racing(name, cfg, registry, stack, executor=None, http_auth=None):
        nonlocal first
        mine = first
        first = False
        if mine:
            await gate.wait()  # the losing attempt finishes last
        registry.register(FakeTool(f"mcp_{name}_a"))
        return [f"mcp_{name}_a"]

    with patch(_PATCH, new=racing):
        loser = _asyncio.create_task(mgr.connect("srv", _cfg()))
        await _asyncio.sleep(0)
        # A config change tears down the in-flight attempt and starts a fresh one.
        await mgr.sync({"srv": _cfg(url="https://changed.test/mcp")})
        gate.set()
        await loser

    assert reg.has("mcp_srv_a"), "the stale attempt unregistered the live tool"
    (snap,) = mgr.status()
    assert snap["state"] == "connected"
    assert snap["tool_count"] == 1


async def test_a_server_parked_at_the_browser_is_exempt_only_for_a_while():
    """The OAuth exemption has to be bounded.

    A flow whose redirect registered but whose callback never arrives leaks its
    pending entry, and an unbounded exemption then parks the server in
    `connecting` for the life of the process -- the exact regression the
    handshake watchdog exists to prevent.
    """
    import asyncio as _asyncio

    import raven.agent.tools.mcp_manager as mod

    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)

    async def never(name, cfg, registry, stack, executor=None, http_auth=None):
        await _asyncio.Event().wait()

    with (
        patch(_PATCH, new=never),
        patch.object(mod, "_HANDSHAKE_TIMEOUT", 0.05),
        patch.object(mod, "_AUTH_PARK_MAX", 0.15),
        patch("raven.agent.tools.mcp_oauth.auth_wait_servers", lambda: {"srv"}),
    ):
        snap = await _asyncio.wait_for(mgr.connect("srv", _cfg()), timeout=5)

    assert snap["state"] == "error"
    assert "no progress" in (snap["error"] or "")


async def test_a_cancelled_connect_leaves_the_record_retryable():
    """A cancelled turn must not park the server mid-connect.

    `sync` skips any record that is not `disconnected`, so a connect abandoned in
    `connecting` is never retried again for the life of the process -- and the
    handshake, being shielded, would keep running and register its tools into a
    registry no connection owns.
    """
    import asyncio as _asyncio

    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    started = _asyncio.Event()

    async def slow(name, cfg, registry, stack, executor=None, http_auth=None):
        started.set()
        await _asyncio.sleep(30)
        registry.register(FakeTool(f"mcp_{name}_a"))
        return [f"mcp_{name}_a"]

    with patch(_PATCH, new=slow):
        task = _asyncio.create_task(mgr.connect("srv", _cfg()))
        await started.wait()
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task
        # Whatever the handshake was doing is over; nothing appears afterwards.
        await _asyncio.sleep(0.1)

    (snap,) = mgr.status()
    assert snap["state"] == "disconnected", "a cancelled connect must be retryable"
    assert not reg.has("mcp_srv_a")

    # And a later sync does retry it, which the parked state prevented.
    with patch(_PATCH, new=_fake_connect(["a"])):
        result = await mgr.sync({"srv": _cfg()})
    assert result["reloaded"] == 1
    assert reg.has("mcp_srv_a")


async def test_a_cancelled_connect_takes_back_what_it_registered():
    """The handshake can win the race and register before the cancel lands.

    Those names belong to a session inside the stack being closed, so leaving
    them registered hands the agent a tool it cannot call and that no disconnect
    can reach (the connection never recorded owning them).
    """
    import asyncio as _asyncio

    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    started = _asyncio.Event()

    async def register_then_hang(name, cfg, registry, stack, executor=None, http_auth=None):
        registry.register(FakeTool(f"mcp_{name}_a"))
        started.set()
        await _asyncio.sleep(30)
        return [f"mcp_{name}_a"]

    with patch(_PATCH, new=register_then_hang):
        task = _asyncio.create_task(mgr.connect("srv", _cfg()))
        await started.wait()
        assert reg.has("mcp_srv_a")
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task

    assert not reg.has("mcp_srv_a"), "the cancelled attempt's registration leaked"
    (snap,) = mgr.status()
    assert snap["state"] == "disconnected"
    assert snap["tool_count"] == 0


async def test_cancelling_a_connect_stops_the_handshake_too():
    """The handshake is shielded, so cancelling the connect does not stop it by
    itself -- and a handshake that outlives its caller registers its tools into
    the live registry moments after the stack they belong to was closed."""
    import asyncio as _asyncio

    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    started = _asyncio.Event()

    async def registers_shortly(name, cfg, registry, stack, executor=None, http_auth=None):
        started.set()
        await _asyncio.sleep(0.05)
        registry.register(FakeTool(f"mcp_{name}_late"))
        return [f"mcp_{name}_late"]

    with patch(_PATCH, new=registers_shortly):
        task = _asyncio.create_task(mgr.connect("srv", _cfg()))
        await started.wait()
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task
        await _asyncio.sleep(0.3)  # long enough for an uncancelled handshake to land

    assert not reg.has("mcp_srv_late"), "the shielded handshake outlived its cancelled connect"


def test_the_auth_park_bound_outlasts_the_oauth_flow_it_waits_on() -> None:
    """The watchdog's exemption must never expire before the flow it exists to
    wait for. These lived in two modules as unrelated literals, and raising the
    flow timeout to 900 left the exemption at 420 -- so the watchdog cancelled
    every authorization at seven minutes while the page counted down fifteen.
    """
    from raven.agent.tools import mcp_manager
    from raven.agent.tools.mcp_oauth import OAUTH_FLOW_TIMEOUT

    assert mcp_manager._AUTH_PARK_MAX > OAUTH_FLOW_TIMEOUT
