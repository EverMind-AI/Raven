"""SkillForge primitives — local-pool storage, retrieval, and feedback types.

Phase B deletions: ``Retrieval`` (local PyTorch matmul) + ``Reranker`` +
``SqliteStore`` + ``SqliteSkillRegistry`` + sync + ``skill_library/``
offline tooling were removed. Mass-library retrieval is now the remote
:class:`MassSkillSource` HTTP client under
:mod:`raven.memory_engine.skill_forge`. The ``SkillService``
aggregate + its ``select`` / LLM-gate / query-rewriter retrieval path
were retired into :class:`LocalSkillCatalog` (under ``skill_forge``)
once :class:`SkillForgeRouter` became the live retrieval path. The no-op
``SkillEvolver`` seam was removed once skill feedback moved to the
:class:`MemoryBackend` plugin (``backend.feedback`` / ``backend.store``).

What remains here is the LOCAL-pool primitive layer:

- :class:`SkillRegistry` — workspace + builtin SKILL.md scanner
- :class:`LocalPool` — BM25 over the registry
- shared dataclasses (:class:`SkillMeta`, :class:`ScoredSkill`)
"""

from raven.memory_engine.skill_local.local_pool import LocalPool
from raven.memory_engine.skill_local.registry import SkillRegistry
from raven.memory_engine.skill_local.types import ScoredSkill, SkillMeta

__all__ = [
    # Data layer
    "SkillRegistry",
    "LocalPool",
    # Shared types
    "SkillMeta",
    "ScoredSkill",
]
