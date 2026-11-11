"""The Task Board's model, and the rules an update has to keep before it is applied.

Everything here is pure: an update is checked whole against the board and the
turn's dispatches, and either refused with every fault the model has to fix or
applied to a copy. Nothing is half-applied, and the board handed in is never
changed.
"""

from __future__ import annotations

import copy
import secrets
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence, Set
from dataclasses import dataclass, field
from typing import Any

STATUSES: tuple[str, ...] = ("needs_you", "running", "idle", "completed")
TITLE_WORDS = 20
# The instruction asks for summaries and questions of at most 50 words; the board refuses
# only past 100. A stated length is overshot, by some models by half again, and every
# refusal costs a whole call -- while a higher stated number makes some models write longer.
GROUP_SUMMARY_WORDS = 100
QUESTION_WORDS = 100
_SNIPPET = 24


@dataclass(frozen=True)
class Dispatch:
    """One spawn call or DAG run a turn started: ``spawn:<node_id>`` or ``dag:<run_id>``."""

    handle: str
    summary: str


@dataclass
class TaskLink:
    kind: str
    id: str
    summary: str


@dataclass
class ProgressStep:
    """One turn's advance on a group: a title, and a summary recall reads back.

    The step that left its group needing the user also carries the questions the matter
    still waits on -- those its reply asked, plus any carried from the step before it;
    the page shows them still unanswered, with the summary.
    """

    id: str
    turn_at: str
    turn_id: str | None
    title: str
    summary: str
    tasks: list[TaskLink] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


@dataclass
class GroupRevision:
    """What one turn's update changed on a group that already stood: each field it
    changed, as it was before and as the update left it, and the questions the carry
    rewrote on a step of an earlier turn, by step id. An undo of that turn puts back
    what it changed, and only where nothing written since has changed it again."""

    turn_at: str
    before: dict[str, Any]
    after: dict[str, Any]
    questions: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)


REVISED_FIELDS = ("title", "summary", "status", "updated_at")


@dataclass
class TaskGroup:
    """One matter: a title, and a summary of where it stands that any turn may rewrite.

    ``revisions`` are kept in the file only; the page is never sent them.
    """

    id: str
    title: str
    summary: str
    status: str
    created_at: int
    updated_at: int
    steps: list[ProgressStep] = field(default_factory=list)
    revisions: list[GroupRevision] = field(default_factory=list)


@dataclass
class Pending:
    """A dispatch whose turn's update never landed; the next update must place it."""

    handle: str
    summary: str
    turn_at: str
    turn_id: str | None


@dataclass
class Board:
    groups: list[TaskGroup]
    unassigned: list[Pending]
    version: int = 1


@dataclass(frozen=True)
class Problem:
    """One rule an update breaks, worded for the model; ``where`` names the entry it is in, ``steps[0]`` or ``groups[1]``."""

    text: str
    where: str = ""

    def __str__(self) -> str:
        return f"{self.where}: {self.text}" if self.where else self.text


class UpdateRejectedError(ValueError):
    """An update the board refuses, with every rule it breaks in ``problems``; ``str(exc)`` reads them one to a line."""

    def __init__(self, problems: str | Sequence[Problem]) -> None:
        self.problems = (Problem(problems),) if isinstance(problems, str) else tuple(problems)
        super().__init__("\n".join(map(str, self.problems)))


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _link(handle: str, summary: str) -> TaskLink:
    kind, _, ident = handle.partition(":")
    return TaskLink(kind, ident, summary)


def word_count(text: str) -> int:
    """A text's length the way a Chinese word count reads it: each CJK character is one, each other word is one.

    So one limit reads as characters to a Chinese writer and as words to an English
    one. Punctuation inside a word (``ui-web``, ``don't``) keeps it one word and on its
    own counts nothing -- except full-width punctuation, which takes a character's place.
    """
    count = 0
    in_word = False
    for ch in text:
        if ch.isspace():
            in_word = False
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            count += 1
            in_word = False
        elif ch.isalnum() and not in_word:
            count += 1
            in_word = True
    return count


class _Rules:
    """The faults of one update, gathered whole: one refusal names all the model has to fix.

    Stopped at the first, each further fault would wait a whole call to be named, and
    an update has three. With ``allow_long`` a text past its limit is taken as written.
    """

    def __init__(self, *, allow_long: bool = False) -> None:
        self.allow_long = allow_long
        self.problems: list[Problem] = []

    def refuse(self, text: str, where: str = "") -> None:
        self.problems.append(Problem(text, where))

    def filled(self, value: Any, name: str, where: str, limit: int | None = None) -> None:
        value = str(value or "").strip()
        if not value:
            self.refuse(f"{name} is empty", where)
            return
        words = word_count(value)
        if limit is None or words <= limit or self.allow_long:
            return
        shown = value if len(value) <= _SNIPPET else value[:_SNIPPET] + "..."
        self.refuse(f"{name} is {words} words long; the limit is {limit}: {shown}", where)


def _status_fault(status: Any) -> str | None:
    return None if status in STATUSES else f"unknown status: {status}; use needs_you, running, idle or completed"


def _check_status(status: Any) -> str:
    if fault := _status_fault(status):
        raise UpdateRejectedError(fault)
    return status


def _check_questions(rules: _Rules, questions: Any, status: str, where: str) -> None:
    """A ``needs_you`` step names what its reply asked the user; a step of any other status names nothing."""
    if questions is not None and (not isinstance(questions, list) or not all(isinstance(q, str) for q in questions)):
        rules.refuse("questions must be a list of questions", where)
    elif not questions:
        if status == "needs_you":
            rules.refuse("a needs_you step lists the questions its reply asked the user in questions", where)
    elif status != "needs_you":
        rules.refuse("questions go only with needs_you: a reply that asks the user something is needs_you", where)
    else:
        for index, question in enumerate(questions):
            rules.filled(question, f"questions[{index}]", where, QUESTION_WORDS)


def _objects(rules: _Rules, value: Any, field: str, item_error: str) -> list[Mapping[str, Any]] | None:
    """A list of objects from the model's arguments, or ``None`` and a refusal it can act on."""
    if value is None:
        return []
    if not isinstance(value, list):
        rules.refuse(f"{field} must be a list")
        return None
    if not all(isinstance(item, Mapping) for item in value):
        rules.refuse(item_error)
        return None
    return value


def _check_steps(rules: _Rules, steps: Sequence[Mapping[str, Any]], groups: Set[str]) -> None:
    seen: set[str] = set()
    for index, raw in enumerate(steps):
        where = f"steps[{index}]"
        group = raw.get("group")
        new_title = raw.get("new_title")
        if bool(group) == (new_title is not None):
            rules.refuse("a step needs exactly one of group and new_title", where)
        elif new_title is not None:
            rules.filled(new_title, "new_title", where, TITLE_WORDS)
            rules.filled(raw.get("new_summary"), "new_summary", where, GROUP_SUMMARY_WORDS)
        elif not isinstance(group, str) or group not in groups:
            rules.refuse(f"unknown group: {group}", where)
        elif group in seen:
            rules.refuse(f"{group} appears twice in steps; give each group one step per turn", where)
        elif raw.get("new_summary") is not None:
            rules.refuse("new_summary goes with new_title; change an existing group's summary in groups", where)
        if isinstance(group, str):
            seen.add(group)
        status = raw.get("status")
        if fault := _status_fault(status):
            rules.refuse(fault, where)
        rules.filled(raw.get("title"), "title", where, TITLE_WORDS)
        rules.filled(raw.get("summary"), "summary", where)
        if not fault:
            _check_questions(rules, raw.get("questions"), status, where)
        tasks = raw.get("tasks")
        if tasks is not None and (not isinstance(tasks, list) or not all(isinstance(h, str) for h in tasks)):
            rules.refuse("tasks must be a list of handles", where)


def _check_changes(rules: _Rules, changes: Sequence[Mapping[str, Any]], groups: Set[str], stepped: Set[str]) -> None:
    """A ``groups`` entry rewrites an existing group's own title, summary or status.

    The status of a group that also has a step this turn is the step's to set, so
    the two can never disagree.
    """
    seen: set[str] = set()
    for index, raw in enumerate(changes):
        where = f"groups[{index}]"
        group = raw.get("group")
        if not isinstance(group, str) or group not in groups:
            rules.refuse(f"unknown group: {group}", where)
            continue
        if group in seen:
            rules.refuse(f"{group} appears twice in groups", where)
            continue
        seen.add(group)
        if all(raw.get(key) is None for key in ("title", "summary", "status")):
            rules.refuse("a groups entry needs at least one of title, summary and status", where)
        if raw.get("title") is not None:
            rules.filled(raw["title"], "title", where, TITLE_WORDS)
        if raw.get("summary") is not None:
            rules.filled(raw["summary"], "summary", where, GROUP_SUMMARY_WORDS)
        status = raw.get("status")
        if status is None:
            continue
        if fault := _status_fault(status):
            rules.refuse(fault, where)
        elif group in stepped:
            rules.refuse(f"{group} has a step this turn; set its status on the step", where)
        elif status == "needs_you":
            rules.refuse("needs_you goes on a step, with the questions it asks", where)


def _stepped(steps: Sequence[Mapping[str, Any]]) -> set[str]:
    return {raw["group"] for raw in steps if isinstance(raw.get("group"), str)}


def _tasks(raw: Mapping[str, Any]) -> list[str]:
    tasks = raw.get("tasks")
    return [h for h in tasks if isinstance(h, str)] if isinstance(tasks, list) else []


def _check_placement(rules: _Rules, placed: Sequence[str], required: Sequence[str]) -> None:
    for handle, times in Counter(placed).items():
        if times > 1:
            rules.refuse(f"placed more than once: {handle}")
    for handle in dict.fromkeys(placed):
        if handle not in required:
            rules.refuse(f"not dispatched this turn: {handle}")
    missing = [handle for handle in required if handle not in placed]
    if missing:
        rules.refuse("not placed: " + ", ".join(missing))


def apply_update(
    board: Board,
    args: Mapping[str, Any],
    dispatches: Sequence[Dispatch],
    *,
    turn_at: str,
    turn_id: str | None,
    now_ms: int,
    mint: Callable[[str], str] = new_id,
    allow_long: bool = False,
) -> Board:
    """The board after one turn's update, or :class:`UpdateRejectedError` naming every rule it breaks.

    Every handle this turn dispatched, and every one an earlier failed update
    left pending, has to be placed on exactly one step of this update. With
    ``allow_long``, a :func:`salvage`'s, text past its limit is taken as written.
    """
    rules = _Rules(allow_long=allow_long)
    steps = _objects(rules, args.get("steps"), "steps", "each step must be an object")
    changes = _objects(rules, args.get("groups"), "groups", "each groups entry must be an object")
    known = {g.id for g in board.groups}
    required = list(dict.fromkeys([d.handle for d in dispatches] + [p.handle for p in board.unassigned]))

    if steps is not None:
        _check_steps(rules, steps, known)
    if changes is not None:
        _check_changes(rules, changes, known, _stepped(steps or []))
    if steps is not None:
        if not steps and required:
            rules.refuse("steps may be empty only when nothing was dispatched")
        else:
            _check_placement(rules, [h for raw in steps for h in _tasks(raw)], required)
    if rules.problems:
        raise UpdateRejectedError(rules.problems)

    steps, changes = steps or [], changes or []
    summaries = {p.handle: p.summary for p in board.unassigned}
    summaries.update({d.handle: d.summary for d in dispatches})
    placed = [h for raw in steps for h in _tasks(raw)]
    out = copy.deepcopy(board)
    by_id = {g.id: g for g in out.groups}
    touched = list(dict.fromkeys([raw["group"] for raw in steps if raw.get("group")] + [c["group"] for c in changes]))
    before = {gid: _revised(by_id[gid]) for gid in touched}
    for raw in steps:
        status = raw["status"]
        links = [_link(h, summaries[h]) for h in (str(h) for h in (raw.get("tasks") or []))]
        if raw.get("new_title") is not None:
            title, summary = str(raw["new_title"]).strip(), str(raw["new_summary"]).strip()
            group = TaskGroup(mint("g"), title, summary, status, now_ms, now_ms)
            out.groups.append(group)
            by_id[group.id] = group
        else:
            group = by_id[raw["group"]]
            group.status = status
            group.updated_at = now_ms
        questions = [q.strip() for q in raw.get("questions") or []]
        step = ProgressStep(
            mint("s"), turn_at, turn_id, str(raw["title"]).strip(), str(raw["summary"]).strip(), links, questions
        )
        group.steps.append(step)
    for change in changes:
        group = by_id[change["group"]]
        if change.get("title") is not None:
            group.title = str(change["title"]).strip()
        if change.get("summary") is not None:
            group.summary = str(change["summary"]).strip()
        if change.get("status") is not None:
            group.status = change["status"]
        group.updated_at = now_ms
    for gid in touched:
        group, was = by_id[gid], before[gid]
        now = _revised(group)
        changed = [name for name in REVISED_FIELDS if now[name] != was[name]]
        if changed:
            group.revisions.append(
                GroupRevision(turn_at, {name: was[name] for name in changed}, {name: now[name] for name in changed})
            )
    out.unassigned = [p for p in out.unassigned if p.handle not in placed]
    return out


def apply_carry(
    board: Board,
    args: Mapping[str, Any],
    eligible: Sequence[str],
    *,
    turn_at: str,
    now_ms: int,
) -> Board:
    """The board with the questions each group's newest step now carries.

    ``eligible`` names the groups this turn updated while their latest step still
    asked something. Each has to be answered with the complete list its newest step
    carries. A question the newest step itself asked this turn may not be left out,
    since it went out with the reply and the user cannot have answered it yet; the
    questions of an older step are the ones this turn may have settled. A group left
    with questions is ``needs_you``; one left with none goes idle, so a waiting group
    never outlives the question it waited on. :class:`UpdateRejectedError` names every
    rule the answer breaks.
    """
    rules = _Rules()
    entries = args.get("groups")
    if not isinstance(entries, list) or not all(isinstance(raw, Mapping) for raw in entries):
        raise UpdateRejectedError(Problem("groups must be a list of objects"))
    known = set(eligible)
    by_id = {group.id: group for group in board.groups}
    carried: dict[str, list[str]] = {}
    for index, raw in enumerate(entries):
        where = f"groups[{index}]"
        group_id = raw.get("group")
        if not isinstance(group_id, str) or group_id not in known:
            rules.refuse(f"not asked about: {group_id}", where)
            continue
        if group_id in carried:
            rules.refuse(f"{group_id} appears twice", where)
            continue
        questions = raw.get("questions")
        if not isinstance(questions, list) or not all(isinstance(question, str) for question in questions):
            rules.refuse("questions must be a list of questions", where)
            continue
        for number, question in enumerate(questions):
            rules.filled(question, f"questions[{number}]", where, QUESTION_WORDS)
        carried[group_id] = [question.strip() for question in questions]
    for group_id in eligible:
        if group_id not in carried:
            rules.refuse(f"not answered: {group_id}")
            continue
        group = by_id.get(group_id)
        if group is None or not group.steps:
            rules.refuse(f"no step to carry on: {group_id}")
            continue
        if group.steps[-1].turn_at != turn_at:
            continue
        missing = [question for question in group.steps[-1].questions if question not in carried[group_id]]
        if missing:
            rules.refuse(f"a question the newest step already asks is missing: {missing[0]}")
    if rules.problems:
        raise UpdateRejectedError(rules.problems)

    out = copy.deepcopy(board)
    by_id = {group.id: group for group in out.groups}
    before = {group_id: _revised(by_id[group_id]) for group_id in carried}
    rewritten: dict[str, dict[str, tuple[list[str], list[str]]]] = {}
    for group_id, questions in carried.items():
        group = by_id[group_id]
        step = group.steps[-1]
        was = list(step.questions)
        step.questions = questions
        if step.turn_at != turn_at and was != questions:
            rewritten[group_id] = {step.id: (was, list(questions))}
        if questions:
            group.status = "needs_you"
        elif group.status == "needs_you":
            group.status = "idle"
        group.updated_at = now_ms
    for group_id in carried:
        group, was = by_id[group_id], before[group_id]
        now = _revised(group)
        changed = [name for name in REVISED_FIELDS if now[name] != was[name]]
        step_questions = rewritten.get(group_id, {})
        if changed or step_questions:
            group.revisions.append(
                GroupRevision(
                    turn_at,
                    {name: was[name] for name in changed},
                    {name: now[name] for name in changed},
                    step_questions,
                )
            )
    return out


def _revised(group: TaskGroup) -> dict[str, Any]:
    return {name: getattr(group, name) for name in REVISED_FIELDS}


def owning_groups(board: Board, handles: Collection[str], groups: Collection[str]) -> list[str]:
    """The groups a turn is known to be about before its update is asked, in the board's order.

    ``handles`` are the delegated runs whose result opened the turn; each belongs to the
    group whose step placed it. ``groups`` are the ones the reader's Move on named. A run
    no step has placed yet, and a group the board no longer holds, name nothing.
    """
    runs = {(kind, ident) for kind, _, ident in (handle.partition(":") for handle in handles)}
    return [
        group.id
        for group in board.groups
        if group.id in groups or any((link.kind, link.id) in runs for step in group.steps for link in step.tasks)
    ]


def confine_update(args: Mapping[str, Any], owners: Collection[str]) -> dict[str, Any]:
    """The update with only what a turn that belongs to ``owners`` may write.

    It keeps the steps on those groups, every step that places work, wherever that
    goes -- a dispatch has to land somewhere, a new group included -- and every step that
    asks the user something, since a question that went out with the reply has to land
    somewhere too; and the ``groups`` changes on the groups those steps keep. What it says
    about any other matter is dropped rather than refused: the turn was not about it.
    Arguments of the wrong shape pass through whole, for the rules to refuse with their
    own reason. No owners, no change.
    """
    out = dict(args)
    steps, changes = args.get("steps"), args.get("groups")
    if not owners or not isinstance(steps, list) or not all(isinstance(raw, Mapping) for raw in steps):
        return out
    owned = list(owners)
    out["steps"] = [raw for raw in steps if raw.get("group") in owned or raw.get("tasks") or raw.get("questions")]
    if isinstance(changes, list) and all(isinstance(raw, Mapping) for raw in changes):
        moved = owned + [raw.get("group") for raw in out["steps"]]
        out["groups"] = [raw for raw in changes if raw.get("group") in moved]
    return out


def salvage(board: Board, args: Mapping[str, Any]) -> dict[str, Any]:
    """The last update a model never got accepted, cut back to what the board can still take.

    Each ``groups`` entry keeps only the fields that pass on their own -- a status the
    step on its group already sets goes, the summary written beside it stays -- and a
    group keeps its first such entry. Applied with ``allow_long``, its text past a
    limit stands as written. Steps are left whole: a fault in one, or in where the work
    is placed, is still refused, and the work parked.
    """
    out = dict(args)
    changes = args.get("groups")
    if changes is None:
        return out
    steps = args.get("steps")
    stepped = _stepped([raw for raw in steps if isinstance(raw, Mapping)]) if isinstance(steps, list) else set()
    known = {g.id for g in board.groups}
    kept: list[dict[str, Any]] = []
    for raw in changes if isinstance(changes, list) else []:
        if not isinstance(raw, Mapping) or any(entry["group"] == raw.get("group") for entry in kept):
            continue
        fields = {}
        for key in ("title", "summary", "status"):
            if raw.get(key) is None:
                continue
            rules = _Rules(allow_long=True)
            _check_changes(rules, [{"group": raw.get("group"), key: raw[key]}], known, stepped)
            if not rules.problems:
                fields[key] = raw[key]
        if fields:
            kept.append({"group": raw["group"], **fields})
    out["groups"] = kept
    return out


def set_group_status(board: Board, group_id: str, status: str, *, now_ms: int) -> Board:
    """The board with one group's status changed; ``KeyError`` for a group it does not hold."""
    status = _check_status(status)
    out = copy.deepcopy(board)
    for group in out.groups:
        if group.id == group_id:
            group.status = status
            group.updated_at = now_ms
            return out
    raise KeyError(group_id)


def ignore_question(board: Board, group_id: str, step_id: str, question: str, *, now_ms: int) -> Board:
    """The board with one question the reader set aside, gone from its step.

    A group that waits on the user goes idle once its latest step asks nothing more.
    ``KeyError`` for a group or step the board does not hold; a question the step no
    longer lists changes nothing, and the board handed in comes back as it was.
    """
    group = next((g for g in board.groups if g.id == group_id), None)
    if group is None:
        raise KeyError(group_id)
    step = next((s for s in group.steps if s.id == step_id), None)
    if step is None:
        raise KeyError(step_id)
    if question not in step.questions:
        return board
    out = copy.deepcopy(board)
    group = next(g for g in out.groups if g.id == group_id)
    next(s for s in group.steps if s.id == step_id).questions.remove(question)
    if group.status == "needs_you" and not group.steps[-1].questions:
        group.status = "idle"
    group.updated_at = now_ms
    return out


def drop_turns(board: Board, removed: Set[str]) -> Board:
    """The board without what it recorded for the named turns -- the ones an undo removed.

    Named rather than inferred from what the log still holds: consolidation moves
    old messages out of the session, and their turns' steps stay. A group such a
    turn changed gets back each field that turn changed, and the questions the carry
    rewrote on a step that is still there, newest turn first, unless a later writer
    -- the reader's own Mark completed or set-aside -- changed it again.
    """
    out = copy.deepcopy(board)
    for group in out.groups:
        group.steps = [s for s in group.steps if s.turn_at not in removed]
        for revision in reversed([r for r in group.revisions if r.turn_at in removed]):
            for name, value in revision.after.items():
                if getattr(group, name) == value:
                    setattr(group, name, revision.before[name])
            for step_id, (was, now) in revision.questions.items():
                step = next((s for s in group.steps if s.id == step_id), None)
                if step is not None and step.questions == now:
                    step.questions = list(was)
        group.revisions = [r for r in group.revisions if r.turn_at not in removed]
    out.groups = [g for g in out.groups if g.steps]
    out.unassigned = [p for p in out.unassigned if p.turn_at not in removed]
    return out


def board_to_dict(board: Board, *, revisions: bool = False) -> dict[str, Any]:
    """The wire's shape, field for field; with ``revisions``, the file's."""
    return {
        "version": board.version,
        "groups": [
            {
                "id": g.id,
                "title": g.title,
                "summary": g.summary,
                "status": g.status,
                "created_at": g.created_at,
                "updated_at": g.updated_at,
                "steps": [
                    {
                        "id": s.id,
                        "turn_at": s.turn_at,
                        "turn_id": s.turn_id,
                        "title": s.title,
                        "summary": s.summary,
                        "questions": list(s.questions),
                        "tasks": [{"kind": t.kind, "id": t.id, "summary": t.summary} for t in s.tasks],
                    }
                    for s in g.steps
                ],
                **(
                    {
                        "revisions": [
                            {
                                "turn_at": r.turn_at,
                                "before": dict(r.before),
                                "after": dict(r.after),
                                **(
                                    {
                                        "questions": {
                                            step_id: {"before": list(was), "after": list(now)}
                                            for step_id, (was, now) in r.questions.items()
                                        }
                                    }
                                    if r.questions
                                    else {}
                                ),
                            }
                            for r in g.revisions
                        ]
                    }
                    if revisions
                    else {}
                ),
            }
            for g in board.groups
        ],
        "unassigned": [
            {"handle": p.handle, "summary": p.summary, "turn_at": p.turn_at, "turn_id": p.turn_id}
            for p in board.unassigned
        ],
    }


def board_from_dict(data: Mapping[str, Any]) -> Board:
    return Board(
        groups=[
            TaskGroup(
                id=str(g["id"]),
                title=str(g.get("title") or ""),
                summary=str(g.get("summary") or ""),
                status=str(g.get("status") or "idle"),
                created_at=int(g.get("created_at") or 0),
                updated_at=int(g.get("updated_at") or 0),
                steps=[
                    ProgressStep(
                        id=str(s["id"]),
                        turn_at=str(s.get("turn_at") or ""),
                        turn_id=s.get("turn_id"),
                        title=str(s.get("title") or s.get("text") or ""),
                        summary=str(s.get("summary") or s.get("text") or ""),
                        tasks=[
                            TaskLink(str(t["kind"]), str(t["id"]), str(t.get("summary") or ""))
                            for t in s.get("tasks") or []
                        ],
                        questions=[str(q) for q in s.get("questions") or []],
                    )
                    for s in g.get("steps") or []
                ],
                revisions=[
                    GroupRevision(
                        str(r["turn_at"]),
                        dict(r.get("before") or {}),
                        dict(r.get("after") or {}),
                        {
                            str(step_id): (
                                [str(q) for q in (change.get("before") or [])],
                                [str(q) for q in (change.get("after") or [])],
                            )
                            for step_id, change in (r.get("questions") or {}).items()
                        },
                    )
                    for r in g.get("revisions") or []
                ],
            )
            for g in data.get("groups") or []
        ],
        unassigned=[
            Pending(str(p["handle"]), str(p.get("summary") or ""), str(p.get("turn_at") or ""), p.get("turn_id"))
            for p in data.get("unassigned") or []
        ],
        version=int(data.get("version") or 1),
    )
