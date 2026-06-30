"""Segment 3 — ``# Memory``. Host user.md ⊕ EverOS recall(user).

The one composite segment: a single ``# Memory`` heading whose body
merges the host's slow-changing ``user.md`` dump with the backend's
query-conditioned recall hits. Two contributing sources, one owner.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.segments import render

if TYPE_CHECKING:
    from raven.memory_engine.backend import MemoryBackend
    from raven.memory_engine.consolidate.consolidator import MemoryStore


class MemorySegmentBuilder:
    name = "memory"
    order = 3
    needs_prefix = False

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
        # Host direct-read (sync) and EverOS recall (async I/O) — the
        # recall propagates on hard failure so a backend outage surfaces
        # at AgentLoop rather than silently dropping memory.
        host = self._memory_store.get_memory_context(current_message=ctx.current_message)
        # Native short-term owns the current session; query EverOS long-term
        # only when native surfaced nothing relevant to this query (on-miss
        # fallback). Normal turns stay native-only.
        recall_hits: list[Any] = []
        if not self._memory_store.has_relevant_long_term(ctx.current_message):
            recall_hits = await self._recall(ctx.current_message, ctx.session_key)
        recall_bullets = render.render_recalled_memory(recall_hits)

        sections = [s for s in (host, recall_bullets) if s]
        meta: dict[str, Any] = {"memory_hits": len(recall_hits)}
        if not sections:
            return Segment(text="", meta=meta)
        return Segment(text="# Memory\n\n" + "\n\n".join(sections), meta=meta)

    async def _recall(self, query: str, current_session_key: str | None = None) -> list[Any]:
        if self._backend is None:
            return []
        # Over-fetch so that dropping current-session hits still leaves up
        # to ``memory_top_k`` to inject.
        raw = list(await self._backend.recall(
            query=query, user_id=self._user_id, top_k=self._memory_top_k * 2,
        ))
        if os.environ.get("DEBUG_MEM_RECALL"):
            logging.getLogger(__name__).info(
                "MEM-RECALL session_key=%r recalled_session_ids=%s",
                current_session_key,
                [(getattr(m, "metadata", None) or {}).get("session_id") for m in raw],
            )
        # Drop hits from the CURRENT session so the ``# Memory`` recall
        # doesn't duplicate the live conversation already present in the
        # history window (long/short-term overlap). A hit without session_id
        # metadata is kept (can't prove it's the current one).
        if current_session_key is not None:
            raw = [
                m for m in raw
                if (getattr(m, "metadata", None) or {}).get("session_id") != current_session_key
            ]
        return raw[: self._memory_top_k]
