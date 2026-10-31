"""What the store on disk promises a reader: one bad file is one bad file."""

from __future__ import annotations

import json
from pathlib import Path

from raven.stint.record import STATUSES, StintRecord, StintStore

CATALOG = Path(__file__).resolve().parent.parent / "i18n" / "messages.json"


def _written(store: StintStore, stint_id: str) -> Path:
    return store.write(StintRecord(stint_id=stint_id, playbook="rounds", spec={}, workdir="/tmp"))


def _rewritten(path: Path, **changes: object) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_round_key_this_build_does_not_know_is_dropped(tmp_path: Path) -> None:
    """A stint written by a newer build stays readable by an older one.

    `StintRecord` already dropped its own unknown keys; the rounds inside it were
    splatted straight into `RoundRecord`, so one added field raised TypeError --
    and out of `read`, which took `list` down with it.
    """
    store = StintStore(tmp_path)
    _rewritten(
        _written(store, "stint-new"),
        rounds=[{"index": 1, "run_id": "r1", "what_the_next_build_added": ["a", "b"]}],
        a_field_from_the_future=7,
    )

    record = store.read("stint-new")

    assert record is not None
    entry = record.round(1)
    assert entry is not None and entry.run_id == "r1"


def test_a_round_that_is_not_an_entry_at_all_is_passed_over(tmp_path: Path) -> None:
    store = StintStore(tmp_path)
    _rewritten(_written(store, "stint-odd"), rounds=["not an entry", {"index": 2, "run_id": "r2"}])

    record = store.read("stint-odd")

    assert record is not None
    assert [entry.index for entry in record.rounds] == [2]


def test_one_unreadable_stint_does_not_take_the_listing_with_it(tmp_path: Path, monkeypatch) -> None:
    """`list` answers for every other stint, or a `stop` cannot find its target."""
    store = StintStore(tmp_path)
    _written(store, "stint-good")
    _written(store, "stint-bad")

    original = StintRecord.from_dict

    def refuse_one(data: dict) -> StintRecord:
        if data.get("stint_id") == "stint-bad":
            raise TypeError("a shape this build has never seen")
        return original(data)

    monkeypatch.setattr(StintRecord, "from_dict", staticmethod(refuse_one))

    assert [record.stint_id for record in store.list()] == ["stint-good"]
    assert store.read("stint-bad") is None


def test_touching_a_stint_moves_only_its_stamp(tmp_path: Path) -> None:
    """A beat is a stamp, not a whole-record rewrite: moving the stamp must not
    write back a snapshot that undoes a `pause` or an `extend` that landed
    between the beat's read and its write."""
    store = StintStore(tmp_path)
    path = _written(store, "stint-live")
    before = json.loads(path.read_text(encoding="utf-8"))
    before["holder_pid"] = 1234
    before["touched_at_ms"] = 1
    path.write_text(json.dumps(before), encoding="utf-8")

    assert store.touch("stint-live", holder_pid=1234) is True

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["touched_at_ms"] > 1
    assert {key: value for key, value in after.items() if key != "touched_at_ms"} == {
        key: value for key, value in before.items() if key != "touched_at_ms"
    }


def test_touching_does_nothing_for_a_stint_that_ended_or_is_held_elsewhere(tmp_path: Path) -> None:
    store = StintStore(tmp_path)
    path = _written(store, "stint-ended")
    ended = json.loads(path.read_text(encoding="utf-8"))
    ended.update(status="stopped", touched_at_ms=1, holder_pid=1234)
    path.write_text(json.dumps(ended), encoding="utf-8")

    assert store.touch("stint-ended", holder_pid=1234) is False
    assert json.loads(path.read_text(encoding="utf-8"))["touched_at_ms"] == 1

    path = _written(store, "stint-other")
    other = json.loads(path.read_text(encoding="utf-8"))
    other.update(holder_pid=999, touched_at_ms=1)
    path.write_text(json.dumps(other), encoding="utf-8")

    assert store.touch("stint-other", holder_pid=1234) is False
    assert json.loads(path.read_text(encoding="utf-8"))["touched_at_ms"] == 1


def test_every_status_a_stint_can_be_in_has_a_word_for_a_reader() -> None:
    """The pill asks the catalog for `gui.pb.stint_status_<status>` using the
    record's own vocabulary, and a miss falls back to the key. `finished` -- the
    ordinary end of a run -- had no message, so a completed run was labelled
    `gui.pb.stint_status_finished` on its card and on its detail header.
    """
    messages = json.loads(CATALOG.read_text(encoding="utf-8"))["ui"]

    missing = [status for status in STATUSES if f"gui.pb.stint_status_{status}" not in messages]

    assert not missing, f"no message for {missing}"
