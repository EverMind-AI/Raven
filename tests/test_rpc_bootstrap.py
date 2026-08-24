"""``build_rpc_stack``: the engine assembly a non-socket transport reuses.

Three things are pinned, and they fail for different reasons.

**The lifecycle is whole.** The assembly starts cron, binds the subagent submit
and the question broker, and its teardown stops what it started. A hook left
unbound is not an error anywhere -- the turn simply never reaches the surface
that was supposed to render it.

**The channel reaches both collaborators.** One name has two consumers, and
giving it to one of them is worse than giving it to neither: the turn runs,
produces output, and delivers it to a channel with no outlet.

**The build error is latched, not raised.** A bad provider config has to let the
client connect and be told, rather than failing the connection.
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

    def set_submit(self, fn) -> None:
        self.submit = fn


class _FakeBackend:
    def __init__(self, *, start_raises: bool = False, stop_raises: bool = False) -> None:
        self.started = False
        self.stopped = False
        self._start_raises = start_raises
        self._stop_raises = stop_raises

    async def start(self) -> None:
        if self._start_raises:
            raise RuntimeError("everos would not come up")
        self.started = True

    async def stop(self) -> None:
        if self._stop_raises:
            raise RuntimeError("index lock stuck")
        self.stopped = True


class _GatedBackend(_FakeBackend):
    """A backend whose ``start`` parks until released, so a test can hold the
    stack in the state a client closing mid-boot puts it in."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.events: list[str] = []

    async def start(self) -> None:
        self.events.append("start-enter")
        self.entered.set()
        await self.gate.wait()
        self.events.append("start-finish")
        self.started = True

    async def stop(self) -> None:
        self.events.append("stop")
        self.stopped = True


class _FakeTools(dict):
    """A tool registry thin enough for the assembly, plus the one attribute the
    session handlers read back through the factory."""

    tool_names: list[str] = []


class _FakeSkills:
    def list_skills(self, **_kwargs) -> list:
        return []


class _FakeContext:
    def __init__(self) -> None:
        self.skills = _FakeSkills()


class _FakeLoop:
    """The AgentLoop surface build_rpc_stack touches, each hook recorded.

    It also carries the little the session handlers read once they have reached
    the loop THROUGH the factory: without it the handler fails on the fake
    before the factory's return value has been proved to be this object, and the
    test would assert nothing about the closure it exists to check.
    """

    def __init__(self, cron: _FakeCron | None = None, backend: _FakeBackend | None = None) -> None:
        self.tools = _FakeTools()
        self.context = _FakeContext()
        self.subagents = _FakeSubagents()
        self.cron_service = cron
        self.backend = backend
        self.deep_research_broker = None
        self.drained = False

    async def drain_backend_stores(self) -> None:
        self.drained = True

    def set_deep_research_broker(self, broker) -> None:
        self.deep_research_broker = broker


class _FakeAskTool:
    def __init__(self) -> None:
        self.broker = None

    def set_broker(self, broker) -> None:
        self.broker = broker


async def _sink(_frame: dict) -> None:
    pass


async def test_the_stack_owns_the_whole_lifecycle(monkeypatch) -> None:
    from raven.cli import tui_commands

    cron = _FakeCron()
    loop = _FakeLoop(cron)
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: loop)

    stack = await bootstrap.build_rpc_stack(_sink)

    assert stack.agent_loop is loop
    assert loop.subagents.submit is not None, "a subagent result turn has nowhere to run without this"
    assert cron.started is True
    assert cron.on_job is not None, "on_job must be bound before start, or a job firing at once has no callback"

    await stack.teardown()
    assert cron.stopped is True


async def test_the_question_broker_reaches_the_tools_that_ask(monkeypatch) -> None:
    """Both bindings are late: the broker exists before the tool registry does.
    deep_research is bound through the loop rather than through the tool so a
    tool built later by a mid-session enable inherits it too."""
    from raven.cli import tui_commands

    loop = _FakeLoop()
    ask = _FakeAskTool()
    loop.tools["ask_user"] = ask
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: loop)

    stack = await bootstrap.build_rpc_stack(_sink)
    try:
        assert ask.broker is stack.question_broker
        assert loop.deep_research_broker is stack.question_broker
    finally:
        await stack.teardown()


async def test_a_build_error_is_latched_and_raised_on_first_use(monkeypatch) -> None:
    """A bad provider config must not fail the connection: the client still
    connects, and the error surfaces when it asks for a turn -- which is what
    lets the surface show what is wrong instead of just closing."""
    from raven.cli import tui_commands
    from raven.rpc.errors import RpcError

    boom = RpcError(-32603, "provider config is broken")

    def _raise():
        raise boom

    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", _raise)

    stack = await bootstrap.build_rpc_stack(_sink)
    try:
        assert stack.agent_loop is None
        assert stack.build_error is boom
        assert stack.dispatcher.methods(), "the client must still be able to call something"
    finally:
        await stack.teardown()


async def test_the_channel_reaches_both_collaborators_or_nothing_is_delivered(monkeypatch) -> None:
    """One name, two consumers, and getting one of them wrong loses every turn.

    ``build_tui`` registers its delivery outlet under the channel name, and
    ``register_turn_methods`` stamps it on every turn ``turn.send`` submits as
    ``source.channel``. The hub routes a deliverable by that name, so a channel
    argument reaching only one of them is worse than none: the turn runs,
    produces output, and delivers it to a channel with no outlet, with nothing
    anywhere reporting a problem.
    """
    from raven.cli import tui_commands
    from raven.rpc import methods as methods_module
    from raven.rpc import spine as spine_module

    seen: dict[str, object] = {}
    real_spine = spine_module.build_tui

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

    monkeypatch.setattr(spine_module, "build_tui", _spy_spine)
    monkeypatch.setattr(methods_module, "register_turn_methods", _spy_register)
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: _FakeLoop())

    stack = await bootstrap.build_rpc_stack(_sink, channel="acp")
    try:
        assert seen["outlet_channel"] == "acp"
        assert seen["turn_channel"] == "acp", "an outlet on 'acp' fed by turns stamped 'tui' delivers nothing"
    finally:
        await stack.teardown()


async def test_the_channel_defaults_to_the_one_both_sides_already_used() -> None:
    """Every existing caller passes no channel, so the default has to be the
    value the two sides independently defaulted to before it was a parameter."""
    from raven.rpc.methods.turn import register_turn_methods
    from raven.rpc.spine import build_tui

    assert inspect.signature(build_tui).parameters["channel"].default == "tui"
    assert inspect.signature(register_turn_methods).parameters["default_channel"].default == "tui"
    assert inspect.signature(bootstrap.build_rpc_stack).parameters["channel"].default == "tui"


async def test_an_overridden_responder_does_not_remove_the_local_broker(monkeypatch) -> None:
    """Only the transport is replaced. ``approval.respond`` is registered from
    the locally built broker, and a caller that answers approvals its own way is
    not necessarily removing that method from the dispatcher."""
    from raven.cli import tui_commands
    from raven.rpc import spine as spine_module

    seen: dict[str, object] = {}
    real_spine = spine_module.build_tui

    def _spy_spine(agent_loop, emitter, *, approval_responder=None, **kwargs):
        seen["responder"] = approval_responder
        return real_spine(agent_loop, emitter, approval_responder=approval_responder, **kwargs)

    monkeypatch.setattr(spine_module, "build_tui", _spy_spine)
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: _FakeLoop())

    responder = object()
    stack = await bootstrap.build_rpc_stack(_sink, approval_responder=responder)
    try:
        assert seen["responder"] is responder
        assert "approval.respond" in stack.dispatcher.methods()
    finally:
        await stack.teardown()


async def test_the_memory_backend_starts_off_the_critical_path(monkeypatch) -> None:
    """Bringing the backend up can take tens of seconds and may spawn a server,
    so it is backgrounded: a client's first render must not wait on the memory
    path. Backgrounded means the assertion has to yield first -- asserting right
    after the call would pass whether or not the task was ever created."""
    from raven.cli import tui_commands

    backend = _FakeBackend()
    loop = _FakeLoop(backend=backend)
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: loop)

    stack = await bootstrap.build_rpc_stack(_sink)
    try:
        assert backend.started is False, "the assembly must not block on the backend"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert backend.started is True
    finally:
        await stack.teardown()


async def test_a_backend_that_will_not_start_leaves_the_stack_usable(monkeypatch) -> None:
    """A degraded memory path is not a dead connection: the client is already
    attached by then, and the alternative is an unhandled exception in a
    background task that nothing retrieves."""
    from raven.cli import tui_commands

    loop = _FakeLoop(backend=_FakeBackend(start_raises=True))
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: loop)

    stack = await bootstrap.build_rpc_stack(_sink)
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert stack.agent_loop is loop
    finally:
        await stack.teardown()


async def test_teardown_drains_before_it_stops_the_backend(monkeypatch) -> None:
    """Stopping without draining loses whatever the last turn wrote and had not
    persisted; the stop is what releases the embedded index lock the next
    process needs, so both have to happen and in this order."""
    from raven.cli import tui_commands

    backend = _FakeBackend()
    loop = _FakeLoop(backend=backend)
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: loop)

    stack = await bootstrap.build_rpc_stack(_sink)
    await stack.teardown()

    assert loop.drained is True
    assert backend.stopped is True


async def test_teardown_finishes_even_when_every_step_fails(monkeypatch) -> None:
    """Teardown runs while something has already gone wrong. Each step is
    guarded on its own so a failure in one does not skip the rest: the index
    lock has to be released even if cron stop raised, or the next process cannot
    start at all.
    """
    from raven.cli import tui_commands
    from raven.rpc import spine as spine_module

    class _AngryCron(_FakeCron):
        def stop(self) -> None:
            raise RuntimeError("cron will not stop")

    backend = _FakeBackend(stop_raises=True)
    loop = _FakeLoop(_AngryCron(), backend=backend)
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: loop)

    real_spine = spine_module.build_tui

    def _spy_spine(agent_loop, emitter, **kwargs):
        scheduler, hub, turn_ids, _teardown = real_spine(agent_loop, emitter, **kwargs)

        async def _angry_teardown() -> None:
            await _teardown()
            raise RuntimeError("spine will not tear down")

        return scheduler, hub, turn_ids, _angry_teardown

    monkeypatch.setattr(spine_module, "build_tui", _spy_spine)

    stack = await bootstrap.build_rpc_stack(_sink)
    await stack.teardown()  # must not raise

    assert loop.drained is True, "the drain has to be reached past two failures above it"


async def test_reminders_dropped_at_startup_reach_the_first_subscription(monkeypatch) -> None:
    """``start()`` drops past-due one-shot reminders, and whoever starts cron
    owns telling someone. This runs before any client has subscribed, so the
    assertion is that the notice survives that gap and reaches the first
    subscription -- not merely that a call was made."""
    from raven.cli import tui_commands
    from raven.proactive_engine.schedulers.cron.types import CronStartupDrop

    class _DroppingCron(_FakeCron):
        async def start(self) -> None:
            await super().start()
            self.last_startup_drops = [CronStartupDrop(name="pills", message="take the pills", at_ms=1749024000000)]

    loop = _FakeLoop(_DroppingCron())
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: loop)

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
    assert events[0]["payload"]["count"] == 1
    assert events[0]["payload"]["items"][0]["name"] == "pills"


async def test_the_factory_hands_out_the_loop_and_re_raises_a_latched_error(monkeypatch) -> None:
    """The factory is how a handler reaches the engine, and it is the only place
    a latched build error is ever raised. Driven through a registered method
    rather than called directly, because that is the path that exists."""
    from raven.cli import tui_commands
    from raven.rpc.errors import RpcError

    loop = _FakeLoop()
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: loop)
    stack = await bootstrap.build_rpc_stack(_sink)
    try:
        result = await stack.dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": "session.create", "params": {}})
        assert "error" not in result, result
    finally:
        await stack.teardown()

    def _raise():
        raise RpcError(-32603, "provider config is broken")

    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", _raise)
    broken = await bootstrap.build_rpc_stack(_sink)
    try:
        # session.* guards the factory call and degrades rather than failing, so
        # what this pins is that the raise is reached at all: the handler still
        # answers, and the error is not silently swallowed at build time.
        result = await broken.dispatcher.dispatch({"jsonrpc": "2.0", "id": 2, "method": "session.create", "params": {}})
        assert result.get("id") == 2
        assert broken.build_error is not None
    finally:
        await broken.teardown()


async def test_a_builder_that_returns_nothing_is_not_an_error(monkeypatch) -> None:
    """The third state, distinct from both a loop and a latched error: no engine
    and nothing to blame. The handlers degrade to their no-loop bundle, which is
    the documented behaviour of a zero-factory call, so the factory returns None
    rather than inventing an error to raise."""
    from raven.cli import tui_commands

    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: None)

    stack = await bootstrap.build_rpc_stack(_sink)
    try:
        assert stack.agent_loop is None
        assert stack.build_error is None
        result = await stack.dispatcher.dispatch({"jsonrpc": "2.0", "id": 3, "method": "session.create", "params": {}})
        assert "error" not in result, result
        assert result["result"]["session_id"].startswith("tui:")
    finally:
        await stack.teardown()


async def test_teardown_settles_a_start_still_in_flight_before_it_stops(monkeypatch) -> None:
    """A client can connect and close while EverOS is still coming up. The start
    task was fire-and-forget, so teardown drained, stopped and returned with
    start still inside ``backend.start()`` -- the service outliving the stack
    that reported itself closed, and holding the index lock the next process
    needs. Cancel-then-stop is the order: ``MemoryBackend.stop`` is contractually
    safe after a failed start, so it cleans up whatever a half-finished start
    created."""
    from raven.cli import tui_commands

    backend = _GatedBackend()
    loop = _FakeLoop(backend=backend)
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: loop)

    stack = await bootstrap.build_rpc_stack(_sink)
    await asyncio.wait_for(backend.entered.wait(), timeout=5)

    await stack.teardown()

    assert backend.events == ["start-enter", "stop"], backend.events
    assert loop.drained is True
    assert backend.stopped is True

    # Releasing the gate afterwards proves the task is settled and not merely
    # unobserved: an uncancelled start would append start-finish here.
    backend.gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert backend.events == ["start-enter", "stop"], backend.events


async def test_teardown_releases_a_pending_question(monkeypatch) -> None:
    """The third broker on the same transport. An ask raised outside an active
    Spine turn is not cancelled by the turn teardown, so before this it survived
    the disconnect and stayed alive until the broker's own default timeout --
    ten minutes of a wait nobody can answer, because the client is gone."""
    from raven.cli import tui_commands

    loop = _FakeLoop()
    monkeypatch.setattr(tui_commands, "_build_tui_agent_loop", lambda: loop)

    stack = await bootstrap.build_rpc_stack(_sink)
    waiting = asyncio.create_task(
        stack.question_broker.await_question(
            "session-a",
            prompt="keep going?",
            default="no",
            timeout_s=600,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await stack.teardown()
    await asyncio.sleep(0)

    # Done, not merely answerable: ``await_question`` returns its default on
    # cancellation too, so waiting on the task would report success even when
    # nothing released it -- the cancellation would do the releasing.
    assert waiting.done(), "teardown must release the wait, not leave it to its own timeout"
    assert waiting.result() == "no", "a released wait fails safe to its default"
