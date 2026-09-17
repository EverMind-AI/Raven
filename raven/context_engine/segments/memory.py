"""Segment 3 — ``# Memory``. Host user.md ⊕ EverOS recall(user).

The one composite segment: a single ``# Memory`` heading whose body
merges the host's slow-changing ``user.md`` dump with the backend's
query-conditioned recall hits. Two contributing sources, one owner.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.context_engine.segments import render
from raven.contracts.context import AssemblyContext, Segment
from raven.observability import semconv
from raven.tracing import trace

if TYPE_CHECKING:
    from raven.contracts.memory import MemoryBackend
    from raven.memory_engine import MemoryStore

# The turn's own bound on recall. The backend plugin carries a stricter one so
# its circuit breaker fires first; this is the floor under any third-party
# MemoryBackend, which the Protocol does not oblige to have a timeout at all.
# Raised with HttpMemoryBackend.RECALL_TIMEOUT_S (2.5 -> 8.0) to keep that
# ordering: a hosted cloud backend answers in seconds, not milliseconds.
_RECALL_BUDGET_S: float = 10.0

# This branch exists to score memory backends on SWE-bench-shaped tasks, and
# there the turn's message is an envelope, not a question: a task id, a
# repository, a commit sha and the closing protocol wrapped around the one
# field that describes the work. Searching the envelope retrieves on the
# wrapper -- a sha matches nothing, and an id matches the wrong memory exactly,
# because the ingested pool carries task ids in its text too. Mem0's and
# MemOS's own published harnesses search the problem text alone, so recalling
# on anything else would measure our framing against their numbers.
_PROBLEM_FIELD = "problem_statement:"
# What the harness appends after the field. Whichever comes first ends it.
_PROBLEM_TRAILERS = ("\nmanual.yaml is at ", "\nWhen you believe the task is complete")


def _problem_statement(message: str) -> str:
    """The task description alone, or the message unchanged if it has none.

    A turn that is not a benchmark task has no problem_statement: field and
    is searched whole, which is the ordinary behaviour.
    """
    head = message.find(_PROBLEM_FIELD)
    if head < 0:
        return message
    body = message[head + len(_PROBLEM_FIELD) :]
    cut = min((i for i in (body.find(t) for t in _PROBLEM_TRAILERS) if i >= 0), default=-1)
    if cut >= 0:
        body = body[:cut]
    return body.strip() or message


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
        recall_hits = await self._recall(_problem_statement(ctx.current_message))
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
