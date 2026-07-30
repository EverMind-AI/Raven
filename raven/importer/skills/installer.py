"""Filter, copy and account for imported skills.

Only ``bundled_pristine`` is skipped. The two error costs are asymmetric:
importing too much leaves a few unused entries in a pool that injects at most
two skills per turn by relevance, and every one of them sits under a single
directory the user can delete; importing too little is silent, and the user
has no way to learn what was left behind.

The category level Hermes uses for browsing is dropped and a single
``hermes/`` level interposed, because Raven's registry reads the first
directory level below a layer root as the skill's source label -- mounting
the tree as-is would mint one bogus source per category.

``SkillImportSummary`` keeps ``pristine`` separate from ``skipped`` so the
counts add up to ``total`` and so the CLI can tell the user which of the two
happened: factory content left alone is the normal case and says nothing is
wrong, while a per-instance skip means a name was taken or the skill had
already been imported.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from raven.importer.skills import DiscoveredSkill, SkillOrigin, SkillSource
from raven.importer.state import ImportState

_SKIP_ORIGINS = frozenset({SkillOrigin.BUNDLED_PRISTINE})

# Hermes' skill root is always literally named "skills" (<hermes_home>/skills),
# so a discovered skill's parent being named "skills" means it sits directly
# at that root with no browsing category above it.
_SKILLS_ROOT_NAME = "skills"


@dataclass(frozen=True)
class SkillImportSummary:
    total: int = 0
    installed: int = 0
    pristine: int = 0
    skipped: int = 0
    failed: int = 0


async def install_skills(
    source: SkillSource,
    workspace: Path,
    state: ImportState,
) -> SkillImportSummary:
    discovered = await source.discover()
    wanted = [s for s in discovered if s.origin not in _SKIP_ORIGINS]
    dest_root = workspace / "skills" / "hermes"

    installed = skipped = failed = 0
    pristine = len(discovered) - len(wanted)
    claimed: set[str] = set()
    for skill in wanted:
        target = _target_for(skill, dest_root, claimed)
        if target is None:
            skipped += 1
            continue
        # Keyed on the resolved target name, not skill.name: two skills that
        # flatten to the same display name land at different targets and
        # must not share one idempotency key.
        key = f"skill-{target.name}"
        if state.is_submitted(source.platform, key):
            skipped += 1
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill.path, target)
        except OSError as exc:
            # copytree fails partway through, and by then the target usually
            # holds SKILL.md already: the pool would serve a skill whose
            # attachments are missing, and the next run would read the leftover
            # as "already present" and never retry it. Removing it restores both
            # the retry (a failed entry is not `submitted`) and the invariant
            # that a directory in the pool is a complete skill.
            shutil.rmtree(target, ignore_errors=True)
            logger.warning("skill import failed for {}: {}", skill.name, exc)
            state.mark_failed(source.platform, key, str(exc))
            failed += 1
            continue
        state.mark_submitted(source.platform, key)
        installed += 1

    logger.info(
        "hermes skills: {} discovered, {} pristine, {} installed, {} skipped, {} failed",
        len(discovered),
        pristine,
        installed,
        skipped,
        failed,
    )
    return SkillImportSummary(
        total=len(discovered),
        installed=installed,
        pristine=pristine,
        skipped=skipped,
        failed=failed,
    )


def _target_for(skill: DiscoveredSkill, dest_root: Path, claimed: set[str]) -> Path | None:
    """Decide the on-disk landing spot, or ``None`` to skip.

    Two situations look alike but must not be handled the same way: two
    Hermes skills flattening to the same name *within this run* is a
    collision to disambiguate, while a target that already exists on disk --
    from a previous import or the user's own work -- must never be
    overwritten or duplicated under another name. The deciding question is
    therefore whether *this run* already claimed the name, not whether the
    path exists.
    """
    name = skill.name
    if name not in claimed:
        plain = dest_root / name
        if plain.exists():
            logger.info("skill {} already present at {}; skipping", name, plain)
            return None
        claimed.add(name)
        return plain

    # Depth varies -- a skill can sit directly at Hermes' skills root with no
    # browsing category above it -- and a made-up category word would mean
    # nothing in a directory the user browses, so fall back to a numeric suffix.
    category = skill.path.parent.name
    candidate = f"{name}-2" if category == _SKILLS_ROOT_NAME else f"{category}-{name}"
    if candidate in claimed or (dest_root / candidate).exists():
        logger.warning("skill name {} collided more than once; skipping duplicate at {}", name, skill.path)
        return None
    claimed.add(candidate)
    logger.info("skill name {} taken this run; landing as {}", name, candidate)
    return dest_root / candidate


__all__ = ["SkillImportSummary", "install_skills"]
