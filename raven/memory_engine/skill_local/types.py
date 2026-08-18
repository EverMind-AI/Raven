"""Shared dataclasses for SkillForge."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class SkillMeta:
    """Metadata for a skill — body is loaded separately via SkillRegistry.get_body()."""

    id: str
    """Unique skill identifier."""

    name: str
    """Skill name (directory name)."""

    description: str
    """One-line description shown to the LLM."""

    path: Path
    """Absolute path to SKILL.md."""

    content: str
    """SKILL.md body content (excluding name and description)."""

    source: str
    """Physical origin: ``workspace`` / ``builtin`` / ``everos`` / ``mirror/*`` etc."""

    always: bool = False
    """Whether to force-inject into the system prompt every turn."""

    requires: dict = field(default_factory=dict)
    """Dependency declarations: ``{"bins": [...], "env": [...]}``."""

    # ---- Later fields (filled by ingest, currently None / empty) ----

    scope: str | None = None
    """Owning pool: personal / team / official / community / mass."""

    license: str | None = None
    """SPDX license (e.g. MIT / Apache-2.0)."""

    imported_at: datetime | None = None
    """Time the skill was pulled in from an external source."""

    raw_frontmatter: dict = field(default_factory=dict)
    """Full original frontmatter, kept for downstream consumers."""


@dataclass
class ScoredSkill:
    """A retrieval hit (name + score + source)."""

    name: str
    """Skill name."""

    score: float
    """Relevance score; higher means more relevant."""

    source: str = ""
    """Physical origin, aligned with SkillMeta.source. Empty string kept for backward compatibility."""
