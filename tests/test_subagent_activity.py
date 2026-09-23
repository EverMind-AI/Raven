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


def test_a_settled_account_is_kept_until_its_run_forgets_it():
    """A node's account outlives its collecting block only through this index:
    the manifest that carries it is written when the whole run ends."""
    meta = {"tokens_in": 7, "tool_calls": ["read_file"]}
    activity.record_settled("dag:r1:a", meta)
    meta["tokens_in"] = 99
    assert activity.settled("dag:r1:a") == {"tokens_in": 7, "tool_calls": ["read_file"]}, (
        "a copy, not the runner's dict"
    )
    activity.forget_settled(["dag:r1:a", "dag:r1:never-recorded"])
    assert activity.settled("dag:r1:a") is None


def test_a_created_file_stays_created_when_it_is_later_written_or_edited() -> None:
    """``add`` is what the run did to the path: a file this run made is a
    creation however many times it was then rewritten, which is what git shows
    for the same range of commits."""
    with activity.collecting() as run:
        activity.note_file_change("a.py", "add", 3, 0, 42)
        activity.note_file_change("a.py", "write", 1, 2, 20)
        activity.note_file_change("a.py", "edit", 2, 1, 30)

    assert run.files == [{"path": "a.py", "op": "add", "add": 6, "del": 3, "size": 30}]


def test_a_removal_replaces_a_write_and_takes_its_own_counts() -> None:
    """What the run did to that path is remove it; the lines it wrote on the way
    are in no file a reader can open, so they are not counted as additions."""
    with activity.collecting() as run:
        activity.note_file_change("a.py", "write", 4, 1, 42)
        activity.note_file_change("a.py", "delete", 0, 6, None)

    assert run.files == [{"path": "a.py", "op": "delete", "add": 0, "del": 6, "size": None}]


def test_a_removal_replaces_an_edit_too() -> None:
    with activity.collecting() as run:
        activity.note_file_change("a.py", "edit", 1, 1, 10)
        activity.note_file_change("a.py", "delete", 0, 3, None)

    assert run.files == [{"path": "a.py", "op": "delete", "add": 0, "del": 3, "size": None}]


def test_a_file_created_and_removed_in_one_run_leaves_no_entry() -> None:
    """Net nothing, and git shows the same nothing for a file born and deleted
    inside one range. An entry here would put a row in the panel for a file that
    never existed before the run and does not exist after it."""
    with activity.collecting() as run:
        activity.note_file_change("kept.py", "write", 1, 0, 5)
        activity.note_file_change("scratch.py", "add", 3, 0, 42)
        activity.note_file_change("scratch.py", "delete", 0, 3, None)

    assert run.files == [{"path": "kept.py", "op": "write", "add": 1, "del": 0, "size": 5}]


def test_a_path_written_again_after_being_removed_is_that_write() -> None:
    """The path exists again, and what is in it was written after the deletion,
    so neither the op nor the counts of the removal survive."""
    with activity.collecting() as run:
        activity.note_file_change("a.py", "edit", 1, 1, 10)
        activity.note_file_change("a.py", "delete", 0, 4, None)
        activity.note_file_change("a.py", "write", 2, 0, 12)

    assert run.files == [{"path": "a.py", "op": "write", "add": 2, "del": 0, "size": 12}]


def test_a_path_created_again_after_being_removed_reads_as_a_rewrite() -> None:
    """``write_file`` calls it a creation because the run's own deletion left
    nothing to replace, but the path was there before the run -- the range
    rewrote it, over the lines the deletion took out."""
    with activity.collecting() as run:
        activity.note_file_change("a.py", "write", 1, 1, 10)
        activity.note_file_change("a.py", "delete", 0, 2, None)
        activity.note_file_change("a.py", "add", 5, 0, 20)

    assert run.files == [{"path": "a.py", "op": "write", "add": 5, "del": 2, "size": 20}]


def test_a_path_removed_again_after_being_put_back_is_still_a_removal() -> None:
    """The run deleted a file that was there before it, and nothing it wrote in
    between survives. Read as a creation, the entry would have cancelled itself
    away here and the run would report nothing for that path at all."""
    with activity.collecting() as run:
        activity.note_file_change("a.py", "delete", 0, 10, None)
        activity.note_file_change("a.py", "add", 3, 0, 20)
        activity.note_file_change("a.py", "delete", 0, 3, None)

    assert run.files == [{"path": "a.py", "op": "delete", "add": 0, "del": 3, "size": None}]


def test_a_pre_fold_record_folds_the_same_way_through_the_tasks_reader() -> None:
    """``tasks.list`` re-folds whatever is on disk, so a record written one
    entry per call must come out of that reader exactly as the recorder would
    have folded it -- including the created-then-removed path that leaves
    nothing behind."""
    from raven.rpc.methods.tasks import _files_of

    stored = {
        "files": [
            {"path": "a.py", "op": "add", "add": 3, "del": 0, "size": 42},
            {"path": "a.py", "op": "edit", "add": 1, "del": 1, "size": 40},
            {"path": "b.py", "op": "write", "add": 2, "del": 0, "size": 9},
            {"path": "b.py", "op": "delete", "add": 0, "del": 2, "size": None},
            {"path": "c.py", "op": "add", "add": 1, "del": 0, "size": 3},
            {"path": "c.py", "op": "delete", "add": 0, "del": 1, "size": None},
        ]
    }

    assert _files_of(stored) == [
        {"path": "a.py", "op": "add", "add": 4, "del": 1, "size": 40},
        {"path": "b.py", "op": "delete", "add": 0, "del": 2, "size": None},
    ]


def test_count_line_changes_reads_a_file_that_did_not_exist_as_all_additions() -> None:
    """``before is None`` is the only record that the write created the file,
    and a creation has nothing to have removed."""
    assert activity.count_line_changes(None, "one\ntwo\n") == (2, 0)
    assert activity.count_line_changes("", "one\ntwo\n") == (2, 0)


def test_count_line_changes_counts_only_the_lines_that_moved() -> None:
    assert activity.count_line_changes("keep\nold\n", "keep\nnew\nextra\n") == (2, 1)
    assert activity.count_line_changes("same\n", "same\n") == (0, 0)


def test_workspace_relative_anchors_a_path_under_the_workspace(tmp_path) -> None:
    inside = tmp_path / "work" / "notes.md"
    inside.parent.mkdir()
    inside.write_text("x")

    assert activity.workspace_relative(str(inside), tmp_path) == "work/notes.md"


def test_workspace_relative_leaves_a_path_outside_it_alone(tmp_path) -> None:
    """Absolute is the honest answer for a file the panel cannot anchor; a
    workspace nobody knows leaves the path as it came."""
    outside = "/etc/hosts"
    assert activity.workspace_relative(outside, tmp_path / "work") == outside
    assert activity.workspace_relative(outside, None) == outside


def test_a_snapshot_records_a_creation_a_rewrite_and_a_removal(tmp_path) -> None:
    """What a command did, read off the directory: the creation counts its
    lines, the rewrite has none to count (the listing never held the old
    content), and the removal has no size because the file is gone."""
    from raven.agent.tools import snapshot

    (tmp_path / "kept.md").write_text("one\n")
    (tmp_path / "gone.md").write_text("bye\n")
    before = snapshot.take(tmp_path)
    (tmp_path / "made.md").write_text("a\nb\n")
    (tmp_path / "kept.md").write_text("one\ntwo\n")
    (tmp_path / "gone.md").unlink()

    with activity.collecting() as run:
        activity.record_snapshot_changes(before, snapshot.take(tmp_path), tmp_path)

    assert run.files == [
        {"path": "made.md", "op": "add", "add": 2, "del": 0, "size": 4},
        {"path": "kept.md", "op": "write", "add": 0, "del": 0, "size": 8},
        {"path": "gone.md", "op": "delete", "add": 0, "del": 0, "size": None},
    ]


def test_a_path_the_call_already_accounted_for_is_not_recorded_twice(tmp_path) -> None:
    """The tool result and the listing see the same removal. Recorded from both,
    a node that deleted one file would have its deletion counted twice -- and
    the listing's copy carries neither the lines the file held nor its op."""
    from raven.agent.tools import snapshot

    (tmp_path / "gone.md").write_text("bye\n")
    before = snapshot.take(tmp_path)
    (tmp_path / "gone.md").unlink()
    (tmp_path / "made.md").write_text("a\n")

    with activity.collecting() as run:
        activity.record_snapshot_changes(before, snapshot.take(tmp_path), tmp_path, already=[str(tmp_path / "gone.md")])

    assert [entry["path"] for entry in run.files] == ["made.md"]


def test_a_snapshot_that_never_happened_records_nothing(tmp_path) -> None:
    from raven.agent.tools import snapshot

    with activity.collecting() as run:
        activity.record_snapshot_changes(None, snapshot.take(tmp_path), tmp_path)
        activity.record_snapshot_changes(snapshot.take(tmp_path), None, tmp_path)

    assert run.files == []


def test_a_snapshot_can_be_recorded_into_a_run_it_is_not_running_inside(tmp_path) -> None:
    """The acp collector is called from the connection's read loop, whose
    ContextVar predates the run -- so the run it writes to has to be the one it
    was handed, not whatever is current."""
    from raven.agent.tools import snapshot

    before = snapshot.take(tmp_path)
    (tmp_path / "made.md").write_text("a\n")
    theirs = activity.RunActivity()

    with activity.collecting() as current:
        activity.record_snapshot_changes(before, snapshot.take(tmp_path), tmp_path, run=theirs)

    assert current.files == []
    assert [entry["path"] for entry in theirs.files] == ["made.md"]
