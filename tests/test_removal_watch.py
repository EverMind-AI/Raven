"""The per-run watch over files the run itself wrote.

A deletion has no tool call of its own, so what a run can say about one is that
a path it wrote is not there any more. These are the watch's own rules: what it
gives back for a path that vanished, what it forgets once it has reported it,
and how much of what it saw it is willing to hold while the run lasts.
"""

from __future__ import annotations

from pathlib import Path

from raven.agent.tools.removals import WATCHED_TEXT_MAX_CHARS, WATCHED_TOTAL_MAX_CHARS, RemovalWatch
from raven.contracts.tool import FileChange


def test_a_written_file_that_vanished_is_reported_with_what_it_held(tmp_path: Path) -> None:
    gone = tmp_path / "notes.md"
    gone.write_text("one\ntwo\n")
    watch = RemovalWatch()
    watch.note_write(FileChange(path=str(gone), after="one\ntwo\n"))
    gone.unlink()

    assert [(r.path, r.before) for r in watch.settle()] == [(str(gone), "one\ntwo\n")]
    assert watch.settle() == []


def test_a_file_still_there_is_not_reported(tmp_path: Path) -> None:
    kept = tmp_path / "kept.md"
    kept.write_text("here\n")
    watch = RemovalWatch()
    watch.note_write(FileChange(path=str(kept), after="here\n"))

    assert watch.settle() == []


def test_one_oversized_write_is_watched_without_its_text(tmp_path: Path) -> None:
    gone = tmp_path / "big.md"
    gone.write_text("x")
    watch = RemovalWatch()
    watch.note_write(FileChange(path=str(gone), after="x" * (WATCHED_TEXT_MAX_CHARS + 1)))
    gone.unlink()

    assert [(r.path, r.before) for r in watch.settle()] == [(str(gone), None)]


def test_the_watch_stops_holding_bodies_once_the_run_has_written_too_much(tmp_path: Path) -> None:
    """A run that writes hundreds of files must not keep every one of them whole
    for as long as it lasts. The path is still watched past the budget -- a
    removal without its text is the degradation the payload already carries --
    so nothing that went missing goes unreported."""
    watch = RemovalWatch()
    body = "y" * WATCHED_TEXT_MAX_CHARS
    written = []
    for index in range(WATCHED_TOTAL_MAX_CHARS // WATCHED_TEXT_MAX_CHARS + 2):
        path = tmp_path / f"f{index}.md"
        path.write_text("y")
        watch.note_write(FileChange(path=str(path), after=body))
        written.append(path)
    for path in written:
        path.unlink()

    removals = watch.settle()
    assert [r.path for r in removals] == [str(p) for p in written]
    held = [r for r in removals if r.before is not None]
    assert len(held) * WATCHED_TEXT_MAX_CHARS <= WATCHED_TOTAL_MAX_CHARS
    assert removals[-1].before is None


def test_rewriting_a_path_gives_its_room_back(tmp_path: Path) -> None:
    """The budget is what the watch holds, not what the run ever wrote: a second
    write of the same path replaces the first rather than adding to it."""
    watch = RemovalWatch()
    body = "z" * WATCHED_TEXT_MAX_CHARS
    gone = tmp_path / "one.md"
    gone.write_text("z")
    for _ in range(WATCHED_TOTAL_MAX_CHARS // WATCHED_TEXT_MAX_CHARS + 2):
        watch.note_write(FileChange(path=str(gone), after=body))
    gone.unlink()

    assert [(r.path, r.before) for r in watch.settle()] == [(str(gone), body)]
