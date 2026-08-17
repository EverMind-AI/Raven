"""SkillForgeRouter — fans :meth:`select` out to every registered source and
fuses the per-source rankings via :func:`rrf_merge_weighted`.

Two policies the router enforces (not its sources):

- **Per-source over-fetch.** :meth:`select(k)` asks every source for
  ``k * over_fetch_factor`` hits. RRF then narrows to ``k`` overall.
  Over-fetching matters because a source's #3 hit might be a great
  cross-source merge candidate even if it would never be a top-3 by
  itself. Default factor is 2 — twice the requested ``k``.

- **Single-source failure isolation.** A source that raises (network
  blip on Mass HTTP, EverOS HTTP down) is caught inside
  :meth:`_safe_search` and turns into an empty list for that round.
  The other sources still feed RRF so the router never produces a
  whole-pipeline failure because of one transient.

The router's source list is **fixed at construction**. Per the design
decision, sources are internal and hardcoded (Local + Mass + Everos
arrive in SR-3 / SR-4); third-party skill retrieval extension goes
through :class:`MemoryBackend` rather than through new SkillSource
implementations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from raven.memory_engine.skill_forge.fusion import rrf_merge_weighted
from raven.memory_engine.skill_forge.types import RouterHit, SkillSource

logger = logging.getLogger(__name__)


class SkillForgeRouter:
    """Compose N :class:`SkillSource` outputs into one top-K ranking."""

    def __init__(
        self,
        sources: list[SkillSource],
        *,
        over_fetch_factor: int = 2,
        dedup_by: str = "name",
    ) -> None:
        # The list is captured by reference; callers should pass an
        # already-frozen tuple if they want to forbid mutation. We
        # deliberately don't freeze for them — host wires sources at
        # boot, never mutates, and verbose immutable wrappers add
        # nothing.
        self._sources = sources
        self._over_fetch_factor = max(1, over_fetch_factor)
        self._dedup_by = dedup_by

    async def select(
        self,
        query: str,
        history: list[dict[str, Any]],
        k: int = 5,
    ) -> list[RouterHit]:
        """Fan out to every source concurrently, fuse to top-K."""
        per_source_k = k * self._over_fetch_factor
        per_source = await asyncio.gather(*[self._safe_search(s, query, history, per_source_k) for s in self._sources])
        return rrf_merge_weighted(
            [(s.name, s.weight, hits) for s, hits in zip(self._sources, per_source)],
            k=k,
            dedup_by=self._dedup_by,
        )

    async def _safe_search(
        self,
        source: SkillSource,
        query: str,
        history: list[dict[str, Any]],
        k: int,
    ) -> list[RouterHit]:
        try:
            return await source.search(query, history, k)
        except Exception as e:
            # ``exception()`` writes the traceback; warning-level so a
            # transient blip doesn't spam ``error`` logs but still
            # shows up in normal aggregations.
            logger.warning(
                "skill source %r failed; treating as empty: %s",
                source.name,
                e,
            )
            return []


__all__ = ["SkillForgeRouter"]
