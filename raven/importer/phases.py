"""Post-import phases: mirror the profile and install the skills, per platform.

EverOS storage alone does not reach every consumer. Curator, Personalizer and
the Sentinel producers read ``user_memory/profile/user.md`` directly, and the
skill pool is a directory tree, so both are landed by the importer itself once
the message pass is over. Each platform contributes what it has: Hermes its
``USER.md`` entries and its skills tree, Claude Code the memory files it marks
as being about the user (``metadata.type`` of ``user`` or ``feedback``) and
the skills under ``~/.claude/skills``.

Both phases are additive and run after the message pass, so a failure in
either is reported without reversing an import that has already landed. The
caller decides whether they run at all: a cancelled run must stop, not hand
over its two longest steps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from loguru import logger

from raven.importer.skills import SkillSource
from raven.importer.skills.claude_code import ClaudeCodeSkillSource
from raven.importer.skills.hermes import HermesSkillSource
from raven.importer.skills.installer import SkillImportSummary, install_skills
from raven.importer.types import Platform, Scanner, ScanResult, SourceKind
from raven.utils.text import parse_frontmatter

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider
    from raven.importer.hermes_user_md import ImportedSections
    from raven.importer.state import ImportState

PhaseKind = Literal["profile", "skills"]
OnPhase = Callable[[PhaseKind, int, int], None]

# The memory kinds Claude Code itself files as being about the user rather
# than about a project: who they are, and how they want to be worked with.
_PROFILE_MEMORY_TYPES = frozenset({"user", "feedback"})

_SKILL_SOURCES: dict[Platform, Callable[[], SkillSource]] = {
    Platform.HERMES: HermesSkillSource,
    Platform.CLAUDE_CODE: ClaudeCodeSkillSource,
}


@dataclass(frozen=True)
class PhaseOutcome:
    """What the two phases produced, with each failure kept beside its result."""

    profile: ImportedSections | None = None
    profile_error: str = ""
    skills: SkillImportSummary | None = None
    skill_error: str = ""
    # A stop arrived while the phases ran; whatever landed before it stays.
    cancelled: bool = False


def profile_entries(items: Sequence[tuple[Scanner, ScanResult]]) -> list[str]:
    """Every profile fact the run's sources carry, in scan order, each once.

    Deduplicated on the text itself: a memory library reached through several
    project directories (git worktrees symlink theirs to one another) yields
    the same files several times, and each duplicate would otherwise cost a
    classification call before the mirror noticed it was already on file.
    """
    seen: set[str] = set()
    out: list[str] = []
    for _scanner, result in items:
        for entry in _entries_of(result):
            stripped = entry.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                out.append(stripped)
    return out


def _entries_of(result: ScanResult) -> list[str]:
    if result.kind is not SourceKind.MEMORY_FILE:
        return []
    if result.platform is Platform.HERMES:
        if result.source_key != "user-md":
            return []
        from raven.importer.scanners.hermes import split_memory_entries

        (path,) = result.file_paths
        return split_memory_entries(_read(path))
    if result.platform is Platform.CLAUDE_CODE:
        entries: list[str] = []
        for path in result.file_paths:
            frontmatter, body = parse_frontmatter(_read(path))
            meta = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}
            if meta.get("type") in _PROFILE_MEMORY_TYPES and body.strip():
                entries.append(body)
        return entries
    return []


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("profile mirror: cannot read {}: {}", path, exc)
        return ""


def skill_source_for(platform: Platform) -> SkillSource | None:
    """The platform's skill source, or None for a platform that has no skills to offer."""
    make = _SKILL_SOURCES.get(platform)
    return make() if make is not None else None


def skill_sources(platforms: Iterable[Platform]) -> list[SkillSource]:
    """One skill source per platform in scope that has skills to offer.

    Skills are directories, not message sources, so they never travel as a
    ScanResult; the scope is the platforms the run was asked for, so a
    platform whose only importable data is skills still gets them installed.
    """
    wanted = set(platforms)
    sources: list[SkillSource] = []
    for platform in _SKILL_SOURCES:
        source = skill_source_for(platform) if platform in wanted else None
        if source is not None:
            sources.append(source)
    return sources


async def run_phases(
    items: Sequence[tuple[Scanner, ScanResult]],
    workspace: Path,
    state: ImportState,
    *,
    provider: LLMProvider | None,
    model: str,
    platforms: Iterable[Platform] | None = None,
    on_phase: OnPhase | None = None,
    cancel_path: Path | None = None,
) -> PhaseOutcome:
    """Mirror the profile, then install the skills, for the run's platforms.

    ``platforms`` is the run's scope; left out, it is the platforms the items
    came from. ``cancel_path`` is the stop file the message pass polls, polled
    here too between profile entries and between skills, because a stop is
    offered while the phases run. ``on_phase(kind, done, total)`` reports each
    phase's own progress: one classification call per entry and one tree copy
    per skill, neither of which can otherwise be told from a hang.

    How the phases ended is written to ``state`` -- pending while they run,
    then done, failed or cancelled -- so a reader of the file can tell a run
    whose phases finished from one the gateway lost halfway through them.
    """
    scope = set(platforms) if platforms is not None else {result.platform for _scanner, result in items}

    def cancelled() -> bool:
        return cancel_path is not None and cancel_path.exists()

    state.set_phases("pending")
    profile: ImportedSections | None = None
    profile_error = ""
    entries = profile_entries(items)
    if entries and not cancelled():
        from raven.importer.hermes_user_md import import_user_md_sections
        from raven.memory_engine import MemoryStore

        try:
            profile = await import_user_md_sections(
                entries,
                MemoryStore(workspace),
                provider=provider,
                model=model,
                on_progress=(lambda done, total: on_phase("profile", done, total)) if on_phase else None,
                cancelled=cancelled,
            )
            logger.info("profile mirror: {} entries landed", len(profile.written))
        except Exception as exc:
            logger.warning("profile mirror failed: {}", exc)
            profile_error = str(exc)

    summaries: list[SkillImportSummary] = []
    skill_errors: list[str] = []
    for source in skill_sources(scope):
        if cancelled():
            break
        try:
            summaries.append(
                await install_skills(
                    source,
                    workspace,
                    state,
                    on_progress=(lambda done, total: on_phase("skills", done, total)) if on_phase else None,
                    cancelled=cancelled,
                )
            )
        except Exception as exc:
            logger.warning("{} skill import failed: {}", source.platform.value, exc)
            skill_errors.append(f"{source.platform.value}: {exc}")

    stopped = cancelled()
    errors = ([f"profile: {profile_error}"] if profile_error else []) + skill_errors
    state.set_phases("cancelled" if stopped else "failed" if errors else "done", errors)
    return PhaseOutcome(
        profile=profile,
        profile_error=profile_error,
        skills=_total(summaries) if summaries else None,
        skill_error="; ".join(skill_errors),
        cancelled=stopped,
    )


def _total(summaries: Sequence[SkillImportSummary]) -> SkillImportSummary:
    return SkillImportSummary(
        total=sum(s.total for s in summaries),
        installed=sum(s.installed for s in summaries),
        pristine=sum(s.pristine for s in summaries),
        skipped=sum(s.skipped for s in summaries),
        failed=sum(s.failed for s in summaries),
    )


__all__ = [
    "OnPhase",
    "PhaseKind",
    "PhaseOutcome",
    "profile_entries",
    "run_phases",
    "skill_source_for",
    "skill_sources",
]
