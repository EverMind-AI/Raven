"""Claude Code skill source: the user's own skills under ``~/.claude/skills``.

Only the direct children of that directory count. Skills that arrive through
Claude Code's plugin cache are the plugin's to install and update, and a copy
taken from the cache would go stale the moment the plugin did; the user's own
directory is the one place a skill lives because they put it there.

Every skill found is ``LOCAL_UNKNOWN``: Claude Code ships no manifest that
would let a factory copy be told from an edited one, so nothing is filtered
as pristine and the installer copies them all.
"""

from __future__ import annotations

from pathlib import Path

from raven.importer.skills import DiscoveredSkill, SkillOrigin
from raven.importer.types import Platform
from raven.utils.text import parse_frontmatter


class ClaudeCodeSkillSource:
    platform = Platform.CLAUDE_CODE

    def __init__(self, claude_dir: Path | None = None) -> None:
        self._root = (claude_dir or Path.home() / ".claude") / "skills"

    async def discover(self) -> list[DiscoveredSkill]:
        if not self._root.is_dir():
            return []
        out: list[DiscoveredSkill] = []
        for directory in sorted(self._root.iterdir()):
            skill_md = directory / "SKILL.md"
            if not directory.is_dir() or not skill_md.is_file():
                continue
            name = _frontmatter_name(skill_md)
            out.append(
                DiscoveredSkill(
                    name=directory.name,
                    path=directory,
                    origin=SkillOrigin.LOCAL_UNKNOWN,
                    registry_name=name or directory.name,
                )
            )
        return out


def _frontmatter_name(skill_md: Path) -> str:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    frontmatter, _ = parse_frontmatter(text)
    name = frontmatter.get("name") if isinstance(frontmatter, dict) else None
    return str(name).strip() if name else ""


__all__ = ["ClaudeCodeSkillSource"]
