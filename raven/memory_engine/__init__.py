"""Memory subsystems for the agent host, entered through this face.

The shapes a memory backend implements (``Memory``, ``MemoryBackend``) and the
assembled-context carriers (``AssembledContext``, ``TokenBudget``) are papers
in :mod:`raven.contracts`; this package holds the machinery around them:

- ``consolidate/``      -- ``MemoryStore`` (the ``user.md`` profile and the
  ``episodes.md`` episode log, with ``attention.md`` and ``behaviors.md``
  beside them, all under a portable file lock), ``MemoryConsolidator``
  (token-driven consolidation), the attention and behaviors parsers and the
  behaviors extractor.
- ``skills/``, ``skill_local/``, ``skill_forge/`` -- the local skill pool, its
  watcher and catalog, and the forge that routes and evolves skills.
- ``store_pipeline.py`` -- the write path from a finished turn into the store.
- ``contract_test.py``  -- the base test class a backend author inherits to
  prove the backend satisfies the host's expectations.

Everything another package needs is a name on this module: ``from
raven.memory_engine import MemoryStore``. The names resolve lazily (PEP 562) so
importing the face costs nothing until a name is used, and so the contract-test
classes (which import ``pytest``) never load outside the test suite. The layout
below the face is the engine's own to rearrange.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.memory_engine.consolidate.attention import (
        DAILY_FIRE_PLAN_HEADER,
        parse_attention,
        upsert_section,
    )
    from raven.memory_engine.consolidate.behaviors import (
        parse_behaviors,
        render_folded_block,
        slice_after_day,
    )
    from raven.memory_engine.consolidate.behaviors_extractor import (
        BehaviorsExtractor,
        BehaviorsOffsets,
    )
    from raven.memory_engine.consolidate.consolidator import (
        MemoryConsolidator,
        MemoryStore,
        parse_episode_line,
        parse_user_md_sections,
    )
    from raven.memory_engine.contract_test import (
        LifecycleContractTests,
        MemoryBackendContractTests,
    )
    from raven.memory_engine.skill_forge import (
        EverosSkillSource,
        HubSkillSource,
        LLMGateFilter,
        LocalSkillCatalog,
        LocalSkillSource,
        QueryRewriter,
        SkillForgeRouter,
    )
    from raven.memory_engine.skill_forge.refs import resolve_refs
    from raven.memory_engine.skill_forge.types import RouterHit
    from raven.memory_engine.skill_local.registry import (
        SkillRegistry,
        filter_by_required_tools,
    )
    from raven.memory_engine.skill_local.types import SkillMeta
    from raven.memory_engine.store_pipeline import StorePipeline

__all__ = [
    "DAILY_FIRE_PLAN_HEADER",
    "BehaviorsExtractor",
    "BehaviorsOffsets",
    "EverosSkillSource",
    "HubSkillSource",
    "LLMGateFilter",
    "LocalSkillCatalog",
    "LocalSkillSource",
    "MemoryConsolidator",
    "MemoryStore",
    "QueryRewriter",
    "RouterHit",
    "SkillForgeRouter",
    "SkillMeta",
    "SkillRegistry",
    "StorePipeline",
    "filter_by_required_tools",
    "parse_attention",
    "parse_behaviors",
    "parse_episode_line",
    "parse_user_md_sections",
    "render_folded_block",
    "resolve_refs",
    "slice_after_day",
    "upsert_section",
    "LifecycleContractTests",
    "MemoryBackendContractTests",
]

_FACE: dict[str, str] = {
    "BehaviorsExtractor": "raven.memory_engine.consolidate.behaviors_extractor",
    "BehaviorsOffsets": "raven.memory_engine.consolidate.behaviors_extractor",
    "EverosSkillSource": "raven.memory_engine.skill_forge",
    "HubSkillSource": "raven.memory_engine.skill_forge",
    "LLMGateFilter": "raven.memory_engine.skill_forge",
    "LocalSkillCatalog": "raven.memory_engine.skill_forge",
    "LocalSkillSource": "raven.memory_engine.skill_forge",
    "MemoryConsolidator": "raven.memory_engine.consolidate.consolidator",
    "MemoryStore": "raven.memory_engine.consolidate.consolidator",
    "QueryRewriter": "raven.memory_engine.skill_forge",
    "RouterHit": "raven.memory_engine.skill_forge.types",
    "SkillForgeRouter": "raven.memory_engine.skill_forge",
    "SkillMeta": "raven.memory_engine.skill_local.types",
    "SkillRegistry": "raven.memory_engine.skill_local.registry",
    "StorePipeline": "raven.memory_engine.store_pipeline",
    "filter_by_required_tools": "raven.memory_engine.skill_local.registry",
    "DAILY_FIRE_PLAN_HEADER": "raven.memory_engine.consolidate.attention",
    "parse_attention": "raven.memory_engine.consolidate.attention",
    "parse_behaviors": "raven.memory_engine.consolidate.behaviors",
    "parse_episode_line": "raven.memory_engine.consolidate.consolidator",
    "parse_user_md_sections": "raven.memory_engine.consolidate.consolidator",
    "render_folded_block": "raven.memory_engine.consolidate.behaviors",
    "resolve_refs": "raven.memory_engine.skill_forge.refs",
    "slice_after_day": "raven.memory_engine.consolidate.behaviors",
    "upsert_section": "raven.memory_engine.consolidate.attention",
    "LifecycleContractTests": "raven.memory_engine.contract_test",
    "MemoryBackendContractTests": "raven.memory_engine.contract_test",
}


def __getattr__(name: str):
    module = _FACE.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
