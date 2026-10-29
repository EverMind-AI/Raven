"""The Task Board's model, and the rules an update has to keep before it is applied.

Everything here is pure: an update is checked whole against the board and the
turn's dispatches, and either refused with a reason the model can act on or
applied to a copy. Nothing is half-applied, and the board handed in is never
changed.
"""

from __future__ import annotations

import copy
import secrets
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass, field
from typing import Any

STATUSES: tuple[str, ...] = ("active", "waiting", "blocked", "parked", "completed")
TITLE_MAX = 80
TEXT_MAX = 400


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
    id: str
    turn_at: str
    turn_id: str | None
    text: str
    tasks: list[TaskLink] = field(default_factory=list)


@dataclass
class TaskGroup:
    id: str
    title: str
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


def _check_status(status: Any) -> str:
    if status not in STATUSES:
        raise UpdateRejectedError(f"unknown status: {status}")
    return status


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
        if new_title is not None and not str(new_title).strip():
            raise UpdateRejectedError("new_title is empty")
        if group and group not in groups:
            raise UpdateRejectedError(f"unknown group: {group}")
        _check_status(raw.get("status"))
        if not str(raw.get("text") or "").strip():
            raise UpdateRejectedError("text is empty")
        tasks = raw.get("tasks")
        if tasks is not None and (not isinstance(tasks, list) or not all(isinstance(h, str) for h in tasks)):
            raise UpdateRejectedError("tasks must be a list of handles")


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
    statuses = _objects(args.get("statuses"), "statuses", "each status change must be an object")
    known = {g.id for g in board.groups}

    _check_steps(steps, known)
    for change in statuses:
        if change.get("group") not in known:
            raise UpdateRejectedError(f"unknown group: {change.get('group')}")
        _check_status(change.get("status"))

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
        text = str(raw["text"]).strip()[:TEXT_MAX]
        if raw.get("new_title") is not None:
            group = TaskGroup(mint("g"), str(raw["new_title"]).strip()[:TITLE_MAX], status, now_ms, now_ms)
            out.groups.append(group)
            by_id[group.id] = group
        else:
            group = by_id[raw["group"]]
            if raw.get("title") and str(raw["title"]).strip():
                group.title = str(raw["title"]).strip()[:TITLE_MAX]
            group.status = status
            group.updated_at = now_ms
        group.steps.append(ProgressStep(mint("s"), turn_at, turn_id, text, links))
    for change in statuses:
        group = by_id[change["group"]]
        group.status = change["status"]
        group.updated_at = now_ms
    out.unassigned = [p for p in out.unassigned if p.handle not in placed]
    return out


def set_group_status(board: Board, group_id: str, status: str, *, now_ms: int) -> Board:
    """The board with one group's status changed; ``KeyError`` for a group it does not hold."""
    _check_status(status)
    out = copy.deepcopy(board)
    for group in out.groups:
        if group.id == group_id:
            group.status = status
            group.updated_at = now_ms
            return out
    raise KeyError(group_id)


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
    """The file's and the wire's shape, field for field."""
    return {
        "version": board.version,
        "groups": [
            {
                "id": g.id,
                "title": g.title,
                "status": g.status,
                "created_at": g.created_at,
                "updated_at": g.updated_at,
                "steps": [
                    {
                        "id": s.id,
                        "turn_at": s.turn_at,
                        "turn_id": s.turn_id,
                        "text": s.text,
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
                status=str(g.get("status") or "active"),
                created_at=int(g.get("created_at") or 0),
                updated_at=int(g.get("updated_at") or 0),
                steps=[
                    ProgressStep(
                        id=str(s["id"]),
                        turn_at=str(s.get("turn_at") or ""),
                        turn_id=s.get("turn_id"),
                        text=str(s.get("text") or ""),
                        tasks=[
                            TaskLink(str(t["kind"]), str(t["id"]), str(t.get("summary") or ""))
                            for t in s.get("tasks") or []
                        ],
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
