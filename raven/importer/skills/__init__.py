"""Skill import -- a landing path the message pipeline cannot express.

A skill is a directory (SKILL.md plus bundled references/, scripts/,
templates/, assets/), and EverOS' agent-skill rows carry only content, name,
id and confidence, so routing one through ``backend.store`` drops every
attachment. Raven's local skill pool is already directory-shaped and already
keys the first directory level below a layer root as the skill's source label,
so the formats match with no conversion.

Origin classification is per-platform because every tool records provenance
differently; only the step after it -- filter, copy, account -- is shared.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from raven.importer.types import Platform


class SkillOrigin(StrEnum):
    BUNDLED_PRISTINE = "bundled_pristine"
    BUNDLED_MODIFIED = "bundled_modified"
    HUB_INSTALLED = "hub_installed"
    # Not "agent authored it". The marker behind this is a management opt-in that
    # a user also sets by hand, so the only thing it proves is "not factory".
    CURATOR_MANAGED = "curator_managed"
    LOCAL_UNKNOWN = "local_unknown"


@dataclass(frozen=True)
class DiscoveredSkill:
    name: str
    path: Path
    origin: SkillOrigin
    # SkillRegistry keys a skill as (source, registry_name), not (source,
    # directory name) -- registry_name is the frontmatter ``name`` with the
    # directory name as fallback, mirroring registry.py's own display-name
    # rule. The installer needs this to detect a collision the on-disk
    # directory name alone would miss.
    registry_name: str


@runtime_checkable
class SkillSource(Protocol):
    platform: Platform

    async def discover(self) -> list[DiscoveredSkill]:
        """Classify every skill the platform holds -- no filtering."""
        ...


def package_hash(directory: Path) -> str:
    """MD5 over the whole package, mirroring Hermes' own ``_dir_hash``.

    Hashing only SKILL.md would call a skill whose references/ the user edited
    pristine. Hermes is not a dependency, so its function cannot be imported
    and is reproduced exactly: sorted rglob, relative path then bytes, and
    OSError swallowed so one unreadable file cannot abort a whole scan.
    """
    hasher = hashlib.md5(usedforsecurity=False)
    try:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                hasher.update(str(path.relative_to(directory)).encode("utf-8"))
                hasher.update(path.read_bytes())
    except OSError:
        pass
    return hasher.hexdigest()


__all__ = ["DiscoveredSkill", "SkillOrigin", "SkillSource", "package_hash"]
