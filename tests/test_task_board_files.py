"""The board's files on a host that refuses to replace, remove or open a file another handle holds.

Windows refuses `os.replace` and `unlink` on a file another handle has open, and
refuses an open of a file that is being replaced; the Task Board page reads every
conversation's board on a worker thread, so a write can meet a read in flight. The
refusals are simulated here at the calls themselves, so the tests run anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from raven.agent.task_board.board import Board, ProgressStep, TaskGroup
from raven.agent.task_board.notes import NOTES_FILENAME, Note, load_notes, save_notes
from raven.agent.task_board.store import BOARD_FILENAME, load_board, reset_board, save_board


def _board(title: str) -> Board:
    return Board(
        [TaskGroup("g_1", title, "where it stands", "idle", 1, 1, [ProgressStep("s_1", "T1", None, "Did", "x")])], []
    )


def _refused(times: int, name: str) -> tuple[dict, object]:
    left = {"times": times}

    def refuse(target: object) -> bool:
        if Path(str(target)).name == name and left["times"] > 0:
            left["times"] -= 1
            return True
        return False

    return left, refuse


def _refuse_replace(monkeypatch, name: str, times: int) -> list[str]:
    _, refuse = _refused(times, name)
    real = os.replace
    sources: list[str] = []

    def replace(src, dst, *args, **kwargs):
        if Path(str(dst)).name == name:
            sources.append(str(src))
        if refuse(dst):
            raise PermissionError(13, "Access is denied", str(dst))
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    return sources


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def test_a_board_write_waits_out_a_reader_holding_the_file(tmp_path, monkeypatch):
    save_board(tmp_path, _board("Before"))
    _refuse_replace(monkeypatch, BOARD_FILENAME, 2)

    save_board(tmp_path, _board("After"))

    assert load_board(tmp_path).groups[0].title == "After"
    assert _leftovers(tmp_path) == []


def test_a_board_write_refused_throughout_raises_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    save_board(tmp_path, _board("Before"))
    _refuse_replace(monkeypatch, BOARD_FILENAME, 99)

    with pytest.raises(PermissionError):
        save_board(tmp_path, _board("After"))

    assert _leftovers(tmp_path) == []
    assert load_board(tmp_path).groups[0].title == "Before"


def test_two_writes_in_one_process_never_share_a_temporary_file(tmp_path, monkeypatch):
    sources = _refuse_replace(monkeypatch, BOARD_FILENAME, 0)

    save_board(tmp_path, _board("One"))
    save_board(tmp_path, _board("Two"))

    assert len(sources) == 2 and sources[0] != sources[1]


def test_a_board_read_waits_out_a_replace_in_flight(tmp_path, monkeypatch):
    save_board(tmp_path, _board("Kept"))
    _, refuse = _refused(2, BOARD_FILENAME)
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        if refuse(self):
            raise PermissionError(13, "Access is denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert load_board(tmp_path).groups[0].title == "Kept"


def test_a_notes_write_waits_out_a_reader_holding_the_file(tmp_path, monkeypatch):
    save_notes(tmp_path, [Note(1, "Fix bug A", "Which fix?")])
    _refuse_replace(monkeypatch, NOTES_FILENAME, 2)

    save_notes(tmp_path, [Note(1, "Fix bug A", "Which fix?"), Note(2, "Fix bug A", "Clear it too?")])

    assert [n.question for n in load_notes(tmp_path)] == ["Which fix?", "Clear it too?"]
    assert _leftovers(tmp_path) == []


def test_emptying_the_notes_waits_out_a_reader_holding_the_file(tmp_path, monkeypatch):
    save_notes(tmp_path, [Note(1, "Fix bug A", "Which fix?")])
    _, refuse = _refused(2, NOTES_FILENAME)
    real = Path.unlink

    def unlink(self, *args, **kwargs):
        if refuse(self):
            raise PermissionError(13, "The process cannot access the file", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    save_notes(tmp_path, [])

    assert not (tmp_path / NOTES_FILENAME).exists()


def test_clearing_the_board_waits_out_a_reader_holding_the_file(tmp_path, monkeypatch):
    save_board(tmp_path, _board("Cleared"))
    _, refuse = _refused(2, BOARD_FILENAME)
    real = Path.unlink

    def unlink(self, *args, **kwargs):
        if refuse(self):
            raise PermissionError(13, "The process cannot access the file", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    reset_board(tmp_path)

    assert not (tmp_path / BOARD_FILENAME).exists()


def test_a_notes_read_waits_out_a_replace_in_flight(tmp_path, monkeypatch):
    """A refused read must not come back as no notes: the acknowledgement after a
    turn would then write that empty list back, and the notes would be gone."""
    save_notes(tmp_path, [Note(1, "Fix bug A", "Which fix?")])
    _, refuse = _refused(2, NOTES_FILENAME)
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        if refuse(self):
            raise PermissionError(13, "Access is denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert [n.question for n in load_notes(tmp_path)] == ["Which fix?"]
