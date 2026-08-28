"""``build_rpc_stack`` over a host-owned agent loop (the gateway hosting the page).

The default path builds its own engine and owns its whole lifecycle. The
mounted path is handed the gateway's loop and must leave everything
process-scoped -- the subagent submit, cron, the memory backend -- to the
host, or one engine ends up double-wired by two spines. What it must still
take are the page-facing hooks: while the page is mounted, it is the surface
that renders a question or a progress stream.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from raven.rpc import bootstrap
from raven.rpc.subscriptions import COALESCE_WINDOW_S


class _FakeCron:
    def __init__(self) -> None:
        self.on_job = None
        self.started = False
        self.stopped = False
        self.last_startup_drops: list = []

    async def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _FakeSubagents:
    def __init__(self, order: list | None = None) -> None:
        self.submit = None
        self.delivery_sink = None
        # Shared with the loop, so teardown order is assertable across both.
        self.order = order if order is not None else []

    def set_submit(self, fn) -> None:
        self.submit = fn

    def set_delivery_sink(self, fn) -> None:
        self.delivery_sink = fn

    async def cancel_all(self) -> None:
        # Reached by an owning stack's teardown, which logs and swallows what
        # this raises -- so without it a real assertion failure in such a test
        # is reported behind an AttributeError traceback that is not the bug.
        self.order.append("cancel_all")


class _FakeLoop:
    """The AgentLoop surface build_rpc_stack touches, each hook recorded."""

    def __init__(self, cron: _FakeCron | None = None) -> None:
        self.tools: dict = {}
        self.order: list[str] = []
        self.subagents = _FakeSubagents(self.order)
        self.mcp_closed = 0
        self.cron_service = cron
        self.backend = None
        self.deep_research_broker = None
        self.dag_sink = None
        self.mcp_sink = None
        self.prewarms = 0
        # Whether the MCP event sink was already bound when the prewarm started.
        # The URL an OAuth server parks on rides that sink and nothing else.
        self.sink_at_prewarm: object = "never called"

    def set_deep_research_broker(self, broker) -> None:
        self.deep_research_broker = broker

    def set_dag_progress_sink(self, sink) -> None:
        self.dag_sink = sink

    def set_mcp_event_sink(self, sink) -> None:
        self.mcp_sink = sink

    def prewarm_mcp(self) -> None:
        self.prewarms += 1
        self.sink_at_prewarm = self.mcp_sink

    async def close_mcp(self) -> None:
        self.mcp_closed += 1
        self.order.append("close_mcp")


async def _sink(_frame: dict) -> None:
    pass


class _RecordingBackend:
    """Records the teardown order the owning stack puts it through."""

    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self._order.append("stop")

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        return []

    async def store(self, session_id, messages, *, metadata=None):
        return True

    async def feedback(self, signals):
        pass


async def test_the_owning_stack_drains_queued_writes_before_stopping_the_backend(monkeypatch) -> None:
    """serve and ACP own turns too, and dispatch now returns before the write
    lands. Stopping the backend without draining closes the HTTP client the
    queued writes still need, so this path lost its last turns silently while
    the agent, TUI and gateway hosts did not."""
    from raven.cli import tui_commands

    order: list[str] = []
    loop = _FakeLoop(_FakeCron())
    loop.backend = _RecordingBackend(order)

    async def _drain(*_a, **_kw) -> None:
        order.append("drain")

    loop.drain_backend_stores = _drain
    monkeypatch.setattr(tui_commands, "_build_agent_loop", lambda: loop)

    stack = await bootstrap.build_rpc_stack(_sink, agent_loop=None)
    assert stack.agent_loop is loop
    await stack.teardown()

    assert order == ["drain", "stop"], order


async def test_a_shared_loop_is_used_not_rebuilt(monkeypatch) -> None:
    from raven.cli import tui_commands

    def _boom():
        raise AssertionError("a mounted stack must not build a second engine")

    monkeypatch.setattr(tui_commands, "_build_agent_loop", _boom)
    cron = _FakeCron()
    loop = _FakeLoop(cron)

    stack = await bootstrap.build_rpc_stack(_sink, agent_loop=loop)
    try:
        assert stack.agent_loop is loop
        assert stack.build_error is None
        # Host lifecycle untouched: the subagent submit stays the gateway's
        # (its hubs can route any channel's delivery; this stack's hub only
        # knows the page's), and cron was neither rewired nor started.
        assert loop.subagents.submit is None
        assert cron.on_job is None
        assert cron.started is False
        # Page-facing hooks applied, so the host mounting this stack after its
        # own wiring hands the question and progress surfaces to the page.
        assert loop.deep_research_broker is not None
        # The page's broker is exposed so a host with a question surface of
        # its own can build a RoutingQuestionBroker over both.
        assert stack.question_broker is loop.deep_research_broker
        assert loop.dag_sink is not None
        assert loop.mcp_sink is not None
        assert loop.subagents.delivery_sink is not None
        # The engine is the host's and the host's ``run()`` already connected
        # it; a second prewarm here would re-walk every server it attached.
        assert loop.prewarms == 0
    finally:
        await stack.teardown()
    # Teardown stops only what this stack built; cron is the host's to stop.
    assert cron.stopped is False


async def test_the_snapshot_backfill_is_scheduled_except_on_the_acp_channel(monkeypatch) -> None:
    """An acp row's statefulness comes from its capability snapshot, and this
    hook is the only writer that needs no human -- unscheduled, a fresh install
    reads every acp agent stateless and the instance picker hides them until
    someone opens a settings page and clicks Test. On the acp channel the stack
    is itself a subagent child, where verifying would cascade (each child
    verifying its own row launches another child).
    """
    from raven.agent.subagent import probe

    scheduled: list = []
    monkeypatch.setattr(probe, "schedule_snapshot_verification", lambda manager: scheduled.append(manager) or None)

    loop = _FakeLoop(_FakeCron())
    stack = await bootstrap.build_rpc_stack(_sink, agent_loop=loop)
    try:
        assert scheduled == [loop.subagents]
    finally:
        await stack.teardown()

    acp_loop = _FakeLoop(_FakeCron())
    acp_stack = await bootstrap.build_rpc_stack(_sink, agent_loop=acp_loop, channel="acp")
    try:
        assert scheduled == [loop.subagents]
    finally:
        await acp_stack.teardown()


async def test_the_default_path_still_owns_the_whole_lifecycle(monkeypatch) -> None:
    from raven import browser as browser_module
    from raven.cli import tui_commands

    class _NoBrowser:
        async def close(self) -> None:
            pass

    monkeypatch.setattr(browser_module, "get_browser", lambda: _NoBrowser())
    cron = _FakeCron()
    loop = _FakeLoop(cron)
    monkeypatch.setattr(tui_commands, "_build_agent_loop", lambda: loop)

    stack = await bootstrap.build_rpc_stack(_sink)

    assert stack.agent_loop is loop
    assert loop.subagents.submit is not None
    assert cron.started is True
    assert cron.on_job is not None

    await stack.teardown()
    assert cron.stopped is True


async def test_the_owning_path_prewarms_mcp_instead_of_charging_the_first_turn(monkeypatch) -> None:
    """This stack never starts ``run()``, and ``run()`` is where the one-time MCP
    connect lived -- so the cost landed on whatever turn arrived first (measured:
    4.52s of handshake before the turn body began). The prewarm has to be ordered
    after the event sink is bound, or an OAuth server's authorization URL is
    minted with nobody to publish it to.
    """
    from raven import browser as browser_module
    from raven.cli import tui_commands

    class _NoBrowser:
        async def close(self) -> None:
            pass

    monkeypatch.setattr(browser_module, "get_browser", lambda: _NoBrowser())
    loop = _FakeLoop(_FakeCron())
    monkeypatch.setattr(tui_commands, "_build_agent_loop", lambda: loop)

    stack = await bootstrap.build_rpc_stack(_sink)
    try:
        assert loop.prewarms == 1
        assert loop.sink_at_prewarm is loop.mcp_sink
        assert loop.sink_at_prewarm is not None
    finally:
        await stack.teardown()


async def test_the_served_page_hears_reminders_dropped_at_startup(monkeypatch) -> None:
    """``start()`` drops past-due one-shot reminders; whoever starts cron owns
    telling someone. This path is the served page's, and it runs before any
    client has subscribed -- so the assertion is that the notice survives that
    gap and reaches the first subscription, not merely that a call was made.
    """
    from raven import browser as browser_module
    from raven.cli import tui_commands
    from raven.proactive_engine.schedulers.cron.types import CronStartupDrop

    class _NoBrowser:
        async def close(self) -> None:
            pass

    class _DroppingCron(_FakeCron):
        async def start(self) -> None:
            await super().start()
            self.last_startup_drops = [CronStartupDrop(name="pills", message="take the pills", at_ms=1749024000000)]

    monkeypatch.setattr(browser_module, "get_browser", lambda: _NoBrowser())
    cron = _DroppingCron()
    loop = _FakeLoop(cron)
    monkeypatch.setattr(tui_commands, "_build_agent_loop", lambda: loop)

    frames: list[dict] = []

    async def _record(frame: dict) -> None:
        frames.append(frame)

    stack = await bootstrap.build_rpc_stack(_record)
    try:
        assert [f for f in frames if f.get("method") == "event"] == []
        await stack.emitter.register("tui:default")
        await asyncio.sleep(COALESCE_WINDOW_S * 3)
    finally:
        await stack.teardown()

    events = [f["params"]["event"] for f in frames if f.get("method") == "event"]
    assert [e["type"] for e in events] == ["cron.missed"]
    assert events[0]["payload"] == {
        "count": 1,
        "items": [
            {
                "name": "pills",
                "scheduled_at": "2025-06-04T08:00:00+00:00",
                "message": "take the pills",
            }
        ],
    }


def test_the_approval_broker_is_conversation_scoped() -> None:
    """A protected-command overlay interrupts one conversation, so it belongs to
    the surface that sent that turn -- not to every socket sharing the page's
    ``/rpc``, which since the TUI relay includes other terminals. The broker is
    not exposed on ``RpcStack``, so pin the assembly source; the routing itself
    is covered in tests/test_approval_broker.py.
    """
    import inspect

    src = inspect.getsource(bootstrap.build_rpc_stack)
    assert "ApprovalBroker(send_frame=conversation_scoped(send_frame))" in src


def test_the_confirm_broker_is_conversation_scoped() -> None:
    """Same reason, and it is the field that made it possible: a destructive
    command's yes/no now names the conversation it was raised in, so it can reach
    that surface instead of every terminal attached to the gateway. Pinned at the
    assembly for the same reason as the approval broker above; the routing and
    the no-conversation fallback are covered in tests/test_rpc_confirm.py.
    """
    import inspect

    src = inspect.getsource(bootstrap.build_rpc_stack)
    assert "ConfirmBroker(send_frame=conversation_scoped(send_frame))" in src


async def test_the_channel_reaches_both_collaborators_or_nothing_is_delivered(monkeypatch) -> None:
    """One name, two consumers, and getting one of them wrong loses every turn.

    ``build_rpc_spine`` registers its delivery outlet under the channel name, and
    ``register_turn_methods`` stamps it on every turn ``turn.send`` submits as
    ``source.channel``. ``spine/turn.py`` says outright that the default channel
    MUST match the channel the outlet was registered under or the reply is
    dropped -- so a ``channel`` argument that reached only one of them would be
    worse than none: the turn runs, produces output, and delivers it to a channel
    with no outlet, with nothing anywhere reporting a problem.
    """
    from raven.cli import tui_commands
    from raven.rpc import methods as methods_module
    from raven.rpc import spine as spine_module

    seen: dict[str, object] = {}
    real_spine = spine_module.build_rpc_spine

    def _spy_spine(agent_loop, emitter, *, channel="tui", **kwargs):
        seen["outlet_channel"] = channel
        return real_spine(agent_loop, emitter, channel=channel, **kwargs)

    # Patched on the umbrella and not on ``methods.turn``: the umbrella imported
    # the function into its own namespace at import time, so a patch on the
    # defining module is never consulted -- and the test would pass while
    # asserting nothing.
    real_register = methods_module.register_turn_methods

    def _spy_register(dispatcher, *, default_channel="tui", **kwargs):
        seen["turn_channel"] = default_channel
        return real_register(dispatcher, default_channel=default_channel, **kwargs)

    monkeypatch.setattr(spine_module, "build_rpc_spine", _spy_spine)
    monkeypatch.setattr(methods_module, "register_turn_methods", _spy_register)
    monkeypatch.setattr(tui_commands, "_build_agent_loop", lambda: _FakeLoop())

    stack = await bootstrap.build_rpc_stack(_sink, agent_loop=_FakeLoop(), channel="acp")
    try:
        assert seen["outlet_channel"] == "acp"
        assert seen["turn_channel"] == "acp", "an outlet on 'acp' fed by turns stamped 'tui' delivers nothing"
    finally:
        await stack.teardown()


async def test_the_channel_defaults_to_the_one_both_sides_already_used() -> None:
    """Every existing caller passes no channel, so the default has to be the
    value the two sides independently defaulted to before it was a parameter."""
    from raven.rpc.methods.turn import register_turn_methods
    from raven.rpc.spine import build_rpc_spine

    assert inspect.signature(build_rpc_spine).parameters["channel"].default == "tui"
    assert inspect.signature(register_turn_methods).parameters["default_channel"].default == "tui"
    assert inspect.signature(bootstrap.build_rpc_stack).parameters["channel"].default == "tui"


def test_the_served_shutdown_stops_subagents_before_it_stops_the_backend() -> None:
    """Closing the memory adapter while a sub-agent run is still going fails
    that run's next write for a reason the service had no part in."""
    src = (Path(__file__).resolve().parents[1] / "raven" / "rpc" / "bootstrap.py").read_text(encoding="utf-8")
    cancel = src.index("await agent_loop.subagents.cancel_all()")
    stop = src.index("await agent_loop.backend.stop()")

    assert cancel < stop


async def test_owning_teardown_closes_the_mcp_it_opened_at_assembly(monkeypatch) -> None:
    """Assembly now opens MCP transports (and stdio children, and the sandbox)
    whether or not a turn is ever served, so a stack stopped before its first
    turn has resources to release that it never used to have.
    """
    from raven import browser as browser_module
    from raven.cli import tui_commands

    class _NoBrowser:
        async def close(self) -> None:
            pass

    monkeypatch.setattr(browser_module, "get_browser", lambda: _NoBrowser())
    loop = _FakeLoop(_FakeCron())
    # A real backend too, so the one assertion below pins where MCP sits among
    # everything else the engine owns rather than only that it is closed at all.
    loop.backend = _RecordingBackend(loop.order)

    async def _drain(*_a, **_kw) -> None:
        loop.order.append("drain")

    loop.drain_backend_stores = _drain
    monkeypatch.setattr(tui_commands, "_build_agent_loop", lambda: loop)

    stack = await bootstrap.build_rpc_stack(_sink)
    assert loop.prewarms == 1
    assert loop.mcp_closed == 0

    await stack.teardown()
    assert loop.mcp_closed == 1
    # Sub-agents first: a run still going can hand the backend another write, and
    # it runs its tools through the executor close_mcp() tears down. MCP last of
    # the engine's own resources, so the executor outlives everything using it.
    assert loop.order == ["cancel_all", "drain", "stop", "close_mcp"]


async def test_a_mounted_stack_does_not_close_its_host_s_mcp(monkeypatch) -> None:
    cron = _FakeCron()
    loop = _FakeLoop(cron)
    stack = await bootstrap.build_rpc_stack(_sink, agent_loop=loop)
    await stack.teardown()
    assert loop.mcp_closed == 0
