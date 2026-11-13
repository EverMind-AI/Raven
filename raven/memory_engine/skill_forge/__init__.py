"""SkillForgeRouter machinery — multi-source skill retrieval + RRF fusion.

The package holds :class:`LocalSkillCatalog` (the local pool and its
rendering), the three sources :class:`LocalSkillSource`,
:class:`BackendSkillSource` and :class:`HubSkillSource`, the
:class:`SkillForgeRouter` that fans out over them with weighted RRF
(``rrf_merge_weighted``). Its consumers are the context engine's scent
menu and the ``find_skill`` tool.

The :class:`ForgeSkillSource` Protocol is **host-internal**: the sources are
hardcoded, not a plugin contribution point. Third-party extension of skill
retrieval happens through :class:`MemoryBackend`
(``backend.recall(agent_id=...)``) — the BackendSkillSource re-emits those
hits as :class:`RouterHit` records.
"""

from __future__ import annotations

from raven.memory_engine.skill_forge.backend_source import BackendSkillSource
from raven.memory_engine.skill_forge.catalog import LocalSkillCatalog
from raven.memory_engine.skill_forge.fusion import RRF_K, rrf_merge_weighted
from raven.memory_engine.skill_forge.hub_source import HubSkillSource
from raven.memory_engine.skill_forge.local_source import LocalSkillSource
from raven.memory_engine.skill_forge.router import SkillForgeRouter
from raven.memory_engine.skill_forge.types import ForgeSkillSource, RouterHit

__all__ = [
    "BackendSkillSource",
    "HubSkillSource",
    "LocalSkillCatalog",
    "LocalSkillSource",
    "RRF_K",
    "RouterHit",
    "SkillForgeRouter",
    "ForgeSkillSource",
    "rrf_merge_weighted",
]
