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

The router's source list is fixed in its membership, not in its lifetime:
the sources are internal (Local, EverOS and Hub) and a third-party skill
retrieval extension goes through :class:`MemoryBackend` rather than through
new ForgeSkillSource implementations. The one exception is the memory
backend's source, which a live switch adds or removes with
:meth:`add_source` / :meth:`remove_source` -- turning the backend on or off
must not rebuild the engine the page's socket is attached to.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from raven.memory_engine.skill_forge.fusion import rrf_merge_weighted
from raven.memory_engine.skill_forge.types import ForgeSkillSource, RouterHit

logger = logging.getLogger(__name__)


class SkillForgeRouter:
    """Compose N :class:`ForgeSkillSource` outputs into one top-K ranking."""

    def __init__(
        self,
        sources: list[ForgeSkillSource],
        *,
        over_fetch_factor: int = 2,
        dedup_by: str = "name",
        rrf_k: int | None = None,
    ) -> None:
        # The list is captured by reference; callers should pass an
        # already-frozen tuple if they want to forbid mutation. We
        # deliberately don't freeze for them — the host wires sources at
        # boot and a live memory switch is the one later writer, so
        # verbose immutable wrappers add nothing.
        self._sources = sources
        self._over_fetch_factor = max(1, over_fetch_factor)
        self._dedup_by = dedup_by
        self._rrf_k = rrf_k

    def add_source(self, source: ForgeSkillSource) -> None:
        """Wire one more source, for a switch that turns memory on.

        The memory backend's source is absent when the router was built with
        no backend, so there is nothing to update in place; recall has to
        start feeding or the newly selected backend never reaches skills.
        A rebuilt list rather than ``append``: the constructor accepts any
        sequence, and a caller that passed a tuple asked for a list it cannot
        mutate.
        """
        self._sources = [*self._sources, source]

    def remove_source(self, source: ForgeSkillSource) -> None:
        """Drop one source by identity, for a switch that turns memory off.

        Identity rather than equality: two backend sources can carry the same
        name and weight, and the one this router holds is the one to drop.
        """
        self._sources = [s for s in self._sources if s is not source]

    async def select(
        self,
        query: str,
        history: list[dict[str, Any]],
        k: int = 5,
    ) -> list[RouterHit]:
        """Fan out to every source concurrently, fuse to top-K."""
        per_source_k = k * self._over_fetch_factor
        # One snapshot for both sides: a live switch may add or remove the
        # memory source while the gather is out, and zipping a second read of
        # ``self._sources`` would pair one source's hits with another's name
        # and weight.
        sources = tuple(self._sources)
        per_source = await asyncio.gather(*[self._safe_search(s, query, history, per_source_k) for s in sources])
        return rrf_merge_weighted(
            [(s.name, s.weight, hits) for s, hits in zip(sources, per_source)],
            k=k,
            dedup_by=self._dedup_by,
            rrf_k=self._rrf_k,
        )

    async def _safe_search(
        self,
        source: ForgeSkillSource,
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
