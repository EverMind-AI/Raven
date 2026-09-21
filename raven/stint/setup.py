"""Laying out a project a stint is written against, before it runs.

A playbook names a layout (``setup: stint``) and this writes the missing half
of it. It exists because the alternative was seven commands: a person who said
"run rounds" in a repository that had never been set up got a stint whose
every round failed on a file reference, thirty times, because ``{{ref:}}``
resolves at node render and nothing checked earlier.

Deterministic on purpose, and free. Nothing here calls a model, nothing here
commits, and nothing here is irreversible: it writes files that are not there,
and a file that is there is left alone. What it cannot supply itself -- a
specification, which is the one thing only the person has -- it reports as
missing rather than inventing.

Kept out of the driver because the driver's job is rounds and this is a
project's shape, and out of the playbook because a playbook is a file that
travels: a setup section that could name *what* to write would be a way to make
the host write anything, so a playbook names a recipe and the recipe ships here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from raven.stint.backlog import BACKLOG_FILE, STINT_DIR
from raven.stint.bootstrap import InitError, find_spec, init

__all__ = ["Layout", "RECIPES", "lay_out"]

#: The recipes a playbook may name, and the only ones. A closed set is what
#: makes ``setup`` safe to honour from a file somebody else wrote.
RECIPES = ("stint",)


@dataclass
class Layout:
    """What the project needed, what it got, and what is still missing."""

    wrote: list[str] = field(default_factory=list)
    """Paths written, relative to the project. Empty when it was already set up."""

    kept: list[str] = field(default_factory=list)
    """Paths the recipe would have written and found already there.

    Reported because "what this run wrote" is the wrong question on the second
    attempt: a run refused after the layout -- for a check nobody had answered,
    for a specification nobody had written -- leaves the layout in the tree,
    uncommitted, and the attempt after it writes nothing and finds its own
    files there as somebody's unfinished work. Every fresh project that had to
    be asked something was refused on the retry, for the files the first try
    had written."""

    spec: str = ""
    """The document the roles will stint from, relative to the project."""

    also_matched: int = 0
    """Other documents that could have been it. Said because the pick is a
    judgement and a person reading the approval is the check on it."""

    backlog: int = -1
    """Tasks in the backlog; ``-1`` when there is no backlog yet."""

    missing: str = ""
    """Why this project cannot start a stint, addressed to whoever can fix it.
    Empty when it can."""

    @property
    def ready(self) -> bool:
        return not self.missing


def lay_out(project: Path, recipe: str) -> Layout:
    """Give ``project`` the layout ``recipe`` names, and say what it still lacks.

    Idempotent by path: a project that already has the layout gets nothing
    written and the same answer, so saying "run it again" costs a person
    nothing and a second stint on a set-up project is not a second setup.
    """
    if recipe not in RECIPES:
        return Layout(missing=f"no setup recipe named {recipe!r}; this build has {', '.join(RECIPES)}")
    project = Path(project).expanduser().resolve()
    if not (project / ".git").exists():
        # Said here rather than left to the checkout step, because the checkout
        # step runs after this one has already written files: a directory that
        # can never hold a stint should not be laid out for one first.
        return Layout(
            missing=(
                f"{project} is not a git repository. A stint undoes what a role writes outside its own "
                "paths by putting the tree back, which needs one -- run `git init` here first."
            )
        )

    candidates = find_spec(project)
    try:
        done = init(project, spec=candidates[0] if candidates else None)
    except InitError as exc:
        return Layout(missing=str(exc))

    found = Layout(
        wrote=list(done.created),
        kept=list(done.kept),
        # From the candidate rather than from what `init` recorded: the link it
        # writes is spelled relative to `.stint/`, so on a project already set
        # up that answer comes back as `../docs/PRD.md`. The choice is this
        # function's own, and so is the spelling of it.
        spec=_relative(candidates[0], project) if candidates else "",
        also_matched=max(len(candidates) - 1, 0),
    )
    if not candidates:
        # The one thing the recipe cannot write. Phrased as a request rather
        # than an error: the project is now laid out, and the only empty file
        # in it is the one whose contents are the person's to give.
        found.missing = (
            f"{project} has no document that says what it is for, so there is nothing to stint from. "
            f"Write what this project is and what done looks like into {STINT_DIR}/SPEC.md "
            "(it is there, empty, now), or say it and it will be written there."
        )
        return found
    found.backlog = _backlog_size(project)
    return found


def _relative(path: Path, project: Path) -> str:
    """The document as somebody standing in the project would name it.

    An absolute path in the approval is a line of noise with a filename at the
    end, and the approval is read by somebody standing here.
    """
    try:
        return path.resolve().relative_to(project).as_posix()
    except (ValueError, OSError):
        return str(path)


def _backlog_size(project: Path) -> int:
    """How many tasks the project's backlog holds, or ``-1`` if it has none.

    Counted off the file rather than through the backlog module's loader: a
    malformed backlog is a number this cannot report, not an exception on the
    path that was about to report what is missing.
    """
    import json

    path = project / STINT_DIR / BACKLOG_FILE
    try:
        return len(json.loads(path.read_text(encoding="utf-8")).get("tasks") or [])
    except (OSError, ValueError, AttributeError):
        return -1
