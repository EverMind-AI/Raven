"""One host process's way to a session's Task Board: a lock per session, and the announcement after each write.

Both writers -- the post-turn call and the reader's own two actions over RPC --
take the same per-session lock and read the board inside it, so a write never
lands on a board another writer has moved since. Every write is announced as a
``tasks.board_updated`` event; an announcement that cannot be delivered costs the
client a refresh, never the write.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.task_board.board import Board, UpdateRejectedError, board_to_dict, drop_turns, set_group_status
from raven.agent.task_board.lineage import board_lineage
from raven.agent.task_board.store import load_board, save_board

if TYPE_CHECKING:
    from raven.session.manager import SessionManager

READER_STATUSES = ("completed", "active")

Sink = Callable[[str, dict[str, Any]], Awaitable[None]]


def _wall_ms() -> int:
    return int(time.time() * 1000)


class TaskBoardService:
    def __init__(self, sessions: SessionManager, *, now_ms: Callable[[], int] = _wall_ms) -> None:
        self._sessions = sessions
        self._locks: dict[str, asyncio.Lock] = {}
        self._sink: Sink | None = None
        self.now_ms = now_ms

    def session_dir(self, session_key: str) -> Path:
        return self._sessions.session_dir(session_key)

    def messages(self, session_key: str) -> list[dict[str, Any]]:
        session = self._sessions.peek(session_key)
        return list(session.messages) if session is not None else []

    def lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

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
            await self.write(session_key, Board([], []))

    async def drop_turns(self, session_key: str, removed: set[str]) -> Board:
        """Drop what the board recorded for the turns an undo removed.

        Under the lock, so an update still running for the turn being undone
        lands first and is dropped with the rest, rather than after.
        """
        async with self.lock(session_key):
            board = self.load(session_key)
            kept = drop_turns(board, removed)
            if board_to_dict(kept) != board_to_dict(board):
                await self.write(session_key, kept)
        return kept

    async def set_status(self, session_key: str, group_id: str, status: str) -> Board:
        """The reader's two actions: mark a group completed, or reopen it."""
        if status not in READER_STATUSES:
            raise UpdateRejectedError(f"a group can only be marked completed or reopened, not {status!r}")
        async with self.lock(session_key):
            board = set_group_status(self.load(session_key), group_id, status, now_ms=self.now_ms())
            await self.write(session_key, board)
        return board
