"""SkillForgeRouter machinery — multi-source skill retrieval + RRF fusion.

The package holds :class:`LocalSkillCatalog` (the local pool and its
rendering), the three sources :class:`LocalSkillSource`,
:class:`EverosSkillSource` and :class:`HubSkillSource`, the
:class:`SkillForgeRouter` that fans out over them with weighted RRF
(``rrf_merge_weighted``), the :class:`LLMGateFilter` and
:class:`QueryRewriter` downstream of the fusion, and ``resolve_refs``.
Its consumer is the context engine's skills segment.

The :class:`ForgeSkillSource` Protocol is **host-internal**: the sources are
hardcoded, not a plugin contribution point. Third-party extension of skill
retrieval happens through :class:`MemoryBackend`
(``backend.recall(agent_id=...)``) — the EverosSkillSource re-emits those
hits as :class:`RouterHit` records.
"""

from __future__ import annotations

from raven.memory_engine.skill_forge.catalog import LocalSkillCatalog
from raven.memory_engine.skill_forge.everos_source import EverosSkillSource
from raven.memory_engine.skill_forge.fusion import RRF_K, rrf_merge_weighted
from raven.memory_engine.skill_forge.gate import LLMGateFilter
from raven.memory_engine.skill_forge.hub_source import HubSkillSource
from raven.memory_engine.skill_forge.local_source import LocalSkillSource
from raven.memory_engine.skill_forge.refs import resolve_refs
from raven.memory_engine.skill_forge.rewriter import (
    QueryRewriter,
    RewriteResult,
)
from raven.memory_engine.skill_forge.router import SkillForgeRouter
from raven.memory_engine.skill_forge.types import ForgeSkillSource, RouterHit

__all__ = [
    "EverosSkillSource",
    "HubSkillSource",
    "LLMGateFilter",
    "LocalSkillCatalog",
    "LocalSkillSource",
    "QueryRewriter",
    "RRF_K",
    "RewriteResult",
    "RouterHit",
    "SkillForgeRouter",
    "ForgeSkillSource",
    "resolve_refs",
    "rrf_merge_weighted",
]
