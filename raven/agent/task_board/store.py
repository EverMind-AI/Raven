"""Where a session's Task Board is kept: one JSON file in the session's directory.

Written whole and moved into place, so a reader in another process never sees
half a board; a file that does not parse reads as an empty board, with a warning,
rather than taking the conversation down with it.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from loguru import logger

from raven.agent.task_board.board import Board, board_from_dict, board_to_dict
from raven.agent.task_board.notes import NOTES_FILENAME

BOARD_FILENAME = "task_board.json"


def load_board(session_dir: Path) -> Board:
    path = Path(session_dir) / BOARD_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return board_from_dict(data)
    except FileNotFoundError:
        return Board([], [])
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning("Task board {} is unreadable; starting from an empty board: {}", path, exc)
        return Board([], [])


def save_board(session_dir: Path, board: Board) -> None:
    directory = Path(session_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / BOARD_FILENAME
    tmp = directory / f"{BOARD_FILENAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(board_to_dict(board, revisions=True), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def reset_board(session_dir: Path) -> None:
    (Path(session_dir) / BOARD_FILENAME).unlink(missing_ok=True)
    (Path(session_dir) / NOTES_FILENAME).unlink(missing_ok=True)


def copy_board(src: Path, dst: Path) -> None:
    source = Path(src) / BOARD_FILENAME
    if not source.is_file():
        return
    Path(dst).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, Path(dst) / BOARD_FILENAME)
    if (notes := Path(src) / NOTES_FILENAME).is_file():
        shutil.copyfile(notes, Path(dst) / NOTES_FILENAME)
