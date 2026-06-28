"""HubSkillSource — SkillForgeRouter source over the remote Skill Hub.

Discovery layer (Tier 0): turns ``GET /openapi/v1/skills?q=`` catalog
metadata into :class:`RouterHit` candidates for RRF fusion. The body
(``skill_md``) is NOT fetched here — that's the
:class:`SkillsSegmentBuilder`'s pre-gate body-hydrate step which calls
``SkillHubClient.get(id)`` in parallel across all Hub candidates so the
LLM gate sees real body excerpts when deciding what to inject.

Bundled file download (zip → extract → resolved refs) happens in the
post-gate hydrate, only for the 0-2 hits the gate actually selects — so
catalog calls are O(K) but downloads are O(selected).

Failures are swallowed into an empty list by the router's
``_safe_search``, so a Hub outage never poisons the whole assembly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from raven.memory_engine.skill_forge.types import RouterHit

if TYPE_CHECKING:
    from raven.skill_hub import SkillHubClient

logger = logging.getLogger(__name__)


class HubSkillSource:
    """SkillSource backed by the remote Skill Hub OpenAPI."""

    name: str = "hub"
    weight: float = 0.85

    def __init__(
        self,
        client: "SkillHubClient",
        *,
        weight: float = 0.85,
        min_safety: float = 0.7,
    ) -> None:
        self._client = client
        self.weight = weight
        self._min_safety = min_safety

    async def search(
        self,
        query: str,
        history: list[dict[str, Any]],
        k: int,
    ) -> list[RouterHit]:
        del history  # Hub search takes only the query + limit.
        items = await self._client.search(query, limit=max(1, k))
        hits: list[RouterHit] = []
        for it in items:
            # Field names follow the Hub OpenAPI catalog payload: ``id``
            # (UUID), ``skill_id`` (readable path), ``quality_score``,
            # ``tags``. The UUID is the stable, slash-free native id used
            # in the qualified id; the readable ``skill_id`` rides in meta.
            sid = it.get("id")
            name = it.get("name")
            if not sid or not name:
                logger.warning("hub hit missing id/name; skipping: %r", it)
                continue
            # The catalog payload omits per-skill safety (it lives in the
            # skill *detail* response only), so this guard no-ops on the
            # standard search path and only bites when a deployment
            # includes a catalog-level ``score_safety``.
            safety = it.get("score_safety")
            if safety is not None and float(safety) < self._min_safety:
                continue
            hits.append(RouterHit(
                qualified_id=f"hub/{sid}",
                name=name,
                content="",  # Tier 0: metadata only; body via pre-gate hydrate
                score=float(it.get("quality_score") or 0.0),
                meta={
                    "source": "hub",
                    "id": sid,
                    "skill_id": it.get("skill_id"),
                    "description": it.get("description"),
                    "tags": it.get("tags"),
                    "category": it.get("category"),
                    "quality_score": it.get("quality_score"),
                    "install_count": it.get("install_count"),
                    "score_safety": safety,
                },
            ))
        return hits


__all__ = ["HubSkillSource"]
