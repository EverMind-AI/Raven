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
    def __init__(self) -> None:
        self.submit = None
        self.delivery_sink = None

    def set_submit(self, fn) -> None:
        self.submit = fn

    def set_delivery_sink(self, fn) -> None:
        self.delivery_sink = fn


class _FakeLoop:
    """The AgentLoop surface build_rpc_stack touches, each hook recorded."""

    def __init__(self, cron: _FakeCron | None = None) -> None:
        self.tools: dict = {}
        self.subagents = _FakeSubagents()
        self.cron_service = cron
        self.backend = None
        self.deep_research_broker = None
        self.dag_sink = None
        self.mcp_sink = None

    def set_deep_research_broker(self, broker) -> None:
        self.deep_research_broker = broker

    def set_dag_progress_sink(self, sink) -> None:
        self.dag_sink = sink

    def set_mcp_event_sink(self, sink) -> None:
        self.mcp_sink = sink


async def _sink(_frame: dict) -> None:
    pass


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
    finally:
        await stack.teardown()
    # Teardown stops only what this stack built; cron is the host's to stop.
    assert cron.stopped is False


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
