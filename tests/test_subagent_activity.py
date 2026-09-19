"""``raven.agent.subagent.activity``: the live index an instance's conversation
view reads is held by the turn answering, not by whichever lane registered last."""

from __future__ import annotations

import time

from raven.agent.subagent import activity

KEY = ("s1", "Raven-Research-NG", "raven-research-ng-b3b872")


def test_the_first_lane_to_register_keeps_the_instance_slot():
    with activity.collecting(instance=KEY) as answering:
        with activity.collecting(instance=KEY) as queued:
            assert activity.live_instance(*KEY) is answering, "a second lane never unseats the first"
            assert queued is not answering
        assert activity.live_instance(*KEY) is answering, "and its exit releases nothing it did not own"
    assert activity.live_instance(*KEY) is None


def test_watching_instance_registers_late_and_releases_only_its_own():
    with activity.collecting(live_key="spawn-1") as run:
        assert activity.live_instance(*KEY) is None, "a block opened without the instance is not indexed by it"
        with activity.watching_instance(run, KEY):
            assert activity.live_instance(*KEY) is run
        assert activity.live_instance(*KEY) is None
    with activity.collecting(instance=KEY) as answering:
        with activity.watching_instance(activity.RunActivity(), KEY):
            assert activity.live_instance(*KEY) is answering
        assert activity.live_instance(*KEY) is answering


def test_the_turn_starts_when_the_slot_is_taken_not_when_collection_opened():
    """A spawn builds its activity, then waits on ``hold_handle``. Only the lane
    that takes the lock reaches the slot, so the wait is not part of its turn --
    and a clock drawn from the build time opened at however long the spawn had
    queued behind the turn before it."""
    run = activity.RunActivity()
    assert run.turn_started_at_ms is None, "not answering an instance yet"
    queued_for_ms = 40
    time.sleep(queued_for_ms / 1000)

    with activity.watching_instance(run, KEY):
        assert run.turn_started_at_ms is not None
        # The stamp is the acquisition, so the wait is excluded. Compared as a
        # floor rather than an equality: the clock is the wall clock and the
        # sleep is a minimum, not a promise.
        assert run.turn_started_at_ms - run.started_at_ms >= queued_for_ms - 5


def test_a_lane_that_never_takes_the_slot_is_never_stamped():
    """Absent is what says an instance is answering nothing, so a lane that lost
    the race must not look like one that is working."""
    with activity.collecting(instance=KEY):
        loser = activity.RunActivity()
        with activity.watching_instance(loser, KEY):
            assert loser.turn_started_at_ms is None


def test_note_file_change_is_recorded_in_order_and_reaches_as_meta():
    with activity.collecting() as run:
        activity.note_file_change("a.py", "write", 3, 0, 42)
        activity.note_file_change("b.py", "edit", 1, 1, 10)

    assert run.files == [
        {"path": "a.py", "op": "write", "add": 3, "del": 0, "size": 42},
        {"path": "b.py", "op": "edit", "add": 1, "del": 1, "size": 10},
    ]
    assert run.as_meta()["files"] == run.files


def test_as_meta_omits_files_when_nothing_was_written():
    """Omitted rather than an empty list: a reader treats a missing key and an
    empty one alike here, and the manifest / spawn meta this feeds already
    drops every other empty field the same way."""
    with activity.collecting() as run:
        pass

    assert "files" not in run.as_meta()


def test_two_writes_of_one_path_fold_into_one_entry() -> None:
    """The record is one entry per path a writing tool touched, not one per
    call: two writes of the same file are the file's final state, once."""
    with activity.collecting() as run:
        activity.note_file_change("a.py", "write", 3, 0, 42)
        activity.note_file_change("a.py", "write", 1, 2, 20)

    assert run.files == [{"path": "a.py", "op": "write", "add": 4, "del": 2, "size": 20}]


def test_write_then_edit_of_one_path_folds_to_a_write() -> None:
    with activity.collecting() as run:
        activity.note_file_change("a.py", "write", 3, 0, 42)
        activity.note_file_change("a.py", "edit", 1, 2, 20)

    assert run.files == [{"path": "a.py", "op": "write", "add": 4, "del": 2, "size": 20}]


def test_edit_then_write_of_one_path_also_folds_to_a_write() -> None:
    """Write wins over edit regardless of which touch came first: a node that
    ever rewrote the path whole produced the file's true current content,
    while a patch against the pre-node baseline is not reconstructible."""
    with activity.collecting() as run:
        activity.note_file_change("a.py", "edit", 1, 1, 10)
        activity.note_file_change("a.py", "write", 3, 0, 42)

    assert run.files == [{"path": "a.py", "op": "write", "add": 4, "del": 1, "size": 42}]


def test_a_path_only_ever_edited_stays_an_edit() -> None:
    with activity.collecting() as run:
        activity.note_file_change("a.py", "edit", 1, 1, 10)
        activity.note_file_change("a.py", "edit", 2, 0, 15)

    assert run.files == [{"path": "a.py", "op": "edit", "add": 3, "del": 1, "size": 15}]


def test_interleaved_paths_keep_first_touch_order() -> None:
    with activity.collecting() as run:
        activity.note_file_change("a.py", "write", 1, 0, 5)
        activity.note_file_change("b.py", "write", 2, 0, 6)
        activity.note_file_change("a.py", "edit", 1, 1, 7)

    assert [f["path"] for f in run.files] == ["a.py", "b.py"]
    assert run.files[0] == {"path": "a.py", "op": "write", "add": 2, "del": 1, "size": 7}


def test_the_cap_gates_new_paths_not_a_repeat_touch_of_one_already_kept() -> None:
    """The cap now bounds distinct paths, not calls: a node that keeps
    touching a path already in the record is never refused for it, and a
    further new path past the cap still is."""
    with activity.collecting() as run:
        for i in range(activity._MAX_TOOL_CALLS):
            activity.note_file_change(f"f{i}.py", "write", 1, 0, 1)
        activity.note_file_change("f0.py", "edit", 1, 1, 2)
        activity.note_file_change("one_too_many.py", "write", 1, 0, 1)

    assert len(run.files) == activity._MAX_TOOL_CALLS
    assert run.files[0] == {"path": "f0.py", "op": "write", "add": 2, "del": 1, "size": 2}
    assert not any(f["path"] == "one_too_many.py" for f in run.files)


def test_note_file_change_needs_a_collector_and_a_path() -> None:
    from raven.agent.subagent import activity

    activity.note_file_change("stray.md", "write", 1, 0, 6)
    with activity.collecting() as did:
        activity.note_file_change("", "write", 1, 0, 0)
        activity.note_file_change("kept.md", "edit", 2, 1, 9)
    assert did.files == [{"path": "kept.md", "op": "edit", "add": 2, "del": 1, "size": 9}]
