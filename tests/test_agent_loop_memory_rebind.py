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

from types import SimpleNamespace

import pytest

from raven.agent.loop.main import AgentLoop
from raven.config.raven import MemoryConfig, SkillForgeRouterConfig
from raven.context_engine.segments.identity import IdentitySegmentBuilder
from raven.context_engine.segments.memory import MemorySegmentBuilder
from raven.memory_engine.skill_forge.backend_source import BackendSkillSource
from raven.memory_engine.skill_forge.router import SkillForgeRouter


class _Backend:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1


def _loop(*, builders: list, router: SkillForgeRouter | None) -> AgentLoop:
    """An AgentLoop with only the parts ``rebind_memory_backend`` touches.

    The builders and the router are the real classes: their private fields are
    the contract this method edits, so a stand-in would let a rename in either
    module pass.
    """
    loop = object.__new__(AgentLoop)
    loop.backend = None
    loop.memory_config = MemoryConfig()
    loop._skill_forge_router_config = SkillForgeRouterConfig(
        weights={"local": 0.96, "everos": 0.8, "everos-cloud": 0.7, "hub": 0.85},
    )
    loop.context = SimpleNamespace(_has_memory_backend=False)
    loop.context_engine = SimpleNamespace(_builders=builders, skills_router=router)
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

    assert loop.backend is None
    assert memory._backend is None
    assert router._sources == []
    assert old.stops == 1
    assert loop.memory_config.backend is None
