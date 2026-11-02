"""What the reader did on the Task Board between two turns, told to the host at the next one.

A question the reader sets aside leaves its step at once (``board.ignore_question``),
but the reply that asked it is still in the host's history, and nothing there says
it was declined: the host would ask it again, and the post-turn update would put it
back on the board. A Move on names the matter the reader's next message is about,
which the message itself may not say. So each is kept as a note in the session's
directory, shown in the runtime context of every turn until one that showed it has
run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger

from raven.agent.task_board import files

NOTES_FILENAME = "task_board_notes.json"


@dataclass(frozen=True)
class Note:
    """A set-aside -- a question, by its matter's title, or with no question the whole
    of what the matter asked -- or a move-on, naming the matter by id and summary."""

    seq: int
    title: str
    question: str | None = None
    kind: str = "set_aside"
    group: str | None = None
    summary: str = ""


def load_notes(session_dir: Path) -> list[Note]:
    path = Path(session_dir) / NOTES_FILENAME
    try:
        data = json.loads(files.read_text(path))
        return [
            Note(
                int(n["seq"]),
                str(n["title"]),
                n.get("question"),
                str(n.get("kind") or "set_aside"),
                n.get("group"),
                str(n.get("summary") or ""),
            )
            for n in data
        ]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.warning("Task board notes {} are unreadable; dropping them: {}", path, exc)
        return []


def save_notes(session_dir: Path, notes: list[Note]) -> None:
    path = Path(session_dir) / NOTES_FILENAME
    if not notes:
        files.remove(path)
        return
    files.write_text(path, json.dumps([asdict(n) for n in notes], ensure_ascii=False))


def add_note(session_dir: Path, title: str, question: str | None = None) -> None:
    notes = load_notes(session_dir)
    save_notes(session_dir, [*notes, Note(max((n.seq for n in notes), default=0) + 1, title, question)])


def add_move_on(session_dir: Path, group: str, title: str, summary: str) -> None:
    """A message is about one matter: a later move-on replaces one not yet acknowledged."""
    notes = load_notes(session_dir)
    seq = max((n.seq for n in notes), default=0) + 1
    kept = [n for n in notes if n.kind != "move_on"]
    save_notes(session_dir, [*kept, Note(seq, title, kind="move_on", group=group, summary=summary)])


def note_line(note: Note) -> str:
    """One note as a runtime-context line. Kept to a single line: the stored turn drops
    the runtime block up to its first blank line, and anything past one would be kept
    as the user's own words."""
    title = " ".join(note.title.split())
    if note.kind == "move_on":
        line = f'Task board: the user\'s message moves on "{title}" ({note.group})'
        summary = " ".join(note.summary.split())
        return f"{line}; where it stands: {summary}" if summary else f"{line}."
    if note.question is None:
        return f'Task board: the user set aside what "{title}" asked, without answering it; it is declined, not open.'
    question = " ".join(note.question.split())
    return f'Task board: the user set aside "{question}" ({title}) without answering it; it is declined, not open.'
