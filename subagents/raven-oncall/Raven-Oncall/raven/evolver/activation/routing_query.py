"""dry_query — offline probe of the real skill discovery + selection path.

Round-4 forensics found that a custom skill authored on disk under
``skill_library/tb2_gap_fill/`` was never injected: the benchmark agent
constructs ``SkillForgeConfig(enabled=False)`` with empty ``local_dirs``, so
the directory is never mounted as a discovery layer and ``select()`` returns
``[]`` before any retrieval runs. ``dry_query`` answers, without an LLM or the
SR server, "which skill names would routing inject for this task?" by building
a real :class:`LocalSkillCatalog` + :class:`SkillForgeRouter` whose
``local_dirs`` point at ``library_root`` and running the actual BM25 retrieval
+ resolve path.

No LLM is involved: the LLM gate and query rewriter are disabled, so selection
reduces to filesystem discovery + lexical (BM25) scoring — the deterministic
core of the real path that the benchmark must wire up.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

__all__ = ["dry_query"]


def dry_query(task_text: str, *, library_root: Path | None = None) -> list[str]:
    """Return the skill names routing would inject for ``task_text``.

    Args:
        task_text: The task description fed to skill selection.
        library_root: Root dir mounted as an extra discovery layer (the
            recursive ``SKILL.md`` scan walks its subtree, so
            ``.../skill_library`` surfaces ``tb2_gap_fill/<skill>/SKILL.md``).
            ``None`` exercises the default layers only (workspace + builtin).

    Returns:
        Flat list of skill names (``SkillMeta.name``) routing would inject,
        in injection order. This mirrors the benchmark's two injection
        surfaces (ContextBuilder.build_system_prompt): the ``always: true``
        skills rendered under "Active Skills", followed by the retrieval
        ``select()`` hits, deduped by name.
    """
    from raven.config.raven import LocalDirConfig, SkillForgeConfig

    # Raven split the old unified SkillService into a discovery catalog
    # (always-skills + registry/pool) and a retrieval router over sources.
    from raven.memory_engine.skill_forge.catalog import LocalSkillCatalog
    from raven.memory_engine.skill_forge.local_source import LocalSkillSource
    from raven.memory_engine.skill_forge.router import SkillForgeRouter

    local_dirs: list[LocalDirConfig] = []
    if library_root is not None:
        local_dirs.append(LocalDirConfig(path=str(Path(library_root)), name="tb2_gap_fill"))

    config = SkillForgeConfig(
        enabled=True,
        local_dirs=local_dirs,
        llm_gate_enabled=False,
        rewrite_enabled=False,
        reranker_enabled=False,
        disable_always=False,
    )

    with tempfile.TemporaryDirectory() as ws:
        catalog = LocalSkillCatalog(
            Path(ws),
            config=config,
            llm_provider=None,
            start_watcher=False,
        )
        always = catalog.get_always_skills()
        router = SkillForgeRouter([LocalSkillSource(catalog.pool, catalog.registry)])
        selected = asyncio.run(router.select(task_text, []))

    names: list[str] = []
    seen: set[str] = set()
    for meta in [*always, *selected]:
        if meta.name in seen:
            continue
        seen.add(meta.name)
        names.append(meta.name)
    return names
