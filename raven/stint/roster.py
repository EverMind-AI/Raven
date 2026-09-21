"""The roles a project declares, read out of the guard files a person edits.

A role is a file in the project -- ``.stint/planner.md`` and its siblings --
carrying frontmatter for the runtime and prose for the model. One file, both
halves editable by hand, which is the whole point: the boundary a person wants
to move is moved by editing the boundary, not by editing raven.

The frontmatter answers four questions the loop has to ask:

* **order** -- where in the round this role runs. Absent means it is not in the
  round at all, which is how a one-shot bootstrap phase or a parallel role is
  declared without the loop having to know about it.
* **session** -- ``fresh`` opens a new conversation each round, ``continue``
  keeps one across the whole run. The roles genuinely differ: a Planner picking
  from a backlog needs last round's reports and nothing else, while a Developer
  carrying an implementation in its head is worse off starting over.
* **owns / appends / reads** -- three grades of access, per path glob.
* **enforce** -- whether those are checked by the program or only stated in the
  prompt. Stated-only is honest; a boundary announced as enforced and quietly
  unenforceable is not.

What a grade *means*, and how overlapping globs resolve, is not H*'s own and
lives in :mod:`raven.stint.ownership`. What stays here is where the
declaration comes from -- a guard file on disk -- and what a run must be able
to assume before it starts. The generic names are re-exported so callers that
have always read them from here keep working.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

import yaml

from raven.stint.backlog import STINT_DIR
from raven.stint.ownership import (
    APPENDS,
    CONTINUE,
    FRESH,
    HARD,
    NEVER,
    OWNS,
    SOFT,
    Role,
    Roster,
    RosterError,
    describe,
    matches_any,
    merged_globs,
    reading_list,
    writable,
)

__all__ = [
    "APPENDS",
    "CONTINUE",
    "DEFAULT_ROLES",
    "DEVELOPER",
    "FRESH",
    "HARD",
    "NEVER",
    "OWNS",
    "PLANNER",
    "QA",
    "SOFT",
    "Role",
    "Roster",
    "RosterError",
    "describe",
    "load",
    "matches_any",
    "merged_globs",
    "parse",
    "preflight",
    "reading_list",
    "role_path",
    "roster_dir",
    "writable",
]

PLANNER = "planner"
DEVELOPER = "developer"
QA = "qa"
DEFAULT_ROLES = (PLANNER, DEVELOPER, QA)

_FRONTMATTER = re.compile(r"\A---\n(?P<data>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)


def roster_dir(project: Path) -> Path:
    return Path(project).expanduser() / STINT_DIR


def role_path(project: Path, name: str) -> Path:
    return roster_dir(project) / f"{name}.md"


def load(project: Path, names: Sequence[str] = DEFAULT_ROLES) -> Roster:
    """Every declared role, or an error naming the first file that is not there."""
    roles: list[Role] = []
    for name in names:
        path = role_path(project, name)
        if not path.is_file():
            raise RosterError(f"no guard file at {path}: run `raven playbook stint init` before a run")
        roles.append(parse(path.read_text(encoding="utf-8", errors="replace"), name=name, path=path))
    return Roster(roles=roles, project=Path(project))


def parse(text: str, *, name: str, path: Path | None = None) -> Role:
    match = _FRONTMATTER.match(text)
    if match is None:
        raise RosterError(f"{path or name}: a guard file starts with a `---` frontmatter block")
    try:
        data = yaml.safe_load(match.group("data")) or {}
    except yaml.YAMLError as error:
        raise RosterError(f"{path or name}: the frontmatter is not readable YAML: {error}") from error
    if not isinstance(data, dict):
        raise RosterError(f"{path or name}: the frontmatter is a mapping of settings")

    declared = str(data.get("role") or name)
    if declared != name:
        raise RosterError(f"{path or name}: declares role {declared!r} but is the guard file for {name!r}")

    session = str(data.get("session") or FRESH)
    if session not in (FRESH, CONTINUE):
        raise RosterError(f"{path or name}: session is {FRESH} or {CONTINUE}, not {session!r}")
    enforce = data.get("enforce") if isinstance(data.get("enforce"), dict) else {}
    read, write = str(enforce.get("read") or SOFT), str(enforce.get("write") or HARD)
    for label, value in (("read", read), ("write", write)):
        if value not in (SOFT, HARD):
            raise RosterError(f"{path or name}: enforce.{label} is {SOFT} or {HARD}, not {value!r}")

    order = data.get("order")
    return Role(
        name=name,
        guard=match.group("body").strip(),
        path=path or Path(f"{name}.md"),
        order=int(order) if order is not None else None,
        session=session,
        enforce_read=read,
        enforce_write=write,
        owns=_strings(data.get("owns")),
        appends=_strings(data.get("appends")),
        reads=_strings(data.get("reads")),
        artifacts=_strings(data.get("artifacts")),
        tasks=_strings(data.get("tasks")),
    )


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def preflight(
    project: Path,
    *,
    harness: str = "raven",
    names: Sequence[str] = DEFAULT_ROLES,
    require_backlog: bool = True,
) -> list[str]:
    """Everything that must hold before a round runs, as the reasons it does not.

    Checked up front and all at once, so a person fixes a project in one pass
    rather than one refusal per attempt. An empty list means the run may start.

    ``require_backlog`` is the two gates on the backlog itself: that a person
    confirmed it and that it is not empty. They hold for a flow where a person
    writes the plan before round one. A playbook whose Planner writes the plan
    *in* round one, from the specification, has an empty backlog by design at
    this point and a person's word already on the round's approval, so its
    driver passes ``False`` and keeps the roster, specification and harness
    checks.
    """
    from raven.stint import backlog as backlog_mod

    reasons: list[str] = []
    try:
        roster = load(project, names)
    except RosterError as error:
        return [str(error)]
    reasons.extend(roster.conflicts())

    if require_backlog:
        try:
            backlog = backlog_mod.load(project)
        except backlog_mod.BacklogError as error:
            reasons.append(str(error))
        else:
            if not backlog.meta.get("confirmed_by_human"):
                reasons.append(
                    f"{backlog_mod.backlog_path(project)} has not been confirmed by a person: "
                    f"read the plan, then set meta.confirmed_by_human"
                )
            if not backlog.tasks:
                reasons.append("the backlog is empty, so the Planner would have nothing to pick from")

    spec = roster_dir(project) / "SPEC.md"
    if not spec.exists():
        reasons.append(
            f"no specification at {spec}: `raven playbook stint init` links it to the project's own document"
        )

    # A read boundary is enforced through the per-role config that carries the
    # tool refusals, and an ACP agent brings its own tools. Refused rather than
    # quietly downgraded: a boundary announced as enforced and unenforceable is
    # worse than one announced as prompt policy.
    if harness != "raven":
        for role in roster.roles:
            if role.enforce_read == HARD:
                reasons.append(
                    f"{role.name} asks for `read: hard`, which needs the raven harness -- "
                    f"{harness} brings its own file tools and raven cannot deny one at its gate"
                )
    return reasons
