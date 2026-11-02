"""Where a session's Task Board is kept: one JSON file in the session's directory.

Written whole and moved into place, so a reader in another process never sees
half a board; a file that does not parse reads as an empty board, with a warning,
rather than taking the conversation down with it.
"""

from __future__ import annotations

import json
import shutil
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
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning("Task board {} is unreadable; starting from an empty board: {}", path, exc)
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
    Path(dst).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, Path(dst) / BOARD_FILENAME)
    if (notes := Path(src) / NOTES_FILENAME).is_file():
        shutil.copyfile(notes, Path(dst) / NOTES_FILENAME)
