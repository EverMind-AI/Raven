"""Undoing what a role wrote outside the paths it may write.

The pass that makes a boundary a boundary. Layer three of the four -- the
prompt says what a role owns, the tool gate refuses a call that would cross it,
this runs after the role has stopped and puts back what got through anyway, and
what it put back is on the record. Without it the first two are advice: a role
that shells out past the tool gate leaves no trace the gate can see.

**Two sources, and they are not interchangeable.** Given ``stage_base`` the
pass reads the stage's own commits as well as the worktree. Without it, a role
that committed its stray write has already vanished from ``git status`` and the
pass finds nothing to undo -- which quietly makes the boundary advisory for
exactly the roles that know how to use git, the ones most able to cross it.

What a grade *means* is not decided here: the caller passes a ``grade``
callable, so a project reading guard files and a playbook reading its own role
rows both get this pass without one of them having to pretend to be the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from raven.stint.git import ProjectGit
from raven.stint.ownership import APPENDS, NEVER, OWNS, Roster, matches_any

__all__ = ["EnforceReport", "enforce", "roster_grader"]


@dataclass(frozen=True)
class EnforceReport:
    """What the pass found and what it did about it."""

    trimmed: tuple[str, ...] = ()
    """Append-only paths the role removed lines from, put back as the stage found them."""

    stray: tuple[str, ...] = ()
    """Paths the role may not write at all, all of which were undone."""

    quarantined: tuple[str, ...] = ()
    """The subset of ``stray`` whose content was copied aside before it went.

    Not always all of it. A stray write the role *committed* is undone by
    putting the stage's base back, which removes the file before there is
    anything left to copy -- there the reverted commit is the copy, and it is
    a better one, because it also records what else that commit touched.
    """

    reverted: bool = False
    """Whether undoing reached into the stage's own commits, not just the worktree."""

    violations: tuple[str, ...] = field(default_factory=tuple)
    """One sentence per finding, for the round's record and the next round's prompt.

    In a fixed order a caller may index by: one per entry of ``trimmed``, in
    that order, and then a single closing sentence for ``quarantined`` as a
    set. A caller that wants to say something different about the two kinds
    reads them apart that way rather than by matching on the text.
    """

    @property
    def clean(self) -> bool:
        return not self.trimmed and not self.stray


def roster_grader(roster: Roster, role: str, round_index: int = 0) -> Callable[[str], str]:
    """The grading half of :func:`enforce`, read off a roster."""

    def grade(path: str) -> str:
        return roster.grade(role, path, round_index=round_index)

    return grade


def enforce(
    git: ProjectGit,
    *,
    role: str,
    grade: Callable[[str], str],
    quarantine: Path,
    stage_base: str = "",
    allowed: Iterable[str] = (),
    artifacts: Sequence[str] = (),
    commit_revert: Callable[[], None] | None = None,
) -> EnforceReport:
    """Put back everything ``role`` wrote that it had no claim to.

    Args:
        git: The project's repository, positioned as the role left it.
        role: Whose turn just ended, for the notes this returns.
        grade: One path to ``owns`` / ``appends`` / ``never``; see
            :func:`roster_grader` for the usual one.
        quarantine: Where an undone file's copy is kept. Undoing without
            keeping a copy destroys the one piece of evidence about what the
            role was trying to do.
        stage_base: The commit this stage started from. Given one, the stage's
            own commits are searched as well as its uncommitted changes.
        allowed: Paths exempt from the pass whatever their grade -- what the
            caller itself wrote before the role started, and the ledgers the
            round keeps outside anyone's ownership.
        artifacts: Paths with no author. A build writes them, so whichever role
            ran the build is the one that "wrote" them, and grading that by
            ownership reverts a measurement for having been made.
        commit_revert: Called once, between undoing the stage's commits and
            copying the strays aside, when the two have to be one step. The
            caller owns the message; the ordering is owned here, because the
            copy-aside reads ``HEAD`` and would keep the unreverted content if
            it ran first.

    Returns:
        What was found and undone, empty when the role stayed inside.
    """
    exempt = set(allowed)
    touched = git.touched_since(stage_base) if stage_base else git.changed()

    stray: list[str] = []
    trimmed: list[str] = []
    for path in touched:
        if path in exempt or matches_any(path, artifacts):
            continue
        verdict = grade(path)
        if verdict == OWNS:
            continue
        if verdict == APPENDS:
            # An append that removed a line is not an append. The file keeps its
            # own owner's text; only additions were this role's to make.
            if stage_base and git.deletions_since(stage_base, path):
                trimmed.append(path)
            continue
        if verdict != NEVER:
            raise ValueError(f"{verdict!r} is not a grade; expected {OWNS!r}, {APPENDS!r} or {NEVER!r}")
        stray.append(path)

    violations: list[str] = []
    for path in trimmed:
        if stage_base:
            git.restore_from(stage_base, [path])
        violations.append(f"{role} may only append to {path}, and it removed lines")

    if not stray:
        return EnforceReport(trimmed=tuple(trimmed), violations=tuple(violations))

    reverted = bool(stage_base) and any(path in set(git.diff_names(stage_base)) for path in stray)
    if reverted:
        git.restore_from(stage_base, stray)
        if commit_revert is not None:
            commit_revert()
    undone = git.restore(stray, quarantine=quarantine)
    # Counted off what the role wrote, not off what could still be copied: a
    # stray write that was committed is gone from the worktree by now, and a
    # note reading "wrote 0 path(s)" about a role that wrote one is worse than
    # no note at all.
    violations.append(f"{role} wrote {len(stray)} path(s) it may not write: {', '.join(stray[:8])}")
    return EnforceReport(
        trimmed=tuple(trimmed),
        stray=tuple(stray),
        quarantined=tuple(undone),
        reverted=reverted,
        violations=tuple(violations),
    )
