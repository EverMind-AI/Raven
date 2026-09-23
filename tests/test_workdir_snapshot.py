"""``raven.agent.tools.snapshot``: what a listing of a working directory sees,
and what it refuses to see.

The listing is the only account of a file a command touched, so its blind spots
are the record's: a tree it skips is a change nobody hears about, and a tree it
walks too far is a run reporting thousands of files it did not author.
"""

from __future__ import annotations

import os
from pathlib import Path

from raven.agent.tools import snapshot


def test_a_created_a_modified_and_a_deleted_file_are_each_seen(tmp_path: Path) -> None:
    kept = tmp_path / "kept.md"
    kept.write_text("same\n")
    doomed = tmp_path / "doomed.md"
    doomed.write_text("bye\n")
    changed = tmp_path / "changed.md"
    changed.write_text("one\n")

    before = snapshot.take(tmp_path)
    (tmp_path / "made.md").write_text("new\n")
    changed.write_text("one\ntwo\n")
    doomed.unlink()

    created, modified, deleted = snapshot.diff(before, snapshot.take(tmp_path))
    assert created == [str(tmp_path / "made.md")]
    assert modified == [str(changed)]
    assert deleted == [str(doomed)]
    assert str(kept) not in created + modified + deleted


def test_a_file_whose_size_did_not_change_is_still_modified(tmp_path: Path) -> None:
    """Same length, different content: without the mtime the listing would call
    a rewritten file unchanged, which is the common shape of an edit."""
    same_size = tmp_path / "notes.md"
    same_size.write_text("aaa\n")
    before = snapshot.take(tmp_path)
    assert before is not None
    same_size.write_text("bbb\n")
    # Stamped after the rewrite, not before it: the write takes the clock's
    # current time, which on a coarse filesystem clock is the baseline's again.
    later = before[str(same_size)][1] + 1_000_000_000
    os.utime(same_size, ns=(later, later))

    _, modified, _ = snapshot.diff(before, snapshot.take(tmp_path))
    assert modified == [str(same_size)]


def test_the_skipped_directories_are_never_walked(tmp_path: Path) -> None:
    """A commit or an install is machinery, not the run's work."""
    for name in snapshot.SKIP_DIRS:
        (tmp_path / name).mkdir()
        (tmp_path / name / "inside.txt").write_text("x")
    (tmp_path / "mine.txt").write_text("x")

    listing = snapshot.take(tmp_path)
    assert listing is not None
    assert list(listing) == [str(tmp_path / "mine.txt")]


def test_the_agents_own_directory_inside_the_tree_is_never_walked(tmp_path: Path) -> None:
    """Named rather than left to the loop above, because this one is inside the
    directory a command runs in: the checkpoint keeps a shadow git repo at
    ``<workdir>/.raven/shadow.git`` and commits into it at every turn's end, so
    a second turn sharing the directory would otherwise land objects and refs
    in the middle of this command's listing and be reported as its work."""
    shadow = tmp_path / ".raven" / "shadow.git" / "objects" / "ab"
    shadow.mkdir(parents=True)
    (shadow / "cdef").write_bytes(b"x")
    (tmp_path / ".raven" / "NOTICE.txt").write_text("x")
    (tmp_path / "mine.txt").write_text("x")

    listing = snapshot.take(tmp_path)
    assert listing is not None
    assert list(listing) == [str(tmp_path / "mine.txt")]


def test_a_tree_past_the_ceiling_has_no_listing_at_all(tmp_path: Path, monkeypatch) -> None:
    """Not a partial one: a listing that stopped half way reads, on the next
    comparison, as a run that deleted everything the walk never reached."""
    monkeypatch.setattr(snapshot, "MAX_ENTRIES", 2)
    for index in range(3):
        (tmp_path / f"f{index}.txt").write_text("x")

    assert snapshot.take(tmp_path) is None


def test_a_symlink_is_not_one_of_the_files(tmp_path: Path) -> None:
    """A link is not a file this run wrote, and following one describes a tree
    the run was never pointed at."""
    real = tmp_path / "real.txt"
    real.write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.txt").write_text("y")
    (tmp_path / "link.txt").symlink_to(real)
    (tmp_path / "linked_dir").symlink_to(outside, target_is_directory=True)

    listing = snapshot.take(tmp_path)
    assert listing is not None
    assert sorted(listing) == sorted([str(real), str(outside / "elsewhere.txt")])


def test_a_missing_side_is_no_comparison(tmp_path: Path) -> None:
    listing = snapshot.take(tmp_path)
    assert snapshot.diff(None, listing) == ([], [], [])
    assert snapshot.diff(listing, None) == ([], [], [])
    assert snapshot.diff(None, None) == ([], [], [])


def test_a_root_that_is_not_a_directory_has_no_listing(tmp_path: Path) -> None:
    """``None``, not an empty listing: empty against a real listing would read
    as a run that deleted the whole tree."""
    assert snapshot.take(tmp_path / "nowhere") is None
