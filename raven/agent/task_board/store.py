"""Where a session's Task Board is kept: one JSON file in the session's directory.

Written whole and moved into place, so a reader in another process never sees
half a board. A file that does not parse reads as an empty board rather than taking
the conversation down with it, and is set aside first, under a name of its own: the
next write would otherwise replace the only copy of it. One that cannot be opened at
all may have nothing wrong with it, and is left where it is.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from loguru import logger

from raven.agent.task_board import files
from raven.agent.task_board.board import Board, board_from_dict, board_to_dict
from raven.agent.task_board.notes import NOTES_FILENAME

BOARD_FILENAME = "task_board.json"


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


def reset_board(session_dir: Path) -> None:
    files.remove(Path(session_dir) / BOARD_FILENAME)
    files.remove(Path(session_dir) / NOTES_FILENAME)


def copy_board(src: Path, dst: Path) -> None:
    source = Path(src) / BOARD_FILENAME
    if not source.is_file():
        return
    files.write_bytes(Path(dst) / BOARD_FILENAME, files.read_bytes(source))
    if (notes := Path(src) / NOTES_FILENAME).is_file():
        files.write_bytes(Path(dst) / NOTES_FILENAME, files.read_bytes(notes))
