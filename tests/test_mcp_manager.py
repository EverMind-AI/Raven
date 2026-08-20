"""Unit tests for MCPConnectionManager (per-server hot connect/disconnect)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import MCPServerConfig
from raven.mcp.manager import MCPConnectionManager
from raven.mcp.naming import MCPToolRef
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
            # Registered the way a real connect does -- with its origin. The
            # registry's origin index is what the manager reads its own teardown
            # list back out of, so a stub without one is invisible to it.
            registry.register(FakeTool(full), origin=MCPToolRef(name=full, server=name, tool=t))
            registered.append(full)
        return _connected(registered)

    return fake


_PATCH = "raven.mcp.manager.connect_mcp_server"


class _Caps:
    """Minimal stand-in for the SDK's ``ServerCapabilities``.

    A field left None means "this server does not offer that primitive", which
    is what ``servers_offering`` reads. Defaults to tools-only, matching the
    majority of real servers.
    """

    def __init__(self, *, resources=None, prompts=None, tools=object()) -> None:
        self.resources = resources
        self.prompts = prompts
        self.tools = tools


def _connected(names, *, session=None, capabilities=None):
    """The shape ``connect_mcp_server`` returns, for a patched stub to hand back."""
    from raven.mcp.client import Connected

    return Connected(
        names=list(names),
        session=session if session is not None else object(),
        capabilities=capabilities if capabilities is not None else _Caps(),
    )


async def test_apply_config_connects_and_reports():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    with patch(_PATCH, new=_fake_connect(["a", "b"])):
        result = await mgr.apply_config({"srv": _cfg()})
    assert result.as_dict() == {"reloaded": 1, "tools_changed": True}
    assert reg.has("mcp_srv_a") and reg.has("mcp_srv_b")
    (snap,) = mgr.status()
    assert snap["state"] == "connected"
    assert snap["tool_count"] == 2
    assert mgr.tool_map()["mcp_srv_a"] == "srv"


async def test_apply_config_unchanged_config_is_noop():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    cfg = _cfg()
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.apply_config({"srv": cfg})
        result = await mgr.apply_config({"srv": cfg.model_copy()})
    assert result.as_dict() == {"reloaded": 0, "tools_changed": False}


async def test_apply_config_removed_server_withdraws_tools():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.apply_config({"srv": _cfg()})
        result = await mgr.apply_config({})
    assert result.as_dict() == {"reloaded": 1, "tools_changed": True}
    assert not reg.has("mcp_srv_a")
    assert mgr.status() == []


async def test_apply_config_disabled_server_keeps_record():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.apply_config({"srv": _cfg()})
        await mgr.apply_config({"srv": _cfg(enabled=False)})
    assert not reg.has("mcp_srv_a")
    (snap,) = mgr.status()
    assert snap["state"] == "disconnected"
    assert snap["enabled"] is False
    # Re-enabling reconnects.
    with patch(_PATCH, new=_fake_connect(["a"])):
        result = await mgr.apply_config({"srv": _cfg()})
    assert result.tools_changed is True
    assert reg.has("mcp_srv_a")


async def test_apply_config_changed_config_reconnects():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.apply_config({"srv": _cfg()})
    with patch(_PATCH, new=_fake_connect(["a", "b"])):
        result = await mgr.apply_config({"srv": _cfg(url="https://other.test/mcp")})
    assert result.reloaded == 1
    assert reg.has("mcp_srv_b")


async def test_connect_failure_lands_in_error_state_and_is_not_retried_by_apply_config():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    calls = []

    async def boom(name, cfg, registry, stack, executor=None, http_auth=None):
        calls.append(name)
        raise RuntimeError("nope")

    cfg = _cfg()
    with patch(_PATCH, new=boom):
        result = await mgr.apply_config({"srv": cfg})
        (snap,) = mgr.status()
        assert snap["state"] == "error"
        assert "nope" in snap["error"]
        assert result.tools_changed is False
        # The 5s poll with unchanged config must not retry a dead server.
        await mgr.apply_config({"srv": cfg.model_copy()})
    assert calls == ["srv"]


async def test_connect_retries_error_state():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)

    async def boom(name, cfg, registry, stack, executor=None, http_auth=None):
        raise RuntimeError("nope")

    cfg = _cfg()
    with patch(_PATCH, new=boom):
        await mgr.apply_config({"srv": cfg})
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
            await mgr.apply_config({"srv": _cfg(command="mcp-server", url="")})


async def test_disconnect_drop_forgets_record():
    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.apply_config({"srv": _cfg()})
    await mgr.disconnect("srv", drop=True)
    assert mgr.status() == []
    assert not reg.has("mcp_srv_a")


async def test_state_change_callback_fires():
    reg = ToolRegistry()
    seen: list[tuple[str, str]] = []
    mgr = MCPConnectionManager(reg, on_state_change=lambda s: seen.append((s["name"], s["state"])))
    with patch(_PATCH, new=_fake_connect(["a"])):
        await mgr.apply_config({"srv": _cfg()})
    await mgr.aclose()
    assert ("srv", "connecting") in seen
    assert ("srv", "connected") in seen


async def test_post_connect_blacklist_shrinks_tool_names():
    reg = ToolRegistry()

    def blacklist():
        reg.unregister("mcp_srv_b")

    mgr = MCPConnectionManager(reg, post_connect=blacklist)
    with patch(_PATCH, new=_fake_connect(["a", "b"])):
        await mgr.apply_config({"srv": _cfg()})
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
        registry.register(FakeTool(f"mcp_{name}_a"), origin=MCPToolRef(name=f"mcp_{name}_a", server=name, tool="a"))
        return _connected([f"mcp_{name}_a"])

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
        registry.register(FakeTool(f"mcp_{name}_a"), origin=MCPToolRef(name=f"mcp_{name}_a", server=name, tool="a"))
        return _connected([f"mcp_{name}_a"])

    with patch(_PATCH, new=racing):
        loser = _asyncio.create_task(mgr.connect("srv", _cfg()))
        await _asyncio.sleep(0)
        # A config change tears down the in-flight attempt and starts a fresh one.
        await mgr.apply_config({"srv": _cfg(url="https://changed.test/mcp")})
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

    import raven.mcp.manager as mod

    reg = ToolRegistry()
    mgr = MCPConnectionManager(reg)

    async def never(name, cfg, registry, stack, executor=None, http_auth=None):
        await _asyncio.Event().wait()

    with (
        patch(_PATCH, new=never),
        patch.object(mod, "_HANDSHAKE_TIMEOUT", 0.05),
        patch.object(mod, "_AUTH_PARK_MAX", 0.15),
        patch("raven.mcp.oauth.auth_wait_servers", lambda: {"srv"}),
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
        registry.register(FakeTool(f"mcp_{name}_a"), origin=MCPToolRef(name=f"mcp_{name}_a", server=name, tool="a"))
        return _connected([f"mcp_{name}_a"])

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
        result = await mgr.apply_config({"srv": _cfg()})
    assert result.reloaded == 1
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
        registry.register(FakeTool(f"mcp_{name}_a"), origin=MCPToolRef(name=f"mcp_{name}_a", server=name, tool="a"))
        started.set()
        await _asyncio.sleep(30)
        return _connected([f"mcp_{name}_a"])

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
        registry.register(
            FakeTool(f"mcp_{name}_late"), origin=MCPToolRef(name=f"mcp_{name}_late", server=name, tool="late")
        )
        return _connected([f"mcp_{name}_late"])

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
    from raven.mcp import manager as mcp_manager
    from raven.mcp.oauth import OAUTH_FLOW_TIMEOUT

    assert mcp_manager._AUTH_PARK_MAX > OAUTH_FLOW_TIMEOUT


# ── Auth park: a state, not a wait ─────────────────────────────────


def _capture_oauth_notify(monkeypatch):
    """Stub provider_for so the test can fire oauth events like the SDK would."""
    from raven.mcp import oauth as mcp_oauth

    captured: dict[str, Any] = {}

    async def fake_provider_for(server, cfg, notify=None, interactive=False):
        captured["notify"] = notify
        captured["interactive"] = interactive
        return None

    monkeypatch.setattr(mcp_oauth, "provider_for", fake_provider_for)
    return captured


@pytest.mark.asyncio
async def test_a_background_connect_that_parks_becomes_auth_required_not_a_wait(monkeypatch):
    """sync() must return while the user has not clicked, with the state saying
    who the wait is on -- and the attempt must stay alive so that click still
    lands: releasing it afterwards commits and the tools appear."""
    import asyncio

    captured = _capture_oauth_notify(monkeypatch)
    released = asyncio.Event()

    async def parks(name, cfg, registry, stack, executor=None, http_auth=None):
        captured["notify"]("oauth.pending", {"server": name, "url": "https://idp.example/a"})
        await released.wait()
        registry.register(
            FakeTool(f"mcp_{name}_late"), origin=MCPToolRef(name=f"mcp_{name}_late", server=name, tool="late")
        )
        return _connected([f"mcp_{name}_late"])

    registry = ToolRegistry()
    mgr = MCPConnectionManager(registry)
    with patch(_PATCH, new=parks):
        result = await asyncio.wait_for(mgr.apply_config({"svc": _cfg(auth="oauth")}), timeout=5)

        assert result.as_dict() == {"reloaded": 1, "tools_changed": False}
        snap = mgr.status()[0]
        assert snap["state"] == "auth_required"
        assert snap["error"] == "waiting for browser authorization"
        assert not registry.has("mcp_svc_late")

        released.set()
        for _ in range(200):
            if mgr.status()[0]["state"] == "connected":
                break
            await asyncio.sleep(0.01)

    assert mgr.status()[0]["state"] == "connected"
    assert registry.has("mcp_svc_late")


@pytest.mark.asyncio
async def test_a_disconnect_while_parked_still_wins_over_the_late_commit(monkeypatch):
    import asyncio

    captured = _capture_oauth_notify(monkeypatch)
    released = asyncio.Event()

    async def parks(name, cfg, registry, stack, executor=None, http_auth=None):
        captured["notify"]("oauth.pending", {"server": name, "url": "https://idp.example/a"})
        await released.wait()
        registry.register(
            FakeTool(f"mcp_{name}_late"), origin=MCPToolRef(name=f"mcp_{name}_late", server=name, tool="late")
        )
        return _connected([f"mcp_{name}_late"])

    registry = ToolRegistry()
    mgr = MCPConnectionManager(registry)
    with patch(_PATCH, new=parks):
        await asyncio.wait_for(mgr.apply_config({"svc": _cfg(auth="oauth")}), timeout=5)
        await mgr.disconnect("svc", drop=True)
        released.set()
        await asyncio.sleep(0.05)

    assert mgr.status() == []
    assert not registry.has("mcp_svc_late")


@pytest.mark.asyncio
async def test_an_interactive_connect_keeps_its_state_while_the_user_authorizes(monkeypatch):
    """plug.auth watches its own flow; flipping it to auth_required mid-click
    would tell the page the explicit retry it just asked for already failed."""
    import asyncio

    captured = _capture_oauth_notify(monkeypatch)
    released = asyncio.Event()
    states: list[str] = []

    async def parks(name, cfg, registry, stack, executor=None, http_auth=None):
        captured["notify"]("oauth.pending", {"server": name, "url": "https://idp.example/a"})
        states.append(mgr.status()[0]["state"])
        released.set()
        return _connected([])

    registry = ToolRegistry()
    mgr = MCPConnectionManager(registry)
    with patch(_PATCH, new=parks):
        await asyncio.wait_for(mgr.connect("svc", _cfg(auth="oauth")), timeout=5)

    assert states == ["connecting"]
    assert captured["interactive"] is True


@pytest.mark.asyncio
async def test_apply_config_connects_servers_concurrently():
    """Two handshakes that each refuse to finish until the other has started:
    the serial sync deadlocks here, the concurrent one does not."""
    import asyncio

    both_started = asyncio.Event()
    started: list[str] = []

    async def meet(name, cfg, registry, stack, executor=None, http_auth=None):
        started.append(name)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=2)
        return _connected([])

    mgr = MCPConnectionManager(ToolRegistry())
    with patch(_PATCH, new=meet):
        await asyncio.wait_for(mgr.apply_config({"a": _cfg(), "b": _cfg()}), timeout=5)

    assert {s["state"] for s in mgr.status()} == {"connected"}


@pytest.mark.asyncio
async def test_a_concurrent_apply_starts_the_executor_once():
    import asyncio

    calls: list[int] = []

    async def provider():
        calls.append(1)
        await asyncio.sleep(0.01)
        return object()

    mgr = MCPConnectionManager(ToolRegistry())
    with patch(_PATCH, new=_fake_connect(["t"])):
        await mgr.apply_config({"a": _cfg(), "b": _cfg()}, executor_provider=provider)

    assert calls == [1]


@pytest.mark.asyncio
async def test_beginning_or_dropping_an_attempt_invalidates_its_pending_link(monkeypatch):
    """The epoch dooms a superseded attempt's commit; its authorization link
    must be invalidated at the same edges (new attempt, detach)."""
    from raven.mcp import oauth as mcp_oauth

    cancelled: list[str] = []
    monkeypatch.setattr(mcp_oauth, "cancel_pending", lambda name: cancelled.append(name))

    mgr = MCPConnectionManager(ToolRegistry())
    with patch(_PATCH, new=_fake_connect([])):
        await mgr.connect("svc", _cfg())
        await mgr.disconnect("svc")

    assert cancelled == ["svc", "svc"]


@pytest.mark.asyncio
async def test_a_disable_racing_a_handshake_does_not_orphan_its_registrations():
    """A record whose epoch is None wants no attempt -- not a newer one.

    `_disconnect_locked` clears the epoch as its first act, so an attempt still
    in flight (parked at browser authorization, or a cold stdio server) reaches
    the commit, fails the epoch check, and asks `_take_back` to clean up. Reading
    `epoch is None` as "somebody else owns these names" left them registered
    while the attempt's own stack was closed underneath them, which is the exact
    failure the abort path exists to prevent: the model is offered a tool whose
    session is dead, and `config_changed` reads the orphan through `names_from`
    and answers "still work to do" on every poll, forever.

    Only `drop=False` reaches it. With `drop=True` the record is gone, so the
    guard never fires.
    """
    import asyncio

    registry = ToolRegistry()
    mgr = MCPConnectionManager(registry)
    gate = asyncio.Event()

    async def parked(name, cfg, registry_, stack, executor=None, http_auth=None):
        await gate.wait()
        full = f"mcp_{name}_x"
        registry_.register(FakeTool(full), origin=MCPToolRef(name=full, server=name, tool="x"))
        return _connected([full])

    with patch(_PATCH, new=parked):
        applying = asyncio.create_task(mgr.apply_config({"srv": _cfg()}))
        await asyncio.sleep(0)
        while mgr._conns.get("srv") is None or mgr._conns["srv"].state != "connecting":
            await asyncio.sleep(0)

        # The user disables the server while the handshake is still parked.
        await mgr._disconnect_locked("srv", drop=False)
        assert mgr._conns["srv"].epoch is None

        gate.set()
        await applying

    assert registry.tool_names == [], "the attempt's registrations must come back out"
    assert registry.names_from("srv") == []
    assert [(s["state"], s["tool_count"]) for s in mgr.status()] == [("disconnected", 0)]
    # And the probe settles, instead of reporting work on every tick.
    assert mgr.config_changed({"srv": _cfg(enabled=False)}) is False
