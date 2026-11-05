"""The running loop's memory backend is a live reference.

The settings switch used to rebuild the whole runtime, which closed the page's
RPC socket and drew as "not connected". These pin the in-place replacement:
the store pipeline's ``self.backend``, the context engine's memory segment and
the skill router's backend source all move together, and the old backend is
stopped after the new one has started.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven.agent.loop.main import AgentLoop
from raven.context_engine.segments.memory import MemorySegmentBuilder
from raven.memory_engine.skill_forge.backend_source import BackendSkillSource


class _Backend:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1


@pytest.mark.asyncio
async def test_rebind_replaces_the_live_references_and_lifecycle() -> None:
    old, new = _Backend(), _Backend()
    loop = object.__new__(AgentLoop)
    loop.backend = old
    loop.context = SimpleNamespace(_has_memory_backend=True)
    memory = MemorySegmentBuilder(None, old, user_id="u1", memory_top_k=5)
    source = BackendSkillSource(old, agent_id="a1", name="everos")
    loop.context_engine = SimpleNamespace(
        _builders=[memory],
        skills_router=SimpleNamespace(_sources=[source]),
    )

    await loop.rebind_memory_backend(new, name="everos-cloud", user_id="u2", memory_top_k=7)

    assert loop.backend is new
    assert new.starts == 1 and old.stops == 1
    assert memory._backend is new
    assert memory._user_id == "u2" and memory._memory_top_k == 7
    assert source._backend is new
    assert source.name == "everos-cloud"
