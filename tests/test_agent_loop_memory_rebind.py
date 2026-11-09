"""The running loop's memory backend is a live reference.

The settings switch used to rebuild the whole runtime, which closed the section's
RPC socket and drew as "not connected". These pin the in-place replacement:
the store pipeline's ``self.backend``, the context engine's memory segment, the
skill router's backend source and the config copy the after-turn feedback
routes by all move together, the old backend is stopped after the new one has
started, and a switch out of "memory off" adds the source the boot factory
never built.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from raven.agent.loop.main import AgentLoop
from raven.config.raven import MemoryConfig, SkillForgeRouterConfig
from raven.context_engine.segments.identity import IdentitySegmentBuilder
from raven.context_engine.segments.memory import MemorySegmentBuilder
from raven.memory_engine.skill_forge.backend_source import BackendSkillSource
from raven.memory_engine.skill_forge.router import SkillForgeRouter
from raven.memory_engine.store_pipeline import StorePipeline


class _Backend:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1


class _StoreBackend(_Backend):
    """A backend whose store blocks until the test opens its gate."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.written: list[tuple[str, str]] = []

    async def store(self, session_key, messages, metadata=None) -> bool:
        await self.gate.wait()
        self.written.append((session_key, messages[0]["content"]))
        return True


def _loop(*, builders: list, router: SkillForgeRouter | None) -> AgentLoop:
    """An AgentLoop with only the parts ``rebind_memory_backend`` touches.

    The builders, the router and the store pipeline are the real classes:
    their private fields and queue are the contract this method edits, so a
    stand-in would let a rename or a lost record pass.
    """
    loop = object.__new__(AgentLoop)
    loop.backend = None
    loop.memory_config = MemoryConfig()
    loop._skill_forge_router_config = SkillForgeRouterConfig(
        weights={"local": 0.96, "everos": 0.8, "everos-cloud": 0.7, "hub": 0.85},
    )
    loop.context = SimpleNamespace(_has_memory_backend=False)
    loop.context_engine = SimpleNamespace(_builders=builders, skills_router=router)
    loop._active_turns = 0
    loop._turns_idle = asyncio.Event()
    loop._turns_idle.set()
    loop._memory_rebind_lock = asyncio.Lock()
    loop._queued_memory_switch = None
    loop._queued_switch_task = None
    loop._memory_switch_closed = False
    loop._backend_stop_tasks = {}
    loop._store_pipeline = StorePipeline(
        lambda: loop.backend,
        on_ok=lambda: None,
        on_failure=lambda _reason: None,
    )
    return loop


@pytest.mark.asyncio
async def test_rebind_replaces_the_live_references_and_lifecycle(tmp_path) -> None:
    old, new = _Backend(), _Backend()
    memory = MemorySegmentBuilder(None, old, user_id="u1", memory_top_k=5)
    identity = IdentitySegmentBuilder(tmp_path, has_memory_backend=True)
    source = BackendSkillSource(old, agent_id="a1", name="everos")
    source.weight = 0.8
    router = SkillForgeRouter([source])
    loop = _loop(builders=[memory, identity], router=router)
    loop.backend = old
    loop.memory_config = MemoryConfig(
        backend="everos",
        user_id="u1",
        agent_id="a1",
        memory_top_k=5,
    )

    await loop.rebind_memory_backend(
        new,
        name="everos-cloud",
        user_id="u2",
        agent_id="a2",
        memory_top_k=7,
    )
    await loop.retire_pending_backends()

    assert loop.backend is new
    assert new.starts == 1 and old.stops == 1
    assert memory._backend is new
    assert memory._user_id == "u2" and memory._memory_top_k == 7
    assert identity._has_memory_backend is True
    assert source._backend is new
    assert source._agent_id == "a2"
    assert source.name == "everos-cloud"
    # The configured share for the backend just selected, not the one the
    # source was built with.
    assert source.weight == 0.7
    assert (loop.memory_config.backend, loop.memory_config.user_id) == ("everos-cloud", "u2")
    assert (loop.memory_config.agent_id, loop.memory_config.memory_top_k) == ("a2", 7)


@pytest.mark.asyncio
async def test_rebind_out_of_memory_off_adds_the_backend_source(tmp_path) -> None:
    """A router built with no backend has no source to update, and the page can
    turn memory on from that state."""
    new = _Backend()
    memory = MemorySegmentBuilder(None, None)
    router = SkillForgeRouter([])
    loop = _loop(builders=[memory], router=router)

    await loop.rebind_memory_backend(
        new,
        name="everos",
        user_id="u1",
        agent_id="a1",
        memory_top_k=5,
    )

    assert loop.backend is new and new.starts == 1
    assert memory._backend is new
    (source,) = router._sources
    assert isinstance(source, BackendSkillSource)
    assert (source._backend, source._agent_id, source.name) == (new, "a1", "everos")
    assert source.weight == 0.8


@pytest.mark.asyncio
async def test_rebind_into_memory_off_drops_the_backend_source() -> None:
    """Left in place, the detached source would call ``None.recall`` on every
    skills search -- caught and logged, but a warning per turn."""
    old = _Backend()
    memory = MemorySegmentBuilder(None, old)
    source = BackendSkillSource(old, agent_id="a1", name="everos")
    router = SkillForgeRouter([source])
    loop = _loop(builders=[memory], router=router)
    loop.backend = old
    loop.memory_config = MemoryConfig(backend="everos", user_id="u1", agent_id="a1")

    await loop.rebind_memory_backend(
        None,
        name=None,
        user_id="u1",
        agent_id="a1",
        memory_top_k=5,
    )
    await loop.retire_pending_backends()

    assert loop.backend is None
    assert memory._backend is None
    assert router._sources == []
    assert old.stops == 1
    assert loop.memory_config.backend is None


@pytest.mark.asyncio
async def test_rebind_waits_for_the_turn_in_flight() -> None:
    """A settings click can land mid-turn; the swap must not split that turn
    across two backends, and stop() must not close a client it is calling."""
    old, new = _Backend(), _Backend()
    memory = MemorySegmentBuilder(None, old)
    loop = _loop(builders=[memory], router=None)
    loop.backend = old
    loop._enter_turn()

    switch = asyncio.create_task(
        loop.rebind_memory_backend(
            new,
            name="everos-cloud",
            user_id="u",
            agent_id="a",
            memory_top_k=5,
        ),
    )
    await asyncio.sleep(0)
    assert loop.backend is old
    assert old.stops == 0
    # The new backend is started before the wait, which is harmless: nothing
    # reads it until the swap.
    assert new.starts == 1

    loop._exit_turn()
    await switch
    await loop.retire_pending_backends()

    assert loop.backend is new
    assert memory._backend is new
    assert old.stops == 1


@pytest.mark.asyncio
async def test_a_switch_keeps_a_queued_write_on_its_own_backend(monkeypatch) -> None:
    """Both turns were produced against ``old``; a switch must neither move
    them to ``new`` nor stop ``old`` under them. The second record is still
    queued when the switch lands, so its backend call starts after it."""
    monkeypatch.setattr("raven.agent.loop._shared._STORE_DRAIN_BUDGET_S", 0.01)
    old, new = _StoreBackend(), _Backend()
    loop = _loop(builders=[MemorySegmentBuilder(None, old)], router=None)
    loop.backend = old
    loop._store_pipeline.enqueue("s1", [{"role": "user", "content": "one"}])
    loop._store_pipeline.enqueue("s1", [{"role": "user", "content": "two"}])
    # Let the worker take the first record and block inside the backend call;
    # the second stays queued behind it.
    await asyncio.sleep(0)

    await loop.rebind_memory_backend(
        new,
        name="everos-cloud",
        user_id="u",
        agent_id="a",
        memory_top_k=5,
    )

    assert loop.backend is new
    assert old.stops == 0, "the old backend's records have not settled"

    old.gate.set()
    for _ in range(200):
        if old.stops:
            break
        await asyncio.sleep(0.01)

    assert old.written == [("s1", "one"), ("s1", "two")], (
        "both turns were written to the backend they were produced against; the second "
        "is the one still queued when the switch lands"
    )
    assert old.stops == 1


@pytest.mark.asyncio
async def test_a_failing_stop_does_not_fail_the_switch() -> None:
    class _BadStop(_Backend):
        async def stop(self) -> None:
            self.stops += 1
            raise RuntimeError("the client was already gone")

    old, new = _BadStop(), _Backend()
    loop = _loop(builders=[MemorySegmentBuilder(None, old)], router=None)
    loop.backend = old

    await loop.rebind_memory_backend(
        new,
        name="everos-cloud",
        user_id="u",
        agent_id="a",
        memory_top_k=5,
    )
    await loop.retire_pending_backends()

    assert loop.backend is new
    assert old.stops == 1


@pytest.mark.asyncio
async def test_run_turn_counts_itself_so_a_switch_waits(monkeypatch) -> None:
    """The wiring, not just the counter: a switch requested while ``run_turn``
    runs must wait until the turn returns. The turn body is stubbed, so what
    this pins is the enter/exit pair around it."""
    from contextlib import nullcontext

    from raven.agent.loop import main as main_mod

    monkeypatch.setattr(main_mod, "use_binding", lambda _binding: nullcontext())
    monkeypatch.setattr(
        main_mod,
        "knowledge_scope",
        SimpleNamespace(bind=lambda *_a: nullcontext(), read=lambda *_a: None),
    )
    monkeypatch.setattr(main_mod, "delegate_scope", lambda _table: nullcontext())
    monkeypatch.setattr(main_mod, "charter_scope", lambda _charter: nullcontext())
    monkeypatch.setattr(main_mod, "bind_harness", lambda _harness: nullcontext())

    old, new = _Backend(), _Backend()
    loop = _loop(builders=[], router=None)
    loop.backend = old
    loop.sessions = None
    loop.harness = None
    loop._playbooks = None
    loop._with_live_window = lambda binding: binding
    loop.binding_for_session = lambda _key: object()
    loop._turn_scope = lambda: nullcontext()
    loop.tools = SimpleNamespace(
        session_scope_for=lambda _key: nullcontext(),
        turn_scope=lambda: nullcontext(),
        get=lambda _name: None,
    )
    loop._take_session_charter = lambda _key: None
    loop.session_persona = lambda _key: None

    async def _resolve_playbook_turn(_req, _key, _binding):
        return SimpleNamespace(
            table=None,
            active=False,
            disposition=None,
            selected_playbook=None,
            artifact_name=None,
            capture_workflow=None,
            persisted=False,
        )

    loop._resolve_playbook_turn = _resolve_playbook_turn

    seen: dict = {}

    async def _run_turn(*_args, **_kwargs):
        seen["during"] = loop.active_turns
        seen["switch"] = asyncio.create_task(
            loop.rebind_memory_backend(
                new,
                name="everos-cloud",
                user_id="u",
                agent_id="a",
                memory_top_k=5,
            ),
        )
        await asyncio.sleep(0)
        seen["swapped_during"] = loop.backend is not old
        return "done"

    loop._run_turn = _run_turn
    req = SimpleNamespace(
        conversation="s1",
        source=None,
        direct_target=None,
        text="hi",
        playbook_mode=None,
    )

    out = await loop.run_turn(req, lambda *_a, **_k: None, lambda *_a, **_k: None)

    assert out == "done"
    assert seen["during"] == 1
    assert seen["swapped_during"] is False
    assert loop.active_turns == 0
    await seen["switch"]
    assert loop.backend is new


@pytest.mark.asyncio
async def test_overlapping_switches_retire_each_other_in_order() -> None:
    """Two clients can switch at once. Each must capture the backend current
    when it gets the lock; capturing before it stopped the same backend twice
    and leaked the intermediate one."""

    class _YieldingStart(_Backend):
        async def start(self) -> None:
            await asyncio.sleep(0)
            await super().start()

    old, mid, new = _Backend(), _YieldingStart(), _YieldingStart()
    loop = _loop(builders=[MemorySegmentBuilder(None, old)], router=None)
    loop.backend = old

    await asyncio.gather(
        loop.rebind_memory_backend(
            mid,
            name="everos-cloud",
            user_id="u",
            agent_id="a",
            memory_top_k=5,
        ),
        loop.rebind_memory_backend(
            new,
            name="everos-cloud",
            user_id="u",
            agent_id="a",
            memory_top_k=5,
        ),
    )
    await loop.retire_pending_backends()

    assert loop.backend is new
    assert (mid.starts, new.starts) == (1, 1)
    assert (old.stops, mid.stops, new.stops) == (1, 1, 0)


async def _noop(*_args, **_kwargs) -> None:
    pass


def _dispose_collaborators(loop: AgentLoop) -> None:
    """The loop parts ``RavenRuntime.dispose`` reaches, on a bare test loop."""
    loop.context.skills = SimpleNamespace(stop_file_watcher=lambda: None)
    loop.subagents = SimpleNamespace(cancel_all=_noop)
    loop.stop_plugin_services = _noop
    loop.close_mcp = _noop
    loop.stop = lambda: None

    async def _drain() -> None:
        await loop._store_pipeline.drain(0.1)

    loop.drain_backend_stores = _drain


@pytest.mark.asyncio
async def test_dispose_stops_the_backend_the_loop_serves_now() -> None:
    from raven.core.runtime import RavenRuntime

    old, new = _Backend(), _Backend()
    loop = _loop(builders=[MemorySegmentBuilder(None, old)], router=None)
    loop.backend = old
    _dispose_collaborators(loop)
    rt = RavenRuntime(loop=loop, plugin_registry=None, strategies=None, deliverables=None)

    await loop.rebind_memory_backend(
        new,
        name="everos-cloud",
        user_id="u",
        agent_id="a",
        memory_top_k=5,
    )
    await loop.retire_pending_backends()
    assert rt.backend is new

    await rt.dispose()

    assert old.stops == 1
    assert new.stops == 1, "disposal must stop the backend the switch installed"


@pytest.mark.asyncio
async def test_dispose_drains_a_write_queued_after_memory_was_switched_on() -> None:
    """The reviewer's memory-off case: boot had no backend, the switch
    installed one, and the write it queued must drain at disposal."""
    from raven.core.runtime import RavenRuntime

    new = _StoreBackend()
    loop = _loop(builders=[MemorySegmentBuilder(None, None)], router=None)
    _dispose_collaborators(loop)
    rt = RavenRuntime(loop=loop, plugin_registry=None, strategies=None, deliverables=None)

    await loop.rebind_memory_backend(
        new,
        name="everos-cloud",
        user_id="u",
        agent_id="a",
        memory_top_k=5,
    )
    new.gate.set()
    loop._store_pipeline.enqueue("s1", [{"role": "user", "content": "hi"}])
    await asyncio.sleep(0)
    assert rt.backend is new

    await rt.dispose()

    assert new.written == [("s1", "hi")], "disposal must drain the backend the switch installed"
    assert new.stops == 1


@pytest.mark.asyncio
async def test_cancelling_a_switch_stops_the_backend_it_started() -> None:
    old, new = _Backend(), _Backend()
    loop = _loop(builders=[MemorySegmentBuilder(None, old)], router=None)
    loop.backend = old
    loop._enter_turn()
    task = asyncio.create_task(
        loop.rebind_memory_backend(
            new,
            name="everos-cloud",
            user_id="u",
            agent_id="a",
            memory_top_k=5,
        ),
    )
    await asyncio.sleep(0)
    assert new.starts == 1 and loop.backend is old

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert loop.backend is old
    assert new.stops == 1, "the started backend was never installed; nothing else owns it"
    loop._exit_turn()


@pytest.mark.asyncio
async def test_a_failed_start_stops_the_backend_it_partly_started() -> None:
    """The contract asks stop() to be safe after a failed start, so partial init
    can clean up. A switch whose start fails never installs the backend, so
    nothing else owns it -- the rebind must stop it before it re-raises."""

    class _BadStart(_Backend):
        async def start(self) -> None:
            self.starts += 1
            raise RuntimeError("the local server did not come up")

    old, new = _Backend(), _BadStart()
    loop = _loop(builders=[MemorySegmentBuilder(None, old)], router=None)
    loop.backend = old

    with pytest.raises(RuntimeError, match="did not come up"):
        await loop.rebind_memory_backend(
            new,
            name="everos",
            user_id="u",
            agent_id="a",
            memory_top_k=5,
        )

    assert loop.backend is old
    assert new.starts == 1
    assert new.stops == 1, "a started backend that was never installed is nobody else's to stop"


@pytest.mark.asyncio
async def test_teardown_force_stops_a_backend_whose_write_is_stuck() -> None:
    old, new = _StoreBackend(), _Backend()
    loop = _loop(builders=[MemorySegmentBuilder(None, old)], router=None)
    loop.backend = old
    loop._store_pipeline.enqueue("s1", [{"role": "user", "content": "stuck"}])
    await asyncio.sleep(0)

    await loop.rebind_memory_backend(
        new,
        name="everos-cloud",
        user_id="u",
        agent_id="a",
        memory_top_k=5,
    )
    await loop.retire_pending_backends(timeout=0.05)

    assert old.stops == 1
    # Let the cut write return and the worker leave; nothing may outlive the test.
    old.gate.set()
    await loop._store_pipeline.drain(0.2)


@pytest.mark.asyncio
async def test_retiring_a_backend_cancels_its_pending_start_first(monkeypatch) -> None:
    from raven.core import plugin_stack

    order: list[str] = []

    async def _cancel(_backend) -> None:
        order.append("cancel_pending")

    class _Ordered(_Backend):
        async def stop(self) -> None:
            order.append("stop")
            await super().stop()

    monkeypatch.setattr(plugin_stack, "cancel_pending_backend_starts", _cancel)
    old, new = _Ordered(), _Backend()
    loop = _loop(builders=[MemorySegmentBuilder(None, old)], router=None)
    loop.backend = old

    await loop.rebind_memory_backend(
        new,
        name="everos-cloud",
        user_id="u",
        agent_id="a",
        memory_top_k=5,
    )
    await loop.retire_pending_backends()

    assert order == ["cancel_pending", "stop"]


@pytest.mark.asyncio
async def test_dispose_retires_a_backend_a_switch_to_off_replaced() -> None:
    """Switching to memory-off leaves ``current`` None while the backend the
    switch replaced still owns a queued write and a retirement task."""
    from raven.core.runtime import RavenRuntime

    old = _StoreBackend()  # its gate stays shut: the write is still queued
    loop = _loop(builders=[MemorySegmentBuilder(None, old)], router=None)
    loop.backend = old
    _dispose_collaborators(loop)
    rt = RavenRuntime(loop=loop, plugin_registry=None, strategies=None, deliverables=None)

    loop._store_pipeline.enqueue("s1", [{"role": "user", "content": "pending"}])
    await asyncio.sleep(0)
    await loop.rebind_memory_backend(
        None,
        name=None,
        user_id="u",
        agent_id="a",
        memory_top_k=5,
    )
    assert loop.backend is None

    await rt.dispose()

    assert old.stops == 1, "disposal must retire a backend a switch to off replaced"
    old.gate.set()
    await loop._store_pipeline.drain(0.2)


@pytest.mark.asyncio
async def test_a_switch_asked_for_during_a_turn_is_queued_and_lands_when_it_ends() -> None:
    """A write made from inside a turn cannot wait for that turn.

    The turn is waiting for the write, so a switch that waited for the turn
    would wait for ever. It waits for the turn instead, and lands when the
    turn ends -- which is what the section's confirmation promises.
    """
    old, new = _Backend(), _Backend()
    loop = _loop(builders=[], router=None)
    loop.backend = old
    loop._enter_turn()

    state = await loop.apply_memory_backend(
        new,
        name="everos-cloud",
        user_id="u",
        agent_id="a",
        memory_top_k=5,
    )

    assert state == "queued"
    assert loop.backend is old
    assert new.starts == 0, "the switch must not land under the running turn"

    loop._exit_turn()
    await loop._queued_switch_task

    assert loop.backend is new
    assert new.starts == 1 and old.stops == 1


@pytest.mark.asyncio
async def test_a_queued_switch_that_fails_to_start_says_so() -> None:
    """The write that queued the switch answered "switches when that turn
    finishes" and returned long ago, and nothing awaits the task that lands it.
    A start that raises there was held on that task unseen, while the file
    named a backend the loop never reached."""
    from loguru import logger

    class _Broken(_Backend):
        async def start(self) -> None:
            raise OSError("the everos root is not writable")

    old, broken = _Backend(), _Broken()
    loop = _loop(builders=[], router=None)
    heard: list[str] = []
    loop.memory_assembly = SimpleNamespace(registry=None, notify=heard.append)
    loop.backend = old
    loop._enter_turn()
    assert await loop.apply_memory_backend(broken, name="x", user_id="u", agent_id="a", memory_top_k=5) == "queued"
    logged: list[str] = []
    sink = logger.add(lambda message: logged.append(str(message)), level="ERROR")
    try:
        loop._exit_turn()
        await asyncio.gather(loop._queued_switch_task, return_exceptions=True)
        await asyncio.sleep(0)
    finally:
        logger.remove(sink)

    assert loop.backend is old
    assert broken.stops == 1, "the partial start is still cleaned up"
    assert any("not writable" in line for line in logged), logged
    assert len(heard) == 1 and "not writable" in heard[0], heard


@pytest.mark.asyncio
async def test_a_queued_switch_does_not_land_after_the_generation_is_retired() -> None:
    """The teardown that would have retired the switched-to backend has
    already run by the time a queued switch could land, so landing it would
    install a backend nothing retires."""
    old, new = _Backend(), _Backend()
    loop = _loop(builders=[], router=None)
    loop.backend = old
    loop._enter_turn()

    state = await loop.apply_memory_backend(
        new,
        name="everos-cloud",
        user_id="u",
        agent_id="a",
        memory_top_k=5,
    )
    assert state == "queued"

    await loop.retire_pending_backends()

    assert loop.backend is old
    assert new.starts == 0


@pytest.mark.asyncio
async def test_a_turn_ending_after_the_teardown_does_not_land_a_queued_switch() -> None:
    """The teardown closes the door, not only the queue.

    A turn that ends after the teardown would land a switch that installs a
    backend nothing retires -- the teardown that would have retired it has
    already run.
    """
    old, new = _Backend(), _Backend()
    loop = _loop(builders=[], router=None)
    loop.backend = old
    loop._enter_turn()

    state = await loop.apply_memory_backend(
        new,
        name="everos-cloud",
        user_id="u",
        agent_id="a",
        memory_top_k=5,
    )
    assert state == "queued"

    await loop.retire_pending_backends()
    loop._exit_turn()
    await asyncio.sleep(0)

    assert loop.backend is old
    assert new.starts == 0


@pytest.mark.asyncio
async def test_a_switch_asked_for_after_the_generation_is_retired_is_refused() -> None:
    """The teardown closes the door for the direct path too.

    A settings write can reach the loop after the drain and the retirements
    have run (the TUI's RPC server is cancelled last). Installing a backend
    then would leave it running with nothing to retire it; the durable write
    stays, and the next generation honors it.
    """
    old, new = _Backend(), _Backend()
    loop = _loop(builders=[], router=None)
    loop.backend = old

    await loop.retire_pending_backends()

    state = await loop.apply_memory_backend(
        new,
        name="everos-cloud",
        user_id="u",
        agent_id="a",
        memory_top_k=5,
    )

    assert loop.memory_switch_closed is True
    assert state == "closed"
    assert loop.backend is old
    assert new.starts == 0


class _CleanupGateBackend(_Backend):
    """A backend whose start fails and whose cleanup ``stop`` blocks once."""

    def __init__(self) -> None:
        super().__init__()
        self.stop_entered = asyncio.Event()
        self.stop_calls = 0

    async def start(self) -> None:
        self.starts += 1
        raise RuntimeError("the backend could not start")

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_calls == 1:
            self.stop_entered.set()
            await asyncio.Event().wait()
        self.stops += 1


@pytest.mark.asyncio
async def test_a_second_cancellation_does_not_skip_the_failed_start_cleanup() -> None:
    """A cancellation inside the cleanup still stops the partial init.

    The first cancellation is the request going away; the second lands while
    the cleanup's ``stop()`` is already running. The stop is then owned by a
    retirement task, so a backend whose start failed is still stopped, and
    the generation's teardown bounds it.
    """
    old = _Backend()
    loop = _loop(builders=[], router=None)
    loop.backend = old
    backend = _CleanupGateBackend()

    task = asyncio.create_task(
        loop.rebind_memory_backend(
            backend,
            name="everos-cloud",
            user_id="u",
            agent_id="a",
            memory_top_k=5,
        )
    )
    await backend.stop_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await loop.retire_pending_backends()

    assert backend.stop_calls == 2, "the retirement task ran the cleanup again"
    assert backend.stops == 1
    assert loop.backend is old


class _GatedStartBackend(_Backend):
    """A backend whose start blocks until the test opens its gate."""

    def __init__(self) -> None:
        super().__init__()
        self.start_entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def start(self) -> None:
        self.starts += 1
        self.start_entered.set()
        await self.gate.wait()


@pytest.mark.asyncio
async def test_a_teardown_that_closes_the_door_while_the_start_is_in_flight_refuses_the_swap() -> None:
    """The door is read with no await between the check and the swap.

    A settings write can pass the closed check and then spend seconds inside
    ``backend.start()``; a teardown that runs while it waits must not leave
    the new backend installed with nothing to retire it.
    """
    old = _Backend()
    loop = _loop(builders=[], router=None)
    loop.backend = old
    backend = _GatedStartBackend()

    task = asyncio.create_task(
        loop.apply_memory_backend(
            backend,
            name="everos-cloud",
            user_id="u",
            agent_id="a",
            memory_top_k=5,
        )
    )
    await backend.start_entered.wait()

    await loop.retire_pending_backends()
    backend.gate.set()

    state = await task

    assert state == "closed"
    assert loop.memory_switch_closed is True
    assert loop.backend is old, "the retired generation must not gain a backend"
    assert backend.stops == 1, "the refused swap stops the start it opened"
