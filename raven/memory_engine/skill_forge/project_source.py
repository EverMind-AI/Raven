"""ProjectSkillSource — the router lane over the turn's project skills.

Each search resolves the working directory bound to the running turn through
:class:`ProjectSkills`, so a router shared by every session of a gateway offers
each turn only its own project's skills. A cold index and the scoring over it
are each waited on for a short bound only: the router gathers every source with
no per-source timeout, so a large project must cost its turn a missing menu
line, never a stalled assembly. A scoring worker left behind by that bound is
told to stop at its next query term instead of ranking for nobody.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from raven.memory_engine.skill_forge.types import RouterHit

if TYPE_CHECKING:
    from raven.memory_engine.skill_forge.project_skills import ProjectSkills


class ProjectSkillSource:
    """ForgeSkillSource adapter over the project's ``.agents/skills`` indexes."""

    name: str = "project"
    weight: float = 0.96

    def __init__(self, project: "ProjectSkills", *, wait_s: float = 0.3) -> None:
        self._project = project
        self._wait_s = wait_s

    async def search(
        self,
        query: str,
        history: list[dict[str, Any]],
        k: int,
    ) -> list[RouterHit]:
        del history
        index = await self._project.index(wait_s=self._wait_s)
        if index is None:
            return []
        stop = threading.Event()
        try:
            ranked = await asyncio.wait_for(
                asyncio.to_thread(index.search, query, k, should_stop=stop.is_set),
                self._wait_s,
            )
        except TimeoutError:
            return []
        finally:
            # Set on every exit: on success the worker has already returned, so
            # the flag only ever reaches a worker this search left behind --
            # one whose wait expired or whose turn was cancelled.
            stop.set()
        return [
            RouterHit(
                qualified_id=f"project/{meta.name}",
                name=meta.name,
                content=meta.content,
                score=score,
                meta={
                    "source": "project",
                    "description": meta.description,
                    "path": str(meta.path),
                },
            )
            for meta, score in ranked
        ]


__all__ = ["ProjectSkillSource"]
