"""The backlog: the one structured thing three roles share.

Everything else a stint writes is prose, because prose is what a role writes
well. This file is structured because three roles have to change different parts
of one row -- the Planner schedules, the Developer submits, and only QA may say
a thing is done -- and "one file, one owner" cannot hold for a prose file three
roles must edit. Per-field ownership can, and only structure makes it checkable.

Two levels. A **task** is a unit of work; an **implement** is one Developer
attempt at it. One task has many implements, which is what makes "this was
attempted three times" readable -- repeated failure is the signal most worth
surfacing, and a round report cannot show it.

Nothing here is edited by hand. Every change goes through :func:`apply`, which
refuses a transition the calling role does not own, so an illegal move is a
refusal at the command rather than a violation found afterwards by diffing a
file. See ``docs/specs/2026-09-17-playbook-rounds-design.md``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

STINT_DIR = ".stint"
"""Where a project keeps what a multi-round run reads and writes about itself.

Hidden, because it is the tool's own bookkeeping sitting in somebody's
repository rather than part of their project; named for the mode rather than
for H*, which is where the layout came from and which no longer exists."""
BACKLOG_FILE = "backlog.json"
DECISIONS_FILE = "HUMAN_DECISIONS.md"
SOURCES_FILE = "SOURCES.md"

PLANNER = "planner"
DEVELOPER = "developer"
QA = "qa"
HUMAN = "human"
BOOTSTRAP = "bootstrap"

OPEN = "open"
ASSIGNED = "assigned"
IN_REVIEW = "in_review"
DONE = "done"
BLOCKED = "blocked"
REJECTED = "rejected"
DROPPED = "dropped"

TERMINAL = (REJECTED, DROPPED)

#: One row per verb: who may call it, which states it may be called from, and
#: where it lands. ``None`` for ``to`` means the verb computes its own target.
TRANSITIONS: dict[str, dict[str, Any]] = {
    "add": {"by": (PLANNER, HUMAN, BOOTSTRAP), "from": (), "to": OPEN},
    "assign": {"by": (PLANNER,), "from": (OPEN,), "to": ASSIGNED},
    "defer": {"by": (PLANNER,), "from": (OPEN, ASSIGNED), "to": OPEN},
    "reject": {"by": (PLANNER,), "from": (OPEN, ASSIGNED, BLOCKED), "to": REJECTED},
    # BLOCKED is in `from` on purpose: a task already waiting on a person can
    # turn out to be waiting on another task as well, and it stays blocked until
    # the last of them lifts.
    "block": {"by": (PLANNER,), "from": (OPEN, ASSIGNED, IN_REVIEW, BLOCKED), "to": BLOCKED},
    "unblock": {"by": (PLANNER, HUMAN), "from": (BLOCKED,), "to": None},
    "implement": {"by": (DEVELOPER,), "from": (ASSIGNED,), "to": IN_REVIEW},
    "proven": {"by": (QA,), "from": (IN_REVIEW,), "to": DONE, "says": "give a verdict on"},
    "not_proven": {"by": (QA,), "from": (IN_REVIEW,), "to": OPEN, "says": "give a verdict on"},
    "reopen": {"by": (QA,), "from": (DONE,), "to": OPEN},
    "drop": {"by": (HUMAN,), "from": (OPEN, ASSIGNED, BLOCKED, DONE), "to": DROPPED},
}


class BacklogError(RuntimeError):
    """The backlog cannot be read, or the change cannot be made -- and why."""


@dataclass
class Task:
    id: int
    title: str
    name: str = ""
    source: str = ""
    gates: list[str] = field(default_factory=list)
    depends_on: list[int] = field(default_factory=list)
    with_: list[int] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    state: str = OPEN
    # Which Developer instance holds this task, when a round runs more than one.
    # Empty is every Developer's: a round with one of them has no split to make,
    # and a task assigned before the split existed still reads correctly.
    owner: str = ""
    deferred: int = 0
    severity: str = ""
    note: str = ""
    # The state a block interrupted, so unblocking returns the task to the work
    # it was in rather than to the back of the queue.
    resume_state: str = ""
    implements: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return self.state in TERMINAL or self.state == DONE

    @property
    def label(self) -> str:
        """What a card or a log line calls the task: its name, or the title's head."""
        return self.name or short_name(self.title)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "title": self.title, "state": self.state}
        for key, value in (
            ("name", self.name),
            ("source", self.source),
            ("gates", self.gates),
            ("depends_on", self.depends_on),
            ("with", self.with_),
            ("blocked_by", self.blocked_by),
            ("owner", self.owner),
            ("deferred", self.deferred),
            ("severity", self.severity),
            ("note", self.note),
            ("resume_state", self.resume_state),
            ("implements", self.implements),
            ("history", self.history),
        ):
            if value:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=int(data["id"]),
            title=str(data.get("title") or ""),
            name=str(data.get("name") or ""),
            source=str(data.get("source") or ""),
            gates=[str(item) for item in data.get("gates") or ()],
            depends_on=[int(item) for item in data.get("depends_on") or ()],
            with_=[int(item) for item in data.get("with") or ()],
            blocked_by=[str(item) for item in data.get("blocked_by") or ()],
            state=str(data.get("state") or OPEN),
            owner=str(data.get("owner") or ""),
            deferred=int(data.get("deferred") or 0),
            severity=str(data.get("severity") or ""),
            note=str(data.get("note") or ""),
            resume_state=str(data.get("resume_state") or ""),
            implements=list(data.get("implements") or ()),
            history=list(data.get("history") or ()),
        )


@dataclass
class Backlog:
    tasks: list[Task] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def get(self, task_id: int) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise BacklogError(f"no task {task_id}")

    def next_id(self) -> int:
        return max((task.id for task in self.tasks), default=0) + 1

    def ready(self) -> list[Task]:
        """Open, unblocked, and every dependency done.

        A dependency that was dropped or rejected counts as satisfied: the plan
        decided not to do it, which is an answer, and leaving its dependants
        unreachable forever would make one rejection silently cancel a branch.
        """
        settled = {task.id for task in self.tasks if task.state == DONE or task.state in TERMINAL}
        return [
            task
            for task in self.tasks
            if task.state == OPEN and not task.blocked_by and all(dep in settled for dep in task.depends_on)
        ]

    def counts(self) -> dict[str, int]:
        tally = {state: 0 for state in (OPEN, ASSIGNED, IN_REVIEW, DONE, BLOCKED, REJECTED, DROPPED)}
        for task in self.tasks:
            tally[task.state] = tally.get(task.state, 0) + 1
        tally["ready"] = len(self.ready())
        return tally

    def to_dict(self) -> dict[str, Any]:
        return {"meta": self.meta, "tasks": [task.to_dict() for task in self.tasks]}


NAME_LIMIT = 48


def short_name(title: str, limit: int = NAME_LIMIT) -> str:
    """The head of a title, for the places that have room for a handle and not a sentence.

    Titles are written `<name>: <what done looks like>`, so the head before the
    first colon (or ` -- `) is the name; a title with neither is cut at a word
    boundary and marked so a reader knows there is more.
    """
    head = re.split(r":\s|\s--\s", title.strip(), maxsplit=1)[0].strip()
    if len(head) <= limit:
        return head
    cut = head[:limit].rsplit(" ", 1)[0].rstrip(",;") or head[:limit]
    return cut + "..."


def check_blocker(blocker: str, backlog: Backlog, question_ids: Iterable[str] | None = None) -> None:
    """Refuse a blocker that names nothing.

    `human:<qid>` must be a question `.stint/HUMAN_DECISIONS.md` already carries:
    a task blocked on an id nobody asked (measured 2026-09-10, `human:q_7_2_definition`)
    is never unblocked, because the answer that would lift it can never arrive.
    `question_ids` is None when the caller has no decisions file to check against.
    """
    kind, _, what = blocker.strip().partition(":")
    what = what.strip()
    if kind == "human":
        if question_ids is not None and what not in set(question_ids):
            raise BacklogError(
                f"no question {what!r} in {DECISIONS_FILE}. Ask it first: "
                f'`raven playbook stint ask "..." --blocks <id>` files the question and blocks the task on the id it mints'
            )
    elif kind == "task":
        if not what.isdigit():
            raise BacklogError(f"task:<id> needs a number, got {blocker!r}")
        backlog.get(int(what))
    elif kind == "external":
        if not what:
            raise BacklogError("external: needs to say what is holding the task up")
    else:
        raise BacklogError(f"a blocker is human:<qid>, task:<id> or external:<what>, not {blocker!r}")


def backlog_path(project: Path) -> Path:
    return Path(project).expanduser() / STINT_DIR / BACKLOG_FILE


def load(project: Path) -> Backlog:
    path = backlog_path(project)
    if not path.is_file():
        raise BacklogError(f"no backlog at {path}: `raven playbook stint task add` files the first one")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BacklogError(f"could not read {path}: {error}") from error
    if not isinstance(raw, dict):
        raise BacklogError(f"{path} is not a backlog document")
    tasks = [Task.from_dict(item) for item in raw.get("tasks") or () if isinstance(item, dict)]
    seen: set[int] = set()
    for task in tasks:
        if task.id in seen:
            raise BacklogError(f"{path}: task id {task.id} appears twice")
        seen.add(task.id)
    return Backlog(tasks=tasks, meta=dict(raw.get("meta") or {}))


def save(project: Path, backlog: Backlog) -> Path:
    path = backlog_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(backlog.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def confirm(project: Path) -> Backlog:
    """A person's word that the plan is the plan.

    Recorded with a time, not only a flag, so a board can say when it was
    confirmed. ``roster.ready`` is the check that reads it, and nothing in the
    rounds path asks that check yet, so today this records the word rather than
    gating on it.
    """
    from datetime import datetime, timezone

    backlog = load(project)
    backlog.meta["confirmed_by_human"] = True
    backlog.meta["confirmed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save(project, backlog)
    return backlog


def apply(
    backlog: Backlog,
    verb: str,
    *,
    role: str,
    task_id: int | None = None,
    round_index: int = 0,
    reason: str = "",
    commit: str = "",
    evidence: str = "",
    blocker: str = "",
    title: str = "",
    name: str = "",
    source: str = "",
    gates: Sequence[str] = (),
    depends_on: Sequence[int] = (),
    severity: str = "",
    note: str = "",
    owner: str = "",
    known_questions: Iterable[str] | None = None,
) -> Task:
    """Make one change, or refuse it in words the calling role can act on."""
    rule = TRANSITIONS.get(verb)
    if rule is None:
        raise BacklogError(f"no such transition {verb!r}; try {', '.join(sorted(TRANSITIONS))}")
    if role not in rule["by"]:
        raise BacklogError(
            f"{role} may not {rule.get('says', verb)} a task -- that is {' or '.join(rule['by'])}'s. "
            f"See your guard file for the transitions you own."
        )

    if verb == "add":
        if not title.strip():
            raise BacklogError("a task needs a title")
        task = Task(
            id=backlog.next_id(),
            title=title.strip(),
            name=" ".join(name.split()),
            source=source,
            gates=[str(item) for item in gates],
            depends_on=[int(item) for item in depends_on],
            severity=severity,
            note=note,
        )
        backlog.tasks.append(task)
        _note(task, round_index, OPEN, role, reason or "")
        return task

    if task_id is None:
        raise BacklogError(f"{verb} needs a task id")
    task = backlog.get(task_id)
    if task.state not in rule["from"]:
        allowed = ", ".join(rule["from"])
        raise BacklogError(f"task {task.id} is {task.state}; {verb} applies to a task that is {allowed}")

    if verb == "defer":
        task.deferred += 1
        task.state = OPEN
        _note(task, round_index, OPEN, role, reason, deferred=task.deferred)
        return task

    if verb == "block":
        if not blocker.strip():
            raise BacklogError("block needs --by: human:<qid>, task:<id>, or external:<what>")
        check_blocker(blocker, backlog, known_questions)
        # Only the first block remembers: a second blocker arriving while the
        # task is already blocked would otherwise record BLOCKED as the state to
        # come back to, and the task could never leave it.
        if task.state != BLOCKED:
            task.resume_state = task.state
        if blocker not in task.blocked_by:
            task.blocked_by.append(blocker)
        task.state = BLOCKED
        _note(task, round_index, BLOCKED, role, reason or blocker)
        return task

    if verb == "unblock":
        if blocker:
            task.blocked_by = [item for item in task.blocked_by if item != blocker]
        else:
            task.blocked_by = []
        if task.blocked_by:
            return task
        task.state = task.resume_state or OPEN
        task.resume_state = ""
        _note(task, round_index, task.state, role, reason or f"unblocked{f' ({blocker})' if blocker else ''}")
        return task

    if verb == "implement":
        if not commit.strip():
            raise BacklogError("implement needs --commit: the commit that carries the work")
        task.implements.append({"round": round_index, "commit": commit, "verdict": None})
        task.state = IN_REVIEW
        _note(task, round_index, IN_REVIEW, role, reason)
        return task

    if verb in ("proven", "not_proven"):
        if verb == "proven" and not evidence.strip():
            raise BacklogError("a proven verdict needs --evidence: the path a reader can open")
        if verb == "not_proven" and not reason.strip():
            raise BacklogError("a not-proven verdict needs --reason")
        for record in reversed(task.implements):
            if record.get("verdict") is None:
                record["verdict"] = verb
                if evidence:
                    record["evidence"] = evidence
                if reason:
                    record["reason"] = reason
                break
        task.state = DONE if verb == "proven" else OPEN
        _note(task, round_index, task.state, role, reason or evidence)
        return task

    if verb in ("reject", "drop") and not reason.strip():
        raise BacklogError(f"{verb} needs --reason: it is the record of why, and QA reads it before re-raising")
    if verb == "reopen" and not reason.strip():
        raise BacklogError("reopen needs --reason: what regressed, and where it was seen")

    if verb == "assign":
        # Cleared on every assign, not only when one is given: a task coming
        # back round and handed to nobody in particular must not still read as
        # last round's instance's.
        task.owner = owner.strip()

    task.state = rule["to"]
    _note(task, round_index, task.state, role, reason, **({"owner": task.owner} if task.owner else {}))
    return task


def _note(task: Task, round_index: int, state: str, role: str, reason: str, **extra: Any) -> None:
    entry: dict[str, Any] = {"round": round_index, "to": state, "by": role}
    if reason:
        entry["reason"] = reason
    entry.update(extra)
    task.history.append(entry)


def held_by(tasks: Sequence[Task], instance: str) -> list[Task]:
    """The assigned tasks one Developer instance is to do.

    A task with no owner belongs to whoever asks: a round with one Developer
    makes no split, and a Planner that assigned without naming an instance has
    said nothing about who does it -- showing it to all of them is the reading
    that loses no work. A task owned by another instance is that instance's
    alone, and is the whole point of the field.
    """
    return [task for task in tasks if not task.owner or task.owner == instance]


def sweep_unverified(backlog: Backlog, round_index: int) -> list[Task]:
    """Round's end: a task QA never judged goes back to open.

    Not verified is not done. The rule doubles as a capacity signal -- tasks
    bouncing back every round mean the round took on more than QA can review.
    """
    swept: list[Task] = []
    for task in backlog.tasks:
        if task.state != IN_REVIEW:
            continue
        task.state = OPEN
        _note(task, round_index, OPEN, "runtime", "unverified: the round ended before QA judged it")
        swept.append(task)
    return swept


def render(tasks: Iterable[Task]) -> str:
    """A table for a terminal, or for a role that asked to see the pool."""
    rows = list(tasks)
    if not rows:
        return "(no tasks)"
    lines = [f"{'id':>4}  {'state':<10} {'def':>3}  {'gates':<12} title"]
    for task in rows:
        gates = ",".join(task.gates)[:12]
        lines.append(f"{task.id:>4}  {task.state:<10} {task.deferred or '':>3}  {gates:<12} {task.title}")
    return "\n".join(lines)
