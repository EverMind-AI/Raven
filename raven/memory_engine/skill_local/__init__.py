"""The local skill pool: SKILL.md discovery, BM25 retrieval over it, and the shared skill types.

- :class:`SkillRegistry` — workspace + builtin SKILL.md scanner
- :class:`LocalPool` — BM25 over the registry
- :class:`SkillMeta`, :class:`ScoredSkill` — the dataclasses the retrieval
  paths under ``skill_forge`` and the skill tools share
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
