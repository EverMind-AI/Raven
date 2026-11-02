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

from raven.agent.task_board import files
from raven.agent.task_board.board import Board, ProgressStep, TaskGroup
from raven.agent.task_board.notes import NOTES_FILENAME, Note, load_notes, save_notes
from raven.agent.task_board.store import BOARD_FILENAME, copy_board, load_board, reset_board, save_board


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


def _hold_the_board(monkeypatch, times: int) -> None:
    """A reader holds the board: replacing it away is refused ``times`` times."""
    real = os.replace
    left = {"times": times}

    def replace(src, dst, *args, **kwargs):
        if Path(str(src)).name == BOARD_FILENAME and left["times"] > 0:
            left["times"] -= 1
            raise PermissionError(13, "Access is denied", str(src))
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def test_an_unreadable_board_is_set_aside_once_a_reader_lets_go_of_it(tmp_path, monkeypatch):
    (tmp_path / BOARD_FILENAME).write_text("{not json", encoding="utf-8")
    _hold_the_board(monkeypatch, 2)

    assert load_board(tmp_path).groups == []
    assert [p.read_text(encoding="utf-8") for p in tmp_path.glob(f"{BOARD_FILENAME}.unreadable-*")] == ["{not json"]


def test_an_unreadable_board_held_throughout_stays_and_reads_as_empty(tmp_path, monkeypatch):
    (tmp_path / BOARD_FILENAME).write_text("{not json", encoding="utf-8")
    _hold_the_board(monkeypatch, 99)

    assert load_board(tmp_path).groups == []
    assert (tmp_path / BOARD_FILENAME).read_text(encoding="utf-8") == "{not json"


def _hold_open(monkeypatch, path: Path, times: int) -> None:
    """A reader holds ``path``: opening it, by any route, is refused ``times`` times."""
    import builtins
    import io

    left = {"times": times}

    def refusing(opener):
        def open_(file, *args, **kwargs):
            if Path(str(file)) == path and left["times"] > 0:
                left["times"] -= 1
                raise PermissionError(13, "Access is denied", str(file))
            return opener(file, *args, **kwargs)

        return open_

    monkeypatch.setattr(builtins, "open", refusing(builtins.open))
    monkeypatch.setattr(io, "open", refusing(io.open))


def test_a_board_copy_waits_out_a_reader(tmp_path, monkeypatch):
    """A branch copies the board while a post-turn save may be replacing it."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    save_board(src, _board("Kept"))
    save_notes(src, [Note(1, "Kept")])
    _hold_open(monkeypatch, src / BOARD_FILENAME, 2)

    copy_board(src, dst)

    assert load_board(dst).groups[0].title == "Kept"
    assert [n.title for n in load_notes(dst)] == ["Kept"]


def test_a_board_copy_keeps_the_bytes_of_a_board_that_does_not_parse(tmp_path):
    """A branch must not fail on a board its source has not set aside yet; the copy is
    set aside on its own first read, as the source would be."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / BOARD_FILENAME).write_bytes(b"\xff{not utf-8")

    copy_board(src, dst)

    assert (dst / BOARD_FILENAME).read_bytes() == b"\xff{not utf-8"


def test_a_bytes_write_is_moved_into_place_past_a_reader(tmp_path, monkeypatch):
    """A copy lands like any other write: whole, beside the target, then moved in."""
    sources = _refuse_replace(monkeypatch, BOARD_FILENAME, 2)

    files.write_bytes(tmp_path / BOARD_FILENAME, b"\xff raw")

    assert (tmp_path / BOARD_FILENAME).read_bytes() == b"\xff raw"
    assert len(sources) == 3 and all(Path(s).name.endswith(".tmp") for s in sources)
    assert _leftovers(tmp_path) == []
