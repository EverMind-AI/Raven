"""Unit tests for MCPConnectionManager (per-server hot connect/disconnect)."""

from __future__ import annotations

from contextlib import suppress
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
        patch("raven.mcp.oauth.auth_wait_servers", lambda **_: {"srv"}),
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

    async def fake_provider_for(server, cfg, notify=None, interactive=False, can_park=True):
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
    must be invalidated at the same edges (new attempt, detach).

    The two edges tell the parked waiter different things, and the difference is
    the point: a detach is the last act of a short-lived host that never opened a
    browser, so reporting it as a newer attempt sends the reader looking for a
    second flow that never existed.
    """
    from raven.mcp import oauth as mcp_oauth

    cancelled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mcp_oauth,
        "cancel_pending",
        lambda name, *, reason="superseded by a newer authorization attempt": cancelled.append((name, reason)),
    )

    mgr = MCPConnectionManager(ToolRegistry())
    with patch(_PATCH, new=_fake_connect([])):
        await mgr.connect("svc", _cfg())
        await mgr.disconnect("svc")

    assert [name for name, _ in cancelled] == ["svc", "svc"]
    begin_reason, detach_reason = (reason for _, reason in cancelled)
    assert "superseded" in begin_reason
    assert "detached" in detach_reason and "superseded" not in detach_reason


@pytest.mark.asyncio
async def test_aclose_reaps_a_handshake_still_parked_on_authorization():
    """A task still pending when the loop closes has its exception printed by
    ``asyncio.run`` itself -- whoever did or did not retrieve it.

    Every wait on a handshake is shielded so a timeout cannot kill an OAuth park,
    which is exactly what lets one outlive its waiter. A short-lived host then
    ends, the loop shuts down, and a run that degraded correctly signs off with a
    wall of stack. Retrieving the exception is not enough on its own; the task
    has to be finished before the loop is.
    """
    import asyncio

    started = asyncio.Event()

    async def parks_forever(*_a, **_k):
        started.set()
        await asyncio.Event().wait()  # the browser callback that never comes

    mgr = MCPConnectionManager(ToolRegistry())
    with patch(_PATCH, new=parks_forever):
        task = asyncio.ensure_future(mgr.connect("svc", _cfg()))
        await asyncio.wait_for(started.wait(), timeout=5)

        await mgr.aclose()

        assert not [t for t in mgr._handshakes if not t.done()], (
            "a handshake left pending here is one asyncio reports at shutdown"
        )
    task.cancel()
    with suppress(BaseException):
        await task


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


async def test_reset_for_retry_only_touches_the_attempts_its_own_apply_began():
    """Scoped by the epochs apply recorded, never a scan of every record.

    The caller is whoever cancelled a sync, and the point is to undo what that
    cancel did: a record left in ``error`` or ``connecting`` with no attempt
    behind it is one no later apply retries and no reload touches, so the server
    would be gone for the life of the process.

    A record whose epoch has moved on is a different matter -- a ``plug.auth`` or
    a config apply started meanwhile owns it, and its commit checks the very
    state a reset would rewrite. Resetting that is worse than the bug: the
    transport is discarded on commit while ``apply_mcp_config`` still reports MCP
    as connected, and the server is lost with nothing logged.
    """
    registry = ToolRegistry()
    mgr = MCPConnectionManager(registry)
    attempts: dict[str, object] = {}

    async def refused(name, cfg, registry, stack, executor=None, http_auth=None):
        raise RuntimeError("nope")

    with patch(_PATCH, new=refused):
        # A per-server connect failure is recorded, not raised: only a
        # SandboxInitError comes back out of apply_config.
        await mgr.apply_config({"mine": _cfg(), "theirs": _cfg()}, attempts=attempts)

    assert {s["name"]: s["state"] for s in mgr.status()} == {"mine": "error", "theirs": "error"}
    assert set(attempts) == {"mine", "theirs"}

    # A newer attempt took `theirs` over. Only the epoch says so.
    mgr._conns["theirs"].epoch = object()

    reset = await mgr.reset_for_retry(attempts)

    assert reset == ["mine"]
    assert mgr._conns["mine"].state == "disconnected"
    assert mgr._conns["theirs"].state == "error", "a record a newer attempt owns must be left alone"


async def test_reset_for_retry_leaves_a_live_park_and_a_live_session_alone():
    registry = ToolRegistry()
    mgr = MCPConnectionManager(registry)

    with patch(_PATCH, new=_fake_connect(["search"])):
        await mgr.apply_config({"up": _cfg()})
    assert mgr._conns["up"].state == "connected"

    # A park: its attempt is still running and commits when the user clicks.
    mgr._conns["up"].state = "auth_required"
    assert await mgr.reset_for_retry({"up": mgr._conns["up"].epoch}) == []
    assert mgr._conns["up"].state == "auth_required"

    mgr._conns["up"].state = "connected"
    assert await mgr.reset_for_retry({"up": mgr._conns["up"].epoch}) == []
    assert mgr._conns["up"].state == "connected"


async def test_a_reaped_apply_does_not_reset_the_apply_that_replaced_it():
    """The token is the caller's, and this is why it cannot live on the manager.

    A manager-wide record of "the most recent apply" is overwritten by whichever
    apply began last. A prewarm cancelled after a config apply started would then
    read the newer apply's name/epoch set and disconnect ITS attempt: the commit
    checks the state that reset rewrote, so the transport is discarded, while
    ``apply_mcp_config`` still sets ``_mcp_connected`` -- MCP reads as connected
    with the server disconnected and nothing logged.

    Modelled with two real applies rather than a hand-edited epoch, because the
    overwrite is what has to happen for the bug to appear.
    """
    import asyncio

    registry = ToolRegistry()
    mgr = MCPConnectionManager(registry)

    started: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    calls = 0

    async def gated(name, cfg, registry, stack, executor=None, http_auth=None):
        nonlocal calls
        mine = calls
        calls += 1
        started[mine].set()
        if mine == 0:
            await asyncio.Event().wait()  # the reaped apply never finishes on its own
        await release.wait()
        return _connected([])

    with patch(_PATCH, new=gated):
        first: dict[str, object] = {}
        reaped = asyncio.ensure_future(mgr.apply_config({"svc": _cfg()}, attempts=first))
        await asyncio.wait_for(started[0].wait(), timeout=2)
        first_epoch = mgr._conns["svc"].epoch

        # A config apply lands while the first is still handshaking. Changed
        # config, so it tears the old record down and begins a new attempt --
        # which is what moves the epoch.
        second: dict[str, object] = {}
        newer = asyncio.ensure_future(mgr.apply_config({"svc": _cfg("https://moved.test/mcp")}, attempts=second))
        await asyncio.wait_for(started[1].wait(), timeout=2)
        assert mgr._conns["svc"].epoch is not first_epoch
        assert second["svc"] is mgr._conns["svc"].epoch

        # Now the first apply is reaped, and it must consult its OWN token.
        reaped.cancel()
        with suppress(BaseException):
            await reaped
        assert await mgr.reset_for_retry(first) == [], "it reset an attempt it never began"
        assert mgr._conns["svc"].state == "connecting"

        release.set()
        await asyncio.wait_for(newer, timeout=2)

    (snap,) = mgr.status()
    assert snap["state"] == "connected", "the newer apply lost its connection to the reaper"


async def test_aclose_reaps_an_attempt_deliberately_left_running():
    """A detached attempt has no awaiter at all, by design.

    An attempt parked at the browser-authorization step is left running on
    purpose -- its PKCE state is what the published link resolves against, and it
    commits on its own when the user clicks. Right while the process lives, a
    leak once it is stopping: nothing else can reach that task, so shutdown
    returned with a transport and possibly an stdio child still alive.
    """
    import asyncio

    registry = ToolRegistry()
    mgr = MCPConnectionManager(registry)
    entered = asyncio.Event()

    async def parks(name, cfg, registry, stack, executor=None, http_auth=None):
        # Reaching the browser step is what makes sync stop awaiting this.
        conn = mgr._conns[name]
        entered.set()
        if conn.auth_parked is not None:
            conn.auth_parked.set()
        await asyncio.Event().wait()

    with patch(_PATCH, new=parks):
        applied = asyncio.ensure_future(mgr.apply_config({"parked": _cfg()}))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await asyncio.wait_for(applied, timeout=2)

        live = {t for t in mgr._attempt_tasks if not t.done()}
        assert live, "the attempt was supposed to be left running"
        # The handshake inside it, which is the level the shield makes outlive
        # its waiter and therefore the one a reap can miss.
        inner = {t for t in mgr._handshakes if not t.done()}
        assert inner

        # Through aclose, which is the path `AgentLoop.close_mcp` takes: the
        # reap has to be part of shutdown, not a separate call a host must know
        # to make.
        await mgr.aclose()
        assert all(t.done() for t in live), "the detached attempt outlived aclose"
        assert all(t.done() for t in inner), "the handshake outlived aclose"

    assert mgr._attempt_tasks == set()
    assert mgr._handshakes == set()
    assert mgr.status() == []


async def test_a_cancelled_sync_takes_its_handshake_with_it():
    """``asyncio.wait`` does not cancel what it waits on.

    Cancelling whoever started the sync reached the coroutine awaiting the
    attempt, not the attempt -- so the handshake, its transport and its stdio
    child kept running against a sandbox executor the canceller was about to
    close.
    """
    import asyncio

    registry = ToolRegistry()
    mgr = MCPConnectionManager(registry)
    entered = asyncio.Event()

    async def never(name, cfg, registry, stack, executor=None, http_auth=None):
        entered.set()
        await asyncio.Event().wait()

    with patch(_PATCH, new=never):
        applied = asyncio.ensure_future(mgr.apply_config({"svc": _cfg()}))
        await asyncio.wait_for(entered.wait(), timeout=2)
        inner = {t for t in mgr._attempt_tasks}
        assert inner and not any(t.done() for t in inner)

        applied.cancel()
        with suppress(BaseException):
            await applied

        assert all(t.done() for t in inner), "the handshake survived its sync being cancelled"


async def test_reaping_the_attempt_takes_the_shielded_handshake_with_it():
    """Why one reaping site is enough, asserted rather than assumed.

    The handshake runs as its own task because ``_handshake_watchdog`` waits on
    it through ``asyncio.shield``: the bound has to stop the WAITING without
    stopping the work, or a timeout would kill an OAuth flow the user is midway
    through. A shielded task outliving its waiter is exactly what a reap can
    miss, and it used to be reaped separately, after the detach.

    The watchdog takes its shielded task down when its own caller is cancelled,
    so for an attempt ``apply_config`` started, reaping the attempt is enough.
    This pins that half. It does NOT generalise -- ``connect()`` awaits
    ``_run_connect`` inline and has no attempt task at all, which is why
    ``reap_attempts`` sweeps the handshakes too and why
    ``test_aclose_reaps_a_handshake_still_parked_on_authorization`` reds when
    that sweep is removed.
    """
    import asyncio

    registry = ToolRegistry()
    mgr = MCPConnectionManager(registry)
    entered = asyncio.Event()

    async def never(name, cfg, registry, stack, executor=None, http_auth=None):
        entered.set()
        await asyncio.Event().wait()

    with patch(_PATCH, new=never):
        applied = asyncio.ensure_future(mgr.apply_config({"svc": _cfg()}))
        await asyncio.wait_for(entered.wait(), timeout=2)
        inner = {t for t in mgr._handshakes if not t.done()}
        assert inner, "no handshake task to speak of"

        assert await mgr.reap_attempts() == 1
        assert all(t.done() for t in inner), "the shielded handshake survived the attempt reap"

    with suppress(BaseException):
        await applied


async def test_a_connect_started_during_the_reap_is_reaped_too():
    """The window a single snapshot leaves open.

    Reaping used to read both task sets once, await that fixed list, then clear
    the sets. A connect registered during that await -- a reload or an install
    landing on a stack that is shutting down -- was therefore dropped from the
    set without ever being cancelled, and ``aclose`` returned reporting itself
    finished while that handshake, its transport and its stdio child were still
    running.

    The second connect here is started from inside the first one's cancellation,
    which is exactly during the reap's await rather than approximately.
    """
    import asyncio

    registry = ToolRegistry()
    mgr = MCPConnectionManager(registry)
    second_started = asyncio.Event()
    latecomer: list[asyncio.Task] = []
    round_one = asyncio.Event()

    async def hangs(name, cfg, registry, stack, executor=None, http_auth=None):
        if name == "first":
            round_one.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Registered while the reap is awaiting the batch it already read.
                latecomer.append(asyncio.ensure_future(mgr.apply_config({"late": _cfg()})))
                raise
        else:
            second_started.set()
            await asyncio.Event().wait()

    with patch(_PATCH, new=hangs):
        first = asyncio.ensure_future(mgr.apply_config({"first": _cfg()}))
        await asyncio.wait_for(round_one.wait(), timeout=2)

        await mgr.reap_attempts()

        assert second_started.is_set(), "the latecomer never got going; the test proves nothing"
        assert not [t for t in mgr._attempt_tasks if not t.done()], "an attempt registered mid-reap survived"
        assert not [t for t in mgr._handshakes if not t.done()], "a handshake registered mid-reap survived"

    for t in (first, *latecomer):
        t.cancel()
        with suppress(BaseException):
            await t
