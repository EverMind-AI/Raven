"""One host process's way to a session's Task Board: a lock per session, and the announcement after each write.

Two locks per session. The update queue holds a post-turn update across its
model call, so the next turn's update is built on this one's result. The write
lock is held only to read, change and write the file, by every writer -- the
post-turn update's last step, the reader's two actions, clear and undo -- so
none of those waits on a model call. A clear or an undo also leaves a mark
(the generation, the dropped turns) that an update still on its way checks
before it writes. Every write is announced as a ``tasks.board_updated`` event;
an announcement that cannot be delivered costs the client a refresh, never the
write.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.task_board.board import (
    Board,
    UpdateRejectedError,
    board_to_dict,
    drop_turns,
    ignore_question,
    set_group_status,
)
from raven.agent.task_board.lineage import board_lineage
from raven.agent.task_board.notes import add_move_on, add_note, load_notes, note_line, save_notes
from raven.agent.task_board.store import load_board, save_board

if TYPE_CHECKING:
    from raven.session.manager import SessionManager

READER_STATUSES = ("completed", "idle")

Sink = Callable[[str, dict[str, Any]], Awaitable[None]]


def _wall_ms() -> int:
    return int(time.time() * 1000)


class TaskBoardService:
    def __init__(self, sessions: SessionManager, *, now_ms: Callable[[], int] = _wall_ms) -> None:
        self._sessions = sessions
        self._locks: dict[str, asyncio.Lock] = {}
        self._queues: dict[str, asyncio.Lock] = {}
        self._generation: dict[str, int] = {}
        self._dropped: dict[str, set[str]] = {}
        self._shown: dict[str, int] = {}
        self._sink: Sink | None = None
        self.now_ms = now_ms

    def session_dir(self, session_key: str) -> Path:
        return self._sessions.session_dir(session_key)

    def messages(self, session_key: str) -> list[dict[str, Any]]:
        session = self._sessions.peek(session_key)
        return list(session.messages) if session is not None else []

    def lock(self, session_key: str) -> asyncio.Lock:
        """The write lock: held only to read, change and write the board."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    def queue(self, session_key: str) -> asyncio.Lock:
        """One post-turn update at a time per session, held across its model call."""
        return self._queues.setdefault(session_key, asyncio.Lock())

    def generation(self, session_key: str) -> int:
        """Moves on every clear; an update started under an older one is stale."""
        return self._generation.get(session_key, 0)

    def was_dropped(self, session_key: str, turn_at: str) -> bool:
        """Whether an undo has removed this turn since its update started."""
        return turn_at in self._dropped.get(session_key, ())

    def set_sink(self, sink: Sink | None) -> None:
        self._sink = sink

    def load(self, session_key: str) -> Board:
        return load_board(self.session_dir(session_key))

    def lineage(self, session_key: str, board: Board) -> dict[str, str]:
        return board_lineage(self.session_dir(session_key), board)

    async def write(self, session_key: str, board: Board) -> None:
        save_board(self.session_dir(session_key), board)
        sink = self._sink
        if sink is None:
            return
        payload = {
            "session_key": session_key,
            "board": board_to_dict(board),
            "lineage": self.lineage(session_key, board),
        }
        try:
            await sink(session_key, {"type": "tasks.board_updated", "payload": payload})
        except Exception:
            logger.opt(exception=True).warning("Task board update for {} could not be announced", session_key)

    async def reset(self, session_key: str) -> None:
        """Empty the board, as clearing the conversation does."""
        async with self.lock(session_key):
            self._generation[session_key] = self.generation(session_key) + 1
            self._shown.pop(session_key, None)
            save_notes(self.session_dir(session_key), [])
            await self.write(session_key, Board([], []))

    async def drop_turns(self, session_key: str, removed: set[str]) -> Board:
        """Drop what the board recorded for the turns an undo removed.

        An update still running for an undone turn finds the turn marked and
        writes nothing.
        """
        async with self.lock(session_key):
            self._dropped.setdefault(session_key, set()).update(removed)
            board = self.load(session_key)
            kept = drop_turns(board, removed)
            if board_to_dict(kept) != board_to_dict(board):
                await self.write(session_key, kept)
        return kept

    async def ignore_question(self, session_key: str, group_id: str, step_id: str, question: str) -> Board:
        """The reader sets one question aside; nothing is written when the step no longer lists it."""
        async with self.lock(session_key):
            board = self.load(session_key)
            after = ignore_question(board, group_id, step_id, question, now_ms=self.now_ms())
            if after is not board:
                await self.write(session_key, after)
                title = next(g.title for g in after.groups if g.id == group_id)
                add_note(self.session_dir(session_key), title, question)
        return after

    async def set_status(self, session_key: str, group_id: str, status: str) -> Board:
        """The reader's two actions: mark a group completed, or reopen it."""
        if status not in READER_STATUSES:
            raise UpdateRejectedError(f"a group can only be marked completed or reopened, not {status!r}")
        async with self.lock(session_key):
            before = self.load(session_key)
            board = set_group_status(before, group_id, status, now_ms=self.now_ms())
            await self.write(session_key, board)
            # Ignore on a group that named no question puts it down from waiting:
            # the same set-aside as one question's, for the whole of what it asked.
            was = next(g for g in before.groups if g.id == group_id)
            if was.status == "needs_you" and status == "idle":
                add_note(self.session_dir(session_key), was.title)
        return board

    async def move_on(self, session_key: str, group_id: str) -> None:
        """The reader's next message moves this group on; ``KeyError`` for a group the board lacks."""
        async with self.lock(session_key):
            group = next((g for g in self.load(session_key).groups if g.id == group_id), None)
            if group is None:
                raise KeyError(group_id)
            add_move_on(self.session_dir(session_key), group.id, group.title, group.summary)

    def turn_notes(self, session_key: str) -> list[str]:
        """The reader's set-asides the turn being assembled is told of, remembered as
        shown until that turn acknowledges them; read again within it, they stay."""
        notes = load_notes(self.session_dir(session_key))
        self._shown[session_key] = max((n.seq for n in notes), default=0)
        return [note_line(n) for n in notes]

    async def ack_turn_notes(self, session_key: str) -> None:
        """A turn that showed the notes has run: drop those, keep any made since. The
        turn awaits this after its reply, so a write the disk refuses is logged, not
        raised: the notes stay, and the next turn is shown them again."""
        shown = self._shown.pop(session_key, 0)
        if not shown:
            return
        async with self.lock(session_key):
            directory = self.session_dir(session_key)
            try:
                save_notes(directory, [n for n in load_notes(directory) if n.seq > shown])
            except OSError as exc:
                logger.warning("Task board notes for {} were not acknowledged: {}", session_key, exc)
