"""Segment 3 — ``# Memory``. Host user.md ⊕ EverOS recall(user).

The one composite segment: a single ``# Memory`` heading whose body
merges the host's slow-changing ``user.md`` dump with the backend's
query-conditioned recall hits. Two contributing sources, one owner.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.segments import render
from raven.tracing import semconv, trace

if TYPE_CHECKING:
    from raven.memory_engine.backend import MemoryBackend
    from raven.memory_engine.consolidate.consolidator import MemoryStore

# The turn's own bound on recall. The backend plugin carries a stricter one so
# its circuit breaker fires first; this is the floor under any third-party
# MemoryBackend, which the Protocol does not oblige to have a timeout at all.
_RECALL_BUDGET_S: float = 5.0


class MemorySegmentBuilder:
    name = "memory"
    order = 3
    needs_prefix = False
    # Host memory is picked per message and EverOS recall is a query against
    # it, so this segment answers to the user's latest words.
    stable = False

    def __init__(
        self,
        memory_store: "MemoryStore",
        backend: "MemoryBackend | None" = None,
        user_id: str = "default",
        memory_top_k: int = 5,
    ) -> None:
        self._memory_store = memory_store
        self._backend = backend
        self._user_id = user_id
        self._memory_top_k = memory_top_k

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        # Host direct-read (sync) and EverOS recall (async I/O). The recall is
        # bounded and degrades to no hits: memory enhances an answer, it does
        # not gate one, and an outage must not hold the turn before the model
        # call.
        host = self._memory_store.get_memory_context(current_message=ctx.current_message)
        recall_hits = await self._recall(ctx.current_message)
        recall_bullets = render.render_recalled_memory(recall_hits)

        sections = [s for s in (host, recall_bullets) if s]
        meta: dict[str, Any] = {"memory_hits": len(recall_hits)}
        if not sections:
            return Segment(text="", meta=meta)
        return Segment(text="# Memory\n\n" + "\n\n".join(sections), meta=meta)

    @trace.instrument("memory.recall", extract=semconv.memory_recall)
    async def _recall(self, query: str) -> list[Any]:
        if self._backend is None:
            return []
        try:
            hits = await asyncio.wait_for(
                self._backend.recall(
                    query=query,
                    user_id=self._user_id,
                    top_k=self._memory_top_k,
                ),
                timeout=_RECALL_BUDGET_S,
            )
        except TimeoutError:
            logger.warning(
                "memory recall exceeded its {}s turn budget; continuing this turn without recalled memory",
                _RECALL_BUDGET_S,
            )
            return []
        except Exception as e:
            logger.warning(
                "memory recall failed ({}: {}); continuing this turn without recalled memory",
                type(e).__name__,
                e,
            )
            return []
        return list(hits)
