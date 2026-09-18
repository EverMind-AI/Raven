"""Which role may write which path, and what to do when one writes elsewhere.

Three grades per path glob. ``owns`` is free, ``appends`` may only add -- a diff
with deletions is a violation -- and anything unlisted may not be written at
all. A fourth answer, ``artifacts``, is for paths with no author: a build writes
them, so grading them by ownership would revert a measurement for having been
made.

Globs overlap by design, in two different ways. One role owns ``tools/**``
while another owns ``tools/gate_checks/check_qa_*.py``: there **the more
specific pattern wins**, the way ``.gitignore`` resolves the same question. But
one role owning ``FIXLOG.md`` while another appends to it is not a contest at
all -- it is the shape the ownership table is built on, so both keep their
grade, however exactly either one spells the path. What is refused is two roles
claiming one pattern at the *same* grade: two owners is a declaration nobody
should guess the meaning of.

Where the declaration comes from is the caller's business. H* reads it out of
the frontmatter of a guard file a person edits by hand; a playbook running
``mode: stint`` reads it off the role's own row. Both build the same
:class:`Roster` and ask it the same question.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

OWNS = "owns"
APPENDS = "appends"
NEVER = "never"

FRESH = "fresh"
CONTINUE = "continue"
SOFT = "soft"
HARD = "hard"


class RosterError(RuntimeError):
    """A role cannot be read, or the roles disagree about who owns what."""


@dataclass(frozen=True)
class Role:
    name: str
    guard: str = ""
    path: Path = Path()
    order: int | None = None
    session: str = FRESH
    enforce_read: str = SOFT
    enforce_write: str = HARD
    owns: tuple[str, ...] = ()
    appends: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()

    @property
    def in_round(self) -> bool:
        return self.order is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "order": self.order,
            "session": self.session,
            "enforce": {"read": self.enforce_read, "write": self.enforce_write},
            "owns": list(self.owns),
            "appends": list(self.appends),
            "reads": list(self.reads),
            "tasks": list(self.tasks),
            "artifacts": list(self.artifacts),
        }


@dataclass
class Roster:
    roles: list[Role] = field(default_factory=list)
    project: Path = Path(".")

    def get(self, name: str) -> Role:
        for role in self.roles:
            if role.name == name:
                return role
        raise RosterError(f"no role named {name}; this project declares {', '.join(r.name for r in self.roles)}")

    def in_round(self) -> list[Role]:
        """The roles the loop runs, in the order they run."""
        return sorted((role for role in self.roles if role.in_round), key=lambda role: (role.order or 0, role.name))

    def grade(self, role_name: str, path: str, *, round_index: int = 0) -> str:
        """What this role may do to one path: own it, append to it, or nothing.

        A role loses only to a *strictly more specific* claim by another role
        **at the same grade**. Two owners of one path is a contest and the
        exact name beats the wildcard, the way ``.gitignore`` resolves it: the
        Developer owns ``tools/**`` but not ``tools/gate_checks/check_qa_*.py``,
        which QA names outright.

        Across grades there is no contest at all, at any specificity. That is
        the shape the whole table is built on -- "the Developer owns FIXLOG, QA
        may append to it" -- and ``appends`` is by definition a claim on
        somebody else's file, so letting it outrank ``owns`` inverts the thing
        it exists to express. Compared across grades, one role appending to
        ``tools/notes.md`` took ``tools/**`` away from its owner: the guard
        section still told that role the tree was its own (it is read off the
        role's own row and knows nothing of anyone else's), so the write it was
        instructed to make came back as a violation.
        """
        mine = self._claim(role_name, path, round_index)
        if mine is None:
            return NEVER
        grade, score = mine
        for other in self.roles:
            if other.name == role_name:
                continue
            theirs = self._claim_at(other.name, path, round_index, grade)
            if theirs is not None and theirs > score:
                return NEVER
        return grade

    def artifacts(self, *, round_index: int = 0) -> tuple[str, ...]:
        """Paths no role owns because they are not anyone's writing.

        The three grades are about documents and source -- things with an author.
        A game's own output has none: the specification here has the build write
        `demo_outputs/feel_log.jsonl` whenever it runs in test mode, so whichever
        role ran it is the one that "wrote" the file, and grading that by
        ownership means QA's measurements are reverted for having been made.

        Declared per role and applied to all of them: the paths are a property
        of the project, and a set one role could write and another could not
        would be the same trap in a smaller room.
        """
        found: list[str] = []
        for role in self.roles:
            found.extend(_expand(pattern, round_index) for pattern in role.artifacts)
        return tuple(dict.fromkeys(found))

    def conflicts(self, *, round_index: int = 0) -> list[str]:
        """Two roles owning one pattern, which nobody should guess the meaning of.

        Only owners. One role owning while another appends is the shape the
        ownership table is built on, and *two* appenders is not a contest either:
        appending is additive and order-independent, which is exactly what makes
        it the weaker grade. Two owners is the case with no sensible reading.
        """
        seen: dict[str, list[str]] = {}
        for role in self.roles:
            for pattern in role.owns:
                seen.setdefault(_expand(pattern, round_index), []).append(role.name)
        return [
            f"{pattern} is owned by {', '.join(sorted(set(names)))}"
            for pattern, names in sorted(seen.items())
            if len(set(names)) > 1
        ]

    def _claim(self, role_name: str, path: str, round_index: int) -> tuple[str, int] | None:
        """This role's most specific claim on ``path``, and how specific it is."""
        best: tuple[str, int] | None = None
        for role in self.roles:
            if role.name != role_name:
                continue
            for grade, patterns in ((OWNS, role.owns), (APPENDS, role.appends)):
                for pattern in patterns:
                    expanded = _expand(pattern, round_index)
                    if not _matches(path, expanded):
                        continue
                    score = _specificity(expanded)
                    if best is None or score > best[1]:
                        best = (grade, score)
        return best

    def _claim_at(self, role_name: str, path: str, round_index: int, grade: str) -> int | None:
        """How specifically this role claims ``path`` at one grade, if it does.

        Separate from :meth:`_claim` because the two questions are genuinely
        different. A role's own grade is its narrowest word on the path whatever
        grade that is; what can *take* the path from another role is only a
        claim at the grade being contested.
        """
        best: int | None = None
        for role in self.roles:
            if role.name != role_name:
                continue
            for pattern in role.owns if grade == OWNS else role.appends:
                expanded = _expand(pattern, round_index)
                if not _matches(path, expanded):
                    continue
                score = _specificity(expanded)
                if best is None or score > best:
                    best = score
        return best

    def to_dict(self) -> dict[str, Any]:
        return {"roles": [role.to_dict() for role in self.roles]}


def _expand(pattern: str, round_index: int) -> str:
    """``reports/round_{NN}.md`` for the round being run.

    ``{NN-1}`` is the round before, which is how a role names the report it reads
    rather than the one it writes.
    """

    def substitute(match: re.Match[str]) -> str:
        offset = int(match.group("offset") or 0)
        return f"{max(round_index - offset, 0):02d}"

    return re.sub(r"\{NN(?:-(?P<offset>\d+))?\}", substitute, pattern)


def expand(pattern: str, round_index: int) -> str:
    """``_expand`` under a name a caller outside this module may use."""
    return _expand(pattern, round_index)


def matches_any(path: str, patterns: Sequence[str]) -> bool:
    """Whether ``path`` is covered by any of ``patterns``, with the roster's glob rules."""
    return any(_matches(path, pattern) for pattern in patterns)


def _matches(path: str, pattern: str) -> bool:
    """``**`` spans directories; a bare directory prefix covers everything under it."""
    if pattern.endswith("/"):
        pattern += "**"
    if "*" not in pattern and "?" not in pattern:
        return path == pattern or path.startswith(pattern.rstrip("/") + "/")
    if "**" in pattern:
        regex = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return re.fullmatch(regex, path) is not None
    return fnmatch.fnmatchcase(path, pattern)


def _specificity(pattern: str) -> int:
    """How exactly a pattern names a path: literal characters beat wildcards.

    A tie is not broken here on purpose -- two roles claiming one path with
    equally specific patterns is a declaration nobody should guess the meaning
    of, and :meth:`Roster.conflicts` is what refuses it.
    """
    return len(pattern) - 40 * pattern.count("*")


def writable(role: Role, *, round_index: int = 0) -> tuple[str, ...]:
    """Everything this role may write at all, for a prompt that lists it."""
    return tuple(_expand(pattern, round_index) for pattern in (*role.owns, *role.appends))


def reading_list(role: Role, *, round_index: int = 0) -> tuple[str, ...]:
    return tuple(_expand(pattern, round_index) for pattern in role.reads)


def describe(roster: Roster) -> str:
    lines = [f"{'role':<12} {'order':>5}  {'session':<9} {'read':<5} {'write':<5} owns"]
    for role in roster.roles:
        order = str(role.order) if role.order is not None else "--"
        lines.append(
            f"{role.name:<12} {order:>5}  {role.session:<9} {role.enforce_read:<5} "
            f"{role.enforce_write:<5} {', '.join(role.owns) or '(nothing)'}"
        )
    return "\n".join(lines)


def merged_globs(roster: Roster, grade: str, *, round_index: int = 0) -> Iterable[str]:
    for role in roster.roles:
        for pattern in role.owns if grade == OWNS else role.appends:
            yield _expand(pattern, round_index)
