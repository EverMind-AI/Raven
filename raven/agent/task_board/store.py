"""Where a session's Task Board is kept: one JSON file in the session's directory.

Written whole and moved into place, so a reader in another process never sees
half a board. A file that does not parse reads as an empty board rather than taking
the conversation down with it, and is set aside first, under a name of its own: the
next write would otherwise replace the only copy of it. One that cannot be opened at
all may have nothing wrong with it, and is left where it is.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from loguru import logger

from raven.agent.task_board import files
from raven.agent.task_board.board import Board, board_from_dict, board_to_dict
from raven.agent.task_board.notes import NOTES_FILENAME

BOARD_FILENAME = "task_board.json"
EMBEDDINGS_FILENAME = "task_board_embeddings.sqlite"


def load_board(session_dir: Path) -> Board:
    path = Path(session_dir) / BOARD_FILENAME
    try:
        data = json.loads(files.read_text(path))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return board_from_dict(data)
    except FileNotFoundError:
        return Board([], [])
    except OSError as exc:
        logger.warning("Task board {} could not be read; starting from an empty board: {}", path, exc)
        return Board([], [])
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        aside = path.with_name(f"{path.name}.unreadable-{int(time.time() * 1000)}")
        try:
            files.move(path, aside)
        except OSError as moving:
            logger.warning("Task board {} is unreadable ({}) and could not be set aside: {}", path, exc, moving)
        else:
            logger.warning(
                "Task board {} is unreadable ({}); set aside as {}, starting from an empty board", path, exc, aside.name
            )
        return Board([], [])


def save_board(session_dir: Path, board: Board) -> None:
    text = json.dumps(board_to_dict(board, revisions=True), ensure_ascii=False, indent=1)
    files.write_text(Path(session_dir) / BOARD_FILENAME, text)


def _clear_vectors(path: Path) -> None:
    connection = sqlite3.connect(path, timeout=1.0)
    try:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vec'").fetchone()
        if table is not None:
            connection.execute("DELETE FROM vec")
            connection.commit()
    finally:
        connection.close()


def remove_embeddings(session_dir: Path) -> None:
    path = Path(session_dir) / EMBEDDINGS_FILENAME
    try:
        files.remove(path)
    except OSError as exc:
        # Windows refuses to unlink a file another handle holds open; clearing the rows
        # is what the next recall needs, and the file can go at the next clear.
        if not path.is_file():
            return
        try:
            _clear_vectors(path)
        except sqlite3.Error as clearing:
            logger.warning(
                "Task board recall cache {} stays with its rows: {}; clearing failed: {}", path, exc, clearing
            )


def reset_board(session_dir: Path) -> None:
    files.remove(Path(session_dir) / BOARD_FILENAME)
    files.remove(Path(session_dir) / NOTES_FILENAME)
    remove_embeddings(session_dir)


def copy_board(src: Path, dst: Path) -> None:
    source = Path(src) / BOARD_FILENAME
    if not source.is_file():
        return
    files.write_bytes(Path(dst) / BOARD_FILENAME, files.read_bytes(source))
    if (notes := Path(src) / NOTES_FILENAME).is_file():
        files.write_bytes(Path(dst) / NOTES_FILENAME, files.read_bytes(notes))
