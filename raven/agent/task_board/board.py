"""The Task Board's model, and the rules an update has to keep before it is applied.

Everything here is pure: an update is checked whole against the board and the
turn's dispatches, and either refused with a reason the model can act on or
applied to a copy. Nothing is half-applied, and the board handed in is never
changed.
"""

from __future__ import annotations

import copy
import secrets
import unicodedata
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass, field
from typing import Any

STATUSES: tuple[str, ...] = ("needs_you", "running", "idle", "completed")
TITLE_WORDS = 20
GROUP_SUMMARY_WORDS = 50
QUESTION_WORDS = 50
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

    The step that left its group needing the user also carries the questions its reply
    asked about the matter; the page shows them still unanswered, with the summary.
    """

    id: str
    turn_at: str
    turn_id: str | None
    title: str
    summary: str
    tasks: list[TaskLink] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


@dataclass
class TaskGroup:
    """One matter: a title, and a summary of where it stands that any turn may rewrite."""

    id: str
    title: str
    summary: str
    status: str
    created_at: int
    updated_at: int
    steps: list[ProgressStep] = field(default_factory=list)


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


class UpdateRejectedError(ValueError):
    """An update the board refuses; ``str(exc)`` is what the model is told."""


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


def _filled(raw: Mapping[str, Any], key: str, name: str) -> str:
    value = str(raw.get(key) or "").strip()
    if not value:
        raise UpdateRejectedError(f"{name} is empty")
    return value


def _within(text: str, limit: int, name: str) -> None:
    words = word_count(text)
    if words > limit:
        shown = text if len(text) <= _SNIPPET else text[:_SNIPPET] + "..."
        raise UpdateRejectedError(f"{name} is {words} words long; the limit is {limit}: {shown}")


def _check_status(status: Any) -> str:
    if status not in STATUSES:
        raise UpdateRejectedError(f"unknown status: {status}; use needs_you, running, idle or completed")
    return status


def _check_questions(questions: Any, status: str) -> None:
    """A ``needs_you`` step names what its reply asked the user; a step of any other status names nothing."""
    if questions is not None and (not isinstance(questions, list) or not all(isinstance(q, str) for q in questions)):
        raise UpdateRejectedError("questions must be a list of questions")
    if not questions:
        if status == "needs_you":
            raise UpdateRejectedError("a needs_you step lists the questions its reply asked the user in questions")
        return
    if status != "needs_you":
        raise UpdateRejectedError("questions go only with needs_you: a reply that asks the user something is needs_you")
    for question in questions:
        if not question.strip():
            raise UpdateRejectedError("a question is empty")
        _within(question.strip(), QUESTION_WORDS, "a question")


def _objects(value: Any, field: str, item_error: str) -> list[Mapping[str, Any]]:
    """A list of objects from the model's arguments, or a refusal it can act on."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise UpdateRejectedError(f"{field} must be a list")
    if not all(isinstance(item, Mapping) for item in value):
        raise UpdateRejectedError(item_error)
    return value


def _check_steps(steps: Sequence[Mapping[str, Any]], groups: Set[str]) -> None:
    for raw in steps:
        group = raw.get("group")
        new_title = raw.get("new_title")
        if bool(group) == (new_title is not None):
            raise UpdateRejectedError("a step needs exactly one of group and new_title")
        if new_title is not None:
            _within(_filled(raw, "new_title", "new_title"), TITLE_WORDS, "new_title")
            _within(_filled(raw, "new_summary", "new_summary"), GROUP_SUMMARY_WORDS, "new_summary")
        elif group not in groups:
            raise UpdateRejectedError(f"unknown group: {group}")
        elif raw.get("new_summary") is not None:
            raise UpdateRejectedError("new_summary goes with new_title; change an existing group's summary in groups")
        status = _check_status(raw.get("status"))
        _within(_filled(raw, "title", "title"), TITLE_WORDS, "title")
        _filled(raw, "summary", "summary")
        _check_questions(raw.get("questions"), status)
        tasks = raw.get("tasks")
        if tasks is not None and (not isinstance(tasks, list) or not all(isinstance(h, str) for h in tasks)):
            raise UpdateRejectedError("tasks must be a list of handles")


def _check_changes(changes: Sequence[Mapping[str, Any]], groups: Set[str], stepped: Set[str]) -> None:
    """A ``groups`` entry rewrites an existing group's own title, summary or status.

    The status of a group that also has a step this turn is the step's to set, so
    the two can never disagree.
    """
    seen: set[str] = set()
    for raw in changes:
        group = raw.get("group")
        if group not in groups:
            raise UpdateRejectedError(f"unknown group: {group}")
        if group in seen:
            raise UpdateRejectedError(f"{group} appears twice in groups")
        seen.add(group)
        if all(raw.get(key) is None for key in ("title", "summary", "status")):
            raise UpdateRejectedError("a groups entry needs at least one of title, summary and status")
        if raw.get("title") is not None:
            _within(_filled(raw, "title", "groups title"), TITLE_WORDS, "groups title")
        if raw.get("summary") is not None:
            _within(_filled(raw, "summary", "groups summary"), GROUP_SUMMARY_WORDS, "groups summary")
        if raw.get("status") is not None:
            _check_status(raw["status"])
            if group in stepped:
                raise UpdateRejectedError(f"{group} has a step this turn; set its status on the step")
            if raw["status"] == "needs_you":
                raise UpdateRejectedError(f"{group}: needs_you goes on a step, with the questions it asks")


def _check_placement(placed: Sequence[str], required: Sequence[str]) -> None:
    seen: set[str] = set()
    for handle in placed:
        if handle in seen:
            raise UpdateRejectedError(f"placed more than once: {handle}")
        seen.add(handle)
    for handle in placed:
        if handle not in required:
            raise UpdateRejectedError(f"not dispatched this turn: {handle}")
    missing = [handle for handle in required if handle not in seen]
    if missing:
        raise UpdateRejectedError("not placed: " + ", ".join(missing))


def apply_update(
    board: Board,
    args: Mapping[str, Any],
    dispatches: Sequence[Dispatch],
    *,
    turn_at: str,
    turn_id: str | None,
    now_ms: int,
    mint: Callable[[str], str] = new_id,
) -> Board:
    """The board after one turn's update, or :class:`UpdateRejectedError` saying why not.

    Every handle this turn dispatched, and every one an earlier failed update
    left pending, has to be placed on exactly one step of this update.
    """
    steps = _objects(args.get("steps"), "steps", "each step must be an object")
    changes = _objects(args.get("groups"), "groups", "each groups entry must be an object")
    known = {g.id for g in board.groups}

    _check_steps(steps, known)
    _check_changes(changes, known, {raw["group"] for raw in steps if raw.get("group")})

    summaries = {p.handle: p.summary for p in board.unassigned}
    summaries.update({d.handle: d.summary for d in dispatches})
    required = list(dict.fromkeys([d.handle for d in dispatches] + [p.handle for p in board.unassigned]))
    if not steps and required:
        raise UpdateRejectedError("steps may be empty only when nothing was dispatched")
    placed = [str(h) for raw in steps for h in (raw.get("tasks") or [])]
    _check_placement(placed, required)

    out = copy.deepcopy(board)
    by_id = {g.id: g for g in out.groups}
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
    out.unassigned = [p for p in out.unassigned if p.handle not in placed]
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
    old messages out of the session, and their turns' steps stay.
    """
    out = copy.deepcopy(board)
    for group in out.groups:
        group.steps = [s for s in group.steps if s.turn_at not in removed]
    out.groups = [g for g in out.groups if g.steps]
    out.unassigned = [p for p in out.unassigned if p.turn_at not in removed]
    return out


def board_to_dict(board: Board) -> dict[str, Any]:
    """The file's shape, field for field."""
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
            )
            for g in data.get("groups") or []
        ],
        unassigned=[
            Pending(str(p["handle"]), str(p.get("summary") or ""), str(p.get("turn_at") or ""), p.get("turn_id"))
            for p in data.get("unassigned") or []
        ],
        version=int(data.get("version") or 1),
    )
