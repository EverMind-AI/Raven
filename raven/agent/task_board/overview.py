"""Every host conversation's Task Groups as cards: the one reader behind the Task Board page.

The page sorts cards by what they need from the owner, and the rule lives here,
once, so the board, the list and the rail badge cannot disagree. A group the
model marked as needing the owner, or completed, reads as marked; every other
group runs while a run it links still runs, and is idle otherwise -- whatever
the model said of it between the two.

Whether a run is still running is never this module's to decide, and never the
model's: it is read off the same rows the sidebar's ``tasks.list`` answers, which
the caller hands in (``rows_of``). Those rows weigh what the node registry
claims against what this process is actually executing -- a run cut off by a
restart reads interrupted there, so it must not read running here.

Boards are found by walking the sessions root for board files, never by reading
every transcript, and a session whose board, registry and transcript have not
moved since the last answer is not read again. Run state is read on every
answer, because what is running is not on disk.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.subagent.dag_store import REGISTRY_FILENAME
from raven.agent.subagent.history import session_history_root
from raven.agent.task_board.board import TaskGroup
from raven.agent.task_board.lineage import board_lineage
from raven.agent.task_board.store import BOARD_FILENAME, load_board
from raven.session.manager import SUBAGENT_CHANNEL, SessionManager

CATEGORIES: tuple[str, ...] = ("needs_you", "running", "idle", "completed")


@dataclass(frozen=True)
class Card:
    session_key: str
    session_title: str
    group_id: str
    title: str
    summary: str
    status: str
    created_at: int
    updated_at: int
    category: str
    running_since_ms: int | None
    events: int
    sub_agents: int


@dataclass(frozen=True)
class Session:
    """One host conversation's board as last read, with what its cards say about the conversation."""

    session_dir: Path
    key: str
    title: str
    groups: tuple[TaskGroup, ...]
    lineage: dict[str, str]

    @property
    def links(self) -> bool:
        return any(s.tasks for g in self.groups for s in g.steps)


Rows = Mapping[tuple[str, str], Mapping[str, Any]]
RowsOf = Callable[[Session], Rows]
"""The ``tasks.list`` rows of one session's runs, keyed ``(kind, id)``."""

Stamp = tuple[int, int, int]
_cache: dict[Path, tuple[Stamp, Session | None]] = {}


def category_of(status: str, running: bool) -> str:
    if status in ("needs_you", "completed"):
        return status
    return "running" if running else "idle"


def _read_session(session_dir: Path, transcript: Path) -> Session | None:
    """The session behind one board, or ``None`` for one the page leaves out.

    The transcript is scanned the way the session pickers scan it
    (``SessionManager._scan_file``): its last metadata record is the
    conversation as it stands, and an untitled one is named by its first
    message, flattened to one line.
    """
    meta, _count, _last, first_user, _preview = SessionManager._scan_file(transcript)
    if meta is None:
        raise ValueError(f"{transcript} has no metadata record")
    key = str(meta.get("key") or "") or SessionManager.key_from_path(transcript)
    fields = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    if key.partition(":")[0] == SUBAGENT_CHANNEL or fields.get("archived"):
        return None
    board = load_board(session_dir)
    if not board.groups:
        return None
    return Session(
        session_dir=session_dir,
        key=key,
        title=str(fields.get("title") or "") or first_user,
        groups=tuple(board.groups),
        lineage=board_lineage(session_dir, board),
    )


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def scan(sessions_dir: Path) -> list[Session]:
    """Every host conversation with a board, read again only where its files moved.

    A session that cannot be read is skipped, so one broken file never blanks the page.
    """
    global _cache
    fresh: dict[Path, tuple[Stamp, Session | None]] = {}
    sessions: list[Session] = []
    for board_file in sorted(Path(sessions_dir).glob(f"*/*/{BOARD_FILENAME}")):
        session_dir = board_file.parent
        transcript = session_dir.parent / f"{session_dir.name}.jsonl"
        stamp = (
            _mtime_ns(board_file),
            _mtime_ns(session_history_root(session_dir) / REGISTRY_FILENAME),
            _mtime_ns(transcript),
        )
        hit = _cache.get(session_dir)
        if hit is not None and hit[0] == stamp:
            session = hit[1]
        else:
            try:
                session = _read_session(session_dir, transcript)
            except Exception:
                logger.opt(exception=True).debug("Task board page: skipped {}", session_dir)
                continue
        fresh[session_dir] = (stamp, session)
        if session is not None:
            sessions.append(session)
    _cache = fresh
    return sessions


def _runs(group: TaskGroup, rows: Rows, lineage: dict[str, str]) -> tuple[int, bool, int | None]:
    """How many sub-agents worked on the group, whether one still runs, and since when (``None``: no clock).

    A spawn is one sub-agent; a DAG run is each node of it and of the runs that
    continued it; a run no row describes any more counts once. A run placed on
    several steps is still one run.
    """
    links = {(t.kind, t.id) for s in group.steps for t in s.tasks}
    count, running, starts = 0, False, []
    for kind, ident in links:
        ids = [ident] + ([run for run, placed in lineage.items() if placed == ident] if kind == "dag" else [])
        found = [rows[(kind, run)] for run in ids if (kind, run) in rows]
        count += sum(len(r.get("nodes") or ()) if kind == "dag" else 1 for r in found) or 1
        for row in found:
            if row.get("status") == "running":
                running = True
                if isinstance(row.get("started_at"), int):
                    starts.append(row["started_at"])
    return count, running, (min(starts) if starts else None)


def answer(sessions: list[Session], rows_of: RowsOf, *, include_completed: bool) -> dict[str, Any]:
    """The cards, newest first, and the two counts the page and the badge read.

    Completed cards come only when asked; the counts always cover them.
    """
    cards: list[Card] = []
    for session in sessions:
        rows: Rows = {}
        if session.links:
            try:
                rows = rows_of(session)
            except Exception:
                logger.opt(exception=True).debug("Task board page: no run state for {}", session.session_dir)
        for group in session.groups:
            sub_agents, running, since = _runs(group, rows, session.lineage)
            cards.append(
                Card(
                    session_key=session.key,
                    session_title=session.title,
                    group_id=group.id,
                    title=group.title,
                    summary=group.summary,
                    status=group.status,
                    created_at=group.created_at,
                    updated_at=group.updated_at,
                    category=category_of(group.status, running),
                    running_since_ms=since,
                    events=len(group.steps),
                    sub_agents=sub_agents,
                )
            )
    shown = sorted(
        (c for c in cards if include_completed or c.category != "completed"), key=lambda c: c.updated_at, reverse=True
    )
    return {
        "cards": [dataclasses.asdict(c) for c in shown],
        "completed_count": sum(1 for c in cards if c.category == "completed"),
        "needs_you_count": sum(1 for c in cards if c.category == "needs_you"),
    }
