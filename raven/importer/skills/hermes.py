"""Hermes skill provenance.

Evidence, in priority order: ``skills/.bundled_manifest`` (name:md5 of the
bundled package as of the last sync), ``skills/.hub/lock.json``, then
``skills/.usage.json``. Provenance has to come from recorded evidence, not
from guessing at names -- a skill named like a factory one can be entirely
user-written, and vice versa.

Manifest keys are written from the frontmatter ``name`` with the directory
name as fallback, so both are tried in that order. Measured against a real
install all 70 manifest entries matched by frontmatter name, the directory
name fallback never fired, and 12 of the 82 skills on disk are not in the
manifest at all. Two of those 82 do carry a frontmatter name that differs
from their directory name, so a single-key lookup would misread them.

When the manifest is absent nothing can be proven bundled, so everything is
reported as LOCAL_UNKNOWN and therefore imported. If this hash ever diverges
from upstream's, every bundled skill reads as modified and is imported: the
failure direction is redundancy, never data loss.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from raven.importer.skills import DiscoveredSkill, SkillOrigin, package_hash
from raven.importer.types import Platform
from raven.utils.text import parse_frontmatter

_MANIFEST_FILENAME = ".bundled_manifest"
_HUB_LOCK_RELPATH = ".hub/lock.json"
_USAGE_FILENAME = ".usage.json"

# Mirrors Hermes' EXCLUDED_SKILL_DIRS and SKILL_SUPPORT_DIRS (agent/skill_utils.py).
# Skipping only .hub would import two things the user never had as skills: packages
# the curator retired into .archive, and the documentation copies of old skills that
# archive and curator workflows preserve under a live skill's references/.
_EXCLUDED_DIRS = frozenset(
    (
        ".git",
        ".github",
        ".hub",
        ".archive",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "__pycache__",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    )
)
_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))


class HermesSkillSource:
    platform = Platform.HERMES

    def __init__(self, hermes_home: Path | None = None) -> None:
        from raven.importer.scanners.hermes import resolve_hermes_home

        self._home = hermes_home if hermes_home is not None else resolve_hermes_home()

    async def discover(self) -> list[DiscoveredSkill]:
        root = self._home / "skills"
        if not root.is_dir():
            return []
        manifest = _read_manifest(root)
        if not manifest:
            logger.warning(
                "hermes {} missing or empty; every skill will be imported",
                _MANIFEST_FILENAME,
            )
        hub = _read_hub_names(root)
        usage = _read_usage(root)

        out: list[DiscoveredSkill] = []
        for skill_md in sorted(root.rglob("SKILL.md")):
            if not _is_discoverable(skill_md, root):
                continue
            directory = skill_md.parent
            frontmatter_name = _frontmatter_name(skill_md)
            out.append(
                DiscoveredSkill(
                    name=directory.name,
                    path=directory,
                    origin=_classify(directory, manifest, hub, usage, frontmatter_name=frontmatter_name),
                    registry_name=frontmatter_name or directory.name,
                )
            )
        return out


def _is_discoverable(skill_md: Path, root: Path) -> bool:
    parts = skill_md.relative_to(root).parts
    if any(part in _EXCLUDED_DIRS for part in parts):
        return False
    for idx, part in enumerate(parts[:-1]):
        # A support dir is only support when it sits inside a skill package, so
        # a category legitimately named scripts/ keeps its skills discoverable.
        if idx and part in _SUPPORT_DIRS and (root / Path(*parts[:idx]) / "SKILL.md").exists():
            return False
    return True


def _classify(
    directory: Path,
    manifest: dict[str, str],
    hub: set[str],
    usage: dict[str, Any],
    *,
    frontmatter_name: str,
) -> SkillOrigin:
    for key in (frontmatter_name, directory.name):
        if key and key in manifest:
            origin_hash = manifest[key]
            if origin_hash and origin_hash == package_hash(directory):
                return SkillOrigin.BUNDLED_PRISTINE
            # A v1 manifest carries no hash: bundled is proven, pristine is not.
            return SkillOrigin.BUNDLED_MODIFIED
    # Hermes' hub lock really is keyed by directory name, unlike the manifest
    # and usage records above -- verified against a real install where the
    # lock key ("segment-anything") differs from the frontmatter name
    # ("segment-anything-model").
    if directory.name in hub:
        return SkillOrigin.HUB_INSTALLED
    if _is_curator_managed(usage.get(frontmatter_name or directory.name)):
        return SkillOrigin.CURATOR_MANAGED
    return SkillOrigin.LOCAL_UNKNOWN


def _is_curator_managed(record: Any) -> bool:
    """Mirrors Hermes' ``_is_curator_managed_record`` (tools/skill_usage.py).

    The on-disk field is ``created_by: "agent"``, which reads like provenance
    but upstream consumes as a curator-management opt-in -- ``hermes curator
    adopt`` stamps the same marker on a skill the user wrote themselves. Both
    that field and the older ``agent_created`` flag count, and neither proves
    authorship; only that the skill is not factory content.
    """
    if not isinstance(record, dict):
        return False
    return record.get("created_by") == "agent" or record.get("agent_created") is True


def _frontmatter_name(skill_md: Path) -> str:
    try:
        front, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return str(front.get("name") or "").strip()


def _read_manifest(root: Path) -> dict[str, str]:
    try:
        raw = (root / _MANIFEST_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, digest = line.partition(":")
        out[name.strip()] = digest.strip()
    return out


def _read_hub_names(root: Path) -> set[str]:
    try:
        data = json.loads((root / _HUB_LOCK_RELPATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    installed = data.get("installed")
    return set(installed) if isinstance(installed, dict) else set()


def _read_usage(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / _USAGE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


__all__ = ["HermesSkillSource"]
