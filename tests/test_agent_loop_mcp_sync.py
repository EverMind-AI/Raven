"""The seam between a real ``AgentLoop`` and the MCP connection manager.

These use a real loop instance, not a fake one. The whole ``plug.*`` surface
calls ``loop.apply_mcp_config`` / ``loop.mcp_manager`` / ``loop._mcp_executor``, and a
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
from raven.config.schema import MCPServerConfig
from raven.contracts.tool import Tool
from raven.mcp.naming import MCPToolRef
from raven.providers.base import LLMProvider
from tests._wiring import wire

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
            # Registered the way a real connect does -- with its origin. The
            # registry's origin index is what the manager reads its own teardown
            # list back out of, so a stub without one is invisible to it.
            registry.register(_FakeTool(full), origin=MCPToolRef(name=full, server=name, tool=t))
            out.append(full)
        return _connected(out)

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
        mcp_servers=servers or {},
        **wire(max_iterations=1, **kw),
    )


def test_the_loop_exposes_exactly_what_the_plug_handlers_call(workspace) -> None:
    """`plug.*` reaches for these three by name; nothing else asserts they exist."""
    loop = _loop(workspace)

    assert callable(loop.apply_mcp_config)
    assert callable(loop._mcp_executor)
    manager = loop.mcp_manager
    for method in ("status", "connect", "disconnect", "apply_config", "config_changed", "aclose", "tool_map"):
        assert callable(getattr(manager, method)), method
    # plug.auth passes the executor provider straight through to connect().
    assert loop.mcp_manager is manager, "the manager must be stable across calls"


async def test_apply_mcp_config_attaches_a_server_while_the_loop_runs(workspace) -> None:
    loop = _loop(workspace)

    with patch(_PATCH, new=_fake_connect(["search"])):
        result = await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp")})

    assert result.tools_changed is True
    assert loop.tools.has("mcp_svc_search")
    (snap,) = loop.mcp_manager.status()
    assert snap["state"] == "connected"


async def test_apply_mcp_config_detaches_a_server_the_config_dropped(workspace) -> None:
    loop = _loop(workspace)

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp")})
        await loop.apply_mcp_config({})

    assert not loop.tools.has("mcp_svc_search")
    assert loop.mcp_manager.status() == []


async def test_an_unloaded_tool_leaves_the_schema_and_answers_both_readings(workspace) -> None:
    """What a turn in flight gets when its server is unloaded under it.

    That turn's prompt was assembled while the tool existed -- the model was
    told it exists -- so the answer must not read as "you got the name wrong".
    An earlier revision kept a tombstone naming the server to say so; the same
    thing is now said by naming both readings in the miss itself, which costs
    twelve tokens and no state.
    """
    loop = _loop(workspace)

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp")})
        await loop.apply_mcp_config({})

    # Gone from the schema, so the next turn never offers it.
    assert "mcp_svc_search" not in [d["function"]["name"] for d in loop.tools.get_definitions()]
    out = await loop.tools.execute("mcp_svc_search", {})
    assert "may have been unloaded" in out
    # And no catalog dump: the other tools are not named back at the model.
    assert "read_file" not in out


async def test_the_server_coming_back_makes_its_tools_callable_again(workspace) -> None:
    loop = _loop(workspace)
    cfg = {"svc": MCPServerConfig(url="https://svc.test/mcp")}

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.apply_mcp_config(cfg)
        await loop.apply_mcp_config({})
        assert not loop.tools.has("mcp_svc_search")
        await loop.apply_mcp_config(cfg)

    assert loop.tools.has("mcp_svc_search")


async def test_a_probe_against_unchanged_config_reports_no_work(workspace) -> None:
    loop = _loop(workspace)
    servers = {"svc": MCPServerConfig(url="https://svc.test/mcp")}

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.apply_mcp_config(servers)

    assert loop.mcp_config_changed(servers) is False
    assert loop.mcp_config_changed({}) is True


async def test_apply_marks_mcp_as_up_so_the_lazy_connect_does_not_re_walk_it(workspace) -> None:
    """``_connect_mcp`` short-circuits on ``_mcp_connected``.

    Left unset by ``apply_mcp_config``, a server this apply already attached
    would be walked a second time by the one-shot lazy connect on the next turn.
    """
    loop = _loop(workspace)

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp")})

    assert loop._mcp_connected is True


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
        await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp")})
        assert loop.tools.has("mcp_svc_search")
        await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp", enabled=False)})

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


async def test_a_blacklisted_mcp_tool_is_withheld_however_late_it_arrives(workspace) -> None:
    """An MCP server can register a name the operator switched off, and it may do
    so on any connect rather than only the first.

    Nothing has to be re-applied for that now: withholding is decided when the
    tool array is assembled, so a name arriving later is covered by the same read
    as a name that was there all along. What this pins is the outcome -- the tool
    is not offered -- and the two facts that follow from getting there by
    withholding rather than by unregistering.
    """
    loop = _loop(workspace, disabled_tools=["mcp_svc_search"])

    with patch(_PATCH, new=_fake_connect(["search", "fetch"])):
        await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp")})

    offered = {d["function"]["name"] for d in loop.tools.get_definitions()}
    assert "mcp_svc_search" not in offered
    assert "mcp_svc_fetch" in offered

    # Both are registered, so the server's record and the registry agree again.
    # It used to hold 1 of the 2 it had connected, because the blacklist had
    # unregistered the other behind its back -- and a record that disagrees with
    # the registry is what made a later disconnect have to be careful about
    # unregistering a name it never owned.
    (snap,) = loop.mcp_manager.status()
    assert snap["tool_count"] == 2
    assert loop.tools.has("mcp_svc_search")


async def test_close_mcp_detaches_everything(workspace) -> None:
    loop = _loop(workspace)

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp")})
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
        await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp")})

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
        await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp")})
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


async def test_a_turn_does_not_wait_for_a_server_that_is_waiting_on_a_person(workspace) -> None:
    """A turn hands the connect off instead of bounding it.

    A server parked at the browser-authorization step blocks on a human for up to
    the OAuth flow's timeout, and that once held a user's message for 10m35s. The
    turn used to survive it with a 90s bound; now it does not join the handshake
    at all, and the connect must still not be cancelled to get there.
    """
    import asyncio

    released = asyncio.Event()

    async def parked(name, cfg, registry, stack, executor=None, http_auth=None):
        await released.wait()
        registry.register(
            _FakeTool(f"mcp_{name}_late"), origin=MCPToolRef(name=f"mcp_{name}_late", server=name, tool="late")
        )
        return _connected([f"mcp_{name}_late"])

    loop = _loop(workspace, {"parks": MCPServerConfig(url="https://parks.test/mcp")})

    with patch(_PATCH, new=parked):
        loop.prewarm_mcp()
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


async def test_the_awaited_caller_still_waits_for_the_whole_sync(workspace) -> None:
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
    from raven.mcp.oauth import OAuthWaitTimeoutError

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
    from raven.contracts.assembled import TokenBudget
    from raven.contracts.context import AssemblyContext

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


async def test_prewarm_brings_mcp_up_with_no_turn_involved(workspace) -> None:
    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})

    with patch(_PATCH, new=_fake_connect(["search"])):
        loop.prewarm_mcp()
        await loop._mcp_prewarm_task

    assert loop._mcp_connected is True
    assert loop._mcp_connecting is False
    assert loop.tools.has("mcp_svc_search")
    (snap,) = loop.mcp_manager.status()
    assert snap["state"] == "connected"


async def test_prewarm_claims_the_connect_before_it_yields(workspace) -> None:
    """The claim has to be synchronous. A coroutine given to ``create_task`` does
    not run until the loop next yields, and a turn arriving in that gap would read
    ``_mcp_connecting`` as unset and run the whole blocking connect itself.
    """
    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})

    with patch(_PATCH, new=_fake_connect(["search"])):
        loop.prewarm_mcp()
        # No await between the call above and this line, on purpose.
        assert loop._mcp_connecting is True
        await loop._mcp_prewarm_task


async def test_a_turn_overtaking_the_prewarm_does_not_wait_for_it(workspace) -> None:
    """The point of the whole change: the turn-side bound is never reached
    because the turn no longer joins the handshake at all.
    """
    import asyncio

    gate = asyncio.Event()
    entered = asyncio.Event()

    async def slow(name, cfg, registry, stack, executor=None, http_auth=None):
        entered.set()
        await gate.wait()
        return _connected([])

    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})
    with patch(_PATCH, new=slow):
        loop.prewarm_mcp()
        # The handshake is genuinely in flight before the turn arrives, which is
        # what a served turn meets: the transport starts listening well after
        # build_rpc_stack returns.
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        # What a turn now does. Would have blocked on the handshake before.
        loop.prewarm_mcp()
        assert loop._mcp_connected is False, "the handshake is still gated, so nothing is up yet"
        gate.set()
        await loop._mcp_prewarm_task

    assert loop._mcp_connected is True


async def test_a_turn_overtaking_the_prewarm_is_told_the_servers_are_connecting(workspace) -> None:
    """Absent tools with no explanation is how the model reports a configured
    plugin as a capability that does not exist.
    """
    import asyncio

    gate = asyncio.Event()
    entered = asyncio.Event()

    async def slow(name, cfg, registry, stack, executor=None, http_auth=None):
        entered.set()
        await gate.wait()
        return _connected([])

    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})
    with patch(_PATCH, new=slow):
        loop.prewarm_mcp()
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        loop.prewarm_mcp()

        (note,) = loop._mcp_tool_notices()
        assert "svc" in note
        assert "still connecting" in note
        assert "later turn" in note

        gate.set()
        await loop._mcp_prewarm_task

    # Up: the reason to mention it is gone with it.
    assert loop._mcp_tool_notices() == []


async def test_prewarm_reports_a_sandbox_that_cannot_start_instead_of_vanishing(workspace) -> None:
    """Nobody awaits the prewarm task, so an exception left in it is only a
    'never retrieved' warning at shutdown. ``run()`` shuts the loop down on this
    one; here the turns keep running without the stdio servers.
    """
    from raven.sandbox import SandboxInitError

    async def no_sandbox(name, cfg, registry, stack, executor=None, http_auth=None):
        raise SandboxInitError("no boxlite here")

    loop = _loop(workspace, {"svc": MCPServerConfig(command="npx", args=["x"])})
    with patch(_PATCH, new=no_sandbox):
        loop.prewarm_mcp()
        await loop._mcp_prewarm_task

    assert loop._mcp_prewarm_task.exception() is None
    assert loop._mcp_connecting is False, "a failed prewarm must not leave the flag set"
    assert loop._mcp_connected is False


async def test_prewarm_costs_nothing_when_no_server_is_configured(workspace) -> None:
    loop = _loop(workspace)
    loop.prewarm_mcp()
    assert loop._mcp_prewarm_task is None
    assert loop._mcp_connecting is False


async def test_prewarm_does_not_re_walk_servers_a_hot_reload_already_attached(workspace) -> None:
    """``apply_mcp_config`` is the runtime-reconfigure path (plug.install,
    reload.mcp) and it sets ``_mcp_connected`` itself. A prewarm that lost that
    race must find the work done rather than connect every server a second time.
    """
    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})

    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop.apply_mcp_config({"svc": MCPServerConfig(url="https://svc.test/mcp")})
        loop.prewarm_mcp()

    assert loop._mcp_prewarm_task is None


async def test_a_failed_turn_leaves_a_reaped_prewarm_retryable(workspace) -> None:
    """The window the fire-and-forget opened: the turn body now runs beside the
    handshake, and the turn's own error path closes the executor a stdio connect
    is spawned into. Left alone, the manager records that as the server's
    ``error`` state while ``apply_mcp_config`` still sets ``_mcp_connected``, so
    every later prewarm stops at the connected guard and no reload retries an
    ``error`` row -- the server is gone until a restart.
    """
    import asyncio

    entered = asyncio.Event()
    gate = asyncio.Event()

    async def slow(name, cfg, registry, stack, executor=None, http_auth=None):
        entered.set()
        await gate.wait()
        return _connected([])

    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})
    with patch(_PATCH, new=slow):
        loop.prewarm_mcp()
        await asyncio.wait_for(entered.wait(), timeout=2.0)

        inner = {t for t in loop.mcp_manager._attempt_tasks}
        assert inner and not any(t.done() for t in inner)

        await loop.reap_mcp_prewarm()

        # The handshake itself is gone, not just its awaiter: the executor this
        # turn is about to close is what a stdio transport was spawned into.
        assert all(t.done() for t in inner), "the handshake outlived the reap"

        # Retryable: nothing claims MCP is up, and no record is parked in a
        # state a reload refuses to touch.
        assert loop._mcp_connected is False
        assert loop._mcp_connecting is False
        assert loop._mcp_prewarm_task is None
        assert [s["state"] for s in loop.mcp_manager.status()] == ["disconnected"]

        # And the next turn really does connect it.
        gate.set()
        loop.prewarm_mcp()
        await loop._mcp_prewarm_task

    assert loop._mcp_connected is True
    assert [s["state"] for s in loop.mcp_manager.status()] == ["connected"]


async def test_close_mcp_reaps_the_prewarm_before_tearing_anything_down(workspace) -> None:
    """Order, not just cleanup: a live handshake holds the manager and the
    executor this closes, and a connect that outlives them registers its tools
    into a registry whose sessions are gone.
    """
    import asyncio

    entered = asyncio.Event()
    closed_while_running: list[bool] = []

    async def never(name, cfg, registry, stack, executor=None, http_auth=None):
        entered.set()
        await asyncio.Event().wait()  # never returns on its own

    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})
    with patch(_PATCH, new=never):
        loop.prewarm_mcp()
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        task = loop._mcp_prewarm_task
        assert task is not None and not task.done()

        inner = {t for t in loop.mcp_manager._attempt_tasks}
        assert inner and not any(t.done() for t in inner)

        await loop.close_mcp()
        closed_while_running.append(task.done())
        # The wrapper being done proves nothing about the handshake. `asyncio.wait`
        # does not cancel what it waits on, so the attempt task -- and the
        # transport and stdio child inside it -- used to survive both the reap and
        # this close, and teardown returned with them alive.
        assert all(t.done() for t in inner), "the attempt task outlived close_mcp"

    assert closed_while_running == [True], "close_mcp must not return with the handshake live"
    assert loop._mcp_prewarm_task is None
    assert loop._mcp_connecting is False


async def test_every_snapshot_carries_the_authorization_url_key(workspace) -> None:
    """Null included. A reader that seeds this from a pull needs the later
    ``mcp.status`` to carry the key in order to clear it -- omitted, the merge
    leaves a settled server showing the link it was parked on.
    """
    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})
    with patch(_PATCH, new=_fake_connect(["search"])):
        await loop._connect_mcp()

    (snap,) = loop.mcp_manager.status()
    assert "auth_url" in snap
    assert snap["auth_url"] is None


async def test_the_prewarm_holds_its_own_attempt_token_for_the_reap(workspace) -> None:
    """The call site, not just the manager rule: the reap must pass the mapping
    THIS prewarm's apply filled, not whatever the manager saw most recently.
    """
    import asyncio

    seen: list[dict] = []

    loop = _loop(workspace, {"svc": MCPServerConfig(url="https://svc.test/mcp")})
    mgr = loop.mcp_manager

    async def _record(attempts):
        seen.append(attempts)
        return []

    mgr.reset_for_retry = _record

    entered = asyncio.Event()

    async def slow(name, cfg, registry, stack, executor=None, http_auth=None):
        entered.set()
        await asyncio.Event().wait()

    with patch(_PATCH, new=slow):
        loop.prewarm_mcp()
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        token = loop._mcp_prewarm_attempts
        assert token == {"svc": mgr._conns["svc"].epoch}

        await loop.reap_mcp_prewarm()

    assert seen == [token], "the reap passed a different mapping than the prewarm began"
    assert loop._mcp_prewarm_attempts == {}, "the token must not survive its own reap"
