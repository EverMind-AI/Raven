"""Turn selection: main-line turns, the dispatched-trace closure, and the cut session record."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from raven.trajectory import selection as sel

SESSION = "tui:s1"


@pytest.fixture
def state(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "traces"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(root))
    return root


@pytest.fixture
def utc(monkeypatch):
    """Naive session stamps read as local time; pin local time to UTC."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _ts(minute: int, second: int = 0) -> str:
    return f"2026-10-01T10:{minute:02d}:{second:02d}+00:00"


def _turn(
    trace: str,
    *,
    minute: int,
    end_minute: int | None = None,
    session: str = SESSION,
    preview: str = "",
    checkpoint: bool = False,
    dispatched_in: str | None = None,
    artifact: str | None = None,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {"session.key": session, "turn.in_progress": checkpoint}
    if preview:
        attrs["turn.input_preview"] = preview
    if dispatched_in:
        attrs["trace.dispatched_in_trace_id"] = dispatched_in
        attrs["trace.dispatched_by_span_id"] = "span-parent"
    if artifact:
        attrs["turn.input.artifact_path"] = artifact
    return {
        "traceId": trace,
        "spanId": f"span-{trace}",
        "name": "session.turn",
        "kind": "agent",
        "startTime": _ts(minute),
        "endTime": _ts(minute, 1) if checkpoint else _ts(end_minute if end_minute is not None else minute + 1),
        "attributes": attrs,
    }


def _child(trace: str, span_id: str, *, session: str = SESSION) -> dict[str, Any]:
    return {
        "traceId": trace,
        "spanId": span_id,
        "parentSpanId": f"span-{trace}",
        "name": "llm.call",
        "startTime": _ts(0, 30),
        "endTime": _ts(0, 40),
        "attributes": {"session.key": session},
    }


def _write(state: Path, *records: dict[str, Any] | bytes) -> None:
    with (state / "logs" / "audit-spans.log").open("ab") as handle:
        for record in records:
            handle.write(record if isinstance(record, bytes) else json.dumps(record).encode() + b"\n")


def _rewrite(state: Path, *records: dict[str, Any]) -> None:
    (state / "logs" / "audit-spans.log").write_bytes(b"")
    _write(state, *records)


def _input_artifact(state: Path, name: str, content: Any) -> str:
    path = state / "logs" / "audit-artifacts" / "turn.input" / "2026-10-01" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"content": content, "channel": "tui", "chat_id": "s1"}), encoding="utf-8")
    return str(path)


def _session_file(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "s1.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _meta(**extra: Any) -> dict[str, Any]:
    return {
        "_type": "metadata",
        "key": SESSION,
        "created_at": "2026-10-01T10:00:00",
        "metadata": {"title": "trip", **extra},
        "last_consolidated": 0,
    }


def _plan(*trace_ids: str, answering: bool = False) -> sel.SelectionPlan:
    return sel.plan_selection(SESSION, list(trace_ids), answering=answering)


# -- main-line turns ---------------------------------------------------------


def test_main_line_turns_leave_out_dispatched_and_foreign_turns(state):
    _write(
        state,
        _turn("trace-b", minute=5, preview="second"),
        _turn("trace-a", minute=1, preview="first"),
        _turn("trace-sub", minute=2, dispatched_in="trace-a"),
        _turn("trace-other", minute=3, session="tui:elsewhere"),
    )
    turns = sel.session_turns(SESSION, answering=False)
    assert [(t.trace_id, t.number, t.input_preview) for t in turns] == [
        ("trace-a", 1, "first"),
        ("trace-b", 2, "second"),
    ]
    assert all(t.main_line and not t.in_progress for t in turns)


def test_a_checkpoint_after_the_final_record_does_not_reopen_the_turn(state):
    _write(state, _turn("trace-a", minute=1, end_minute=4), _turn("trace-a", minute=1, checkpoint=True))
    [turn] = sel.session_turns(SESSION, answering=True)
    assert not turn.in_progress
    assert turn.end == _ts(4)


@pytest.mark.parametrize(
    ("answering", "latest", "expected"),
    [(True, True, True), (False, True, False), (True, False, False)],
    ids=["latest-while-answering", "latest-after-the-process-died", "stale-checkpoint-earlier"],
)
def test_in_progress_needs_the_latest_turn_a_checkpoint_and_an_answering_session(state, answering, latest, expected):
    if latest:
        _write(state, _turn("trace-a", minute=1), _turn("trace-b", minute=2, checkpoint=True))
        probe = "trace-b"
    else:
        _write(state, _turn("trace-a", minute=1, checkpoint=True), _turn("trace-b", minute=2))
        probe = "trace-a"
    turns = {t.trace_id: t for t in sel.session_turns(SESSION, answering=answering)}
    assert turns[probe].in_progress is expected
    assert turns[probe].end is None


def test_answering_must_be_stated(state):
    with pytest.raises(TypeError):
        sel.session_turns(SESSION)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        sel.plan_selection(SESSION, ["trace-a"])  # type: ignore[call-arg]


# -- planning ----------------------------------------------------------------


def test_an_empty_selection_is_refused(state):
    with pytest.raises(sel.SelectionError) as excinfo:
        sel.plan_selection(SESSION, [], answering=False)
    assert excinfo.value.why == sel.WHY_EMPTY and excinfo.value.invalid == ()


def test_every_invalid_turn_is_named_with_its_reason(state):
    _write(state, _turn("trace-a", minute=1), _turn("trace-b", minute=2, checkpoint=True))
    _write(state, _turn("trace-sub", minute=3, dispatched_in="trace-a"))
    with pytest.raises(sel.SelectionError) as excinfo:
        sel.plan_selection(SESSION, ["trace-a", "trace-sub", "trace-b", "trace-none"], answering=True)
    assert set(excinfo.value.invalid) == {
        ("trace-sub", sel.WHY_NOT_IN_SESSION),
        ("trace-b", sel.WHY_IN_PROGRESS),
        ("trace-none", sel.WHY_NOT_IN_SESSION),
    }


def test_the_closure_follows_dispatch_links_down_every_generation(state):
    _write(
        state,
        _turn("trace-a", minute=1),
        _turn("trace-b", minute=4),
        _turn("trace-sub", minute=2, session="subagent:x", dispatched_in="trace-a"),
        _turn("trace-grand", minute=3, session="subagent:y", dispatched_in="trace-sub"),
        _turn("trace-loop-1", minute=5, session="subagent:z", dispatched_in="trace-loop-2"),
        _turn("trace-loop-2", minute=5, session="subagent:z", dispatched_in="trace-loop-1"),
        _turn("trace-foreign", minute=6, session="subagent:w", dispatched_in="trace-elsewhere"),
    )
    plan = _plan("trace-b", "trace-a")
    assert plan.selected == ("trace-a", "trace-b")
    assert plan.dispatched == ("trace-grand", "trace-sub")
    assert [t.number for t in plan.turns] == [1, 2]
    assert plan.closure == {"trace-a", "trace-b", "trace-sub", "trace-grand"}


def test_a_dispatch_cycle_in_the_logs_does_not_hang_planning(state):
    """Each trace names one parent, so a cycle can never hang off a main-line
    turn; a corrupt pair pointing at each other must still not stall the walk."""
    _write(
        state,
        _turn("trace-a", minute=1),
        _turn("trace-sub", minute=2, session="subagent:s", dispatched_in="trace-a"),
        _turn("trace-x", minute=2, session="subagent:x", dispatched_in="trace-y"),
        _turn("trace-y", minute=2, session="subagent:y", dispatched_in="trace-x"),
        _turn("trace-self", minute=2, session="subagent:z", dispatched_in="trace-self"),
    )
    assert _plan("trace-a").dispatched == ("trace-sub",)


# -- collecting --------------------------------------------------------------


def test_collecting_returns_the_closure_in_write_order_with_checkpoints(state):
    _write(
        state,
        _turn("trace-a", minute=1, checkpoint=True),
        _child("trace-a", "span-a1"),
        _turn("trace-b", minute=4),
        _turn("trace-sub", minute=2, session="subagent:x", dispatched_in="trace-a"),
        _child("trace-sub", "span-s1", session="subagent:x"),
        _turn("trace-a", minute=1),
    )
    selection = sel.collect_selection(_plan("trace-a"))
    assert [(r["traceId"], r["spanId"]) for r in selection.spans] == [
        ("trace-a", "span-trace-a"),
        ("trace-a", "span-a1"),
        ("trace-sub", "span-trace-sub"),
        ("trace-sub", "span-s1"),
        ("trace-a", "span-trace-a"),
    ]


def test_collecting_takes_only_a_plan(state):
    with pytest.raises(TypeError):
        sel.collect_selection(["trace-a"])  # type: ignore[arg-type]


def test_a_turn_purged_between_the_two_scans_fails_the_collection(state):
    _write(state, _turn("trace-a", minute=1), _turn("trace-b", minute=2))
    plan = _plan("trace-a", "trace-b")
    _rewrite(state, _turn("trace-b", minute=2))
    with pytest.raises(LookupError, match="trace-a"):
        sel.collect_selection(plan)


def test_the_prefilter_keeps_every_line_that_can_match(state):
    multibyte = "café naïve über"
    _write(
        state,
        b"not json at all\n",
        b"\xff\xfe broken utf-8 trace-a\n",
        _turn("trace-a", minute=1, preview=multibyte),
        {**_child("trace-a", "span-a1"), "attributes": {"note": "trace-b and trace-a on one line"}},
        b"[1, 2, 3]\n",
    )
    [turn] = sel.session_turns(SESSION, answering=False)
    assert turn.input_preview == multibyte
    assert [r["spanId"] for r in sel.collect_selection(_plan("trace-a")).spans] == ["span-trace-a", "span-a1"]


def test_selection_leaves_no_trace_in_the_store(state):
    _write(state, _turn("trace-a", minute=1))
    sel.collect_selection(_plan("trace-a"))
    sel.traces_present(["trace-a"])
    assert sorted(p.name for p in state.iterdir()) == ["logs"]


def test_traces_present_names_only_the_ids_still_recorded(state):
    _write(state, _turn("trace-a", minute=1), _child("trace-b", "span-b1"))
    assert sel.traces_present(["trace-a", "trace-b", "trace-gone", ""]) == {"trace-a", "trace-b"}
    assert sel.traces_present([]) == set()


def test_only_regular_files_inside_the_artifact_store_are_accepted(state, tmp_path):
    inside = Path(_input_artifact(state, "ok", "hello"))
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    escape = state / "logs" / "audit-artifacts" / ".." / ".." / ".." / "outside.json"
    assert sel.store_artifact(str(inside), state) == inside.resolve()
    assert sel.store_artifact(str(inside.parent / "missing.json"), state) is None
    assert sel.store_artifact(str(outside), state) is None
    assert sel.store_artifact(str(escape), state) is None
    assert sel.store_artifact(str(inside.parent), state) is None
    assert sel.store_artifact(42, state) is None
    assert sel.store_artifact("", state) is None
    assert sel.store_artifact("bad\x00path", state) is None


# -- cutting the session record ----------------------------------------------


def _user(text: Any, stamp: str, **extra: Any) -> dict[str, Any]:
    return {"role": "user", "content": text, "timestamp": stamp, **extra}


def _assistant(text: str, stamp: str, **extra: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": text, "timestamp": stamp, **extra}


def test_marked_blocks_are_cut_to_the_selected_turns(state, tmp_path):
    _write(state, _turn("trace-a", minute=1), _turn("trace-b", minute=2), _turn("trace-c", minute=3))
    path = _session_file(
        tmp_path,
        [
            _meta(annotated_through=6),
            _user("one", "2026-10-01T10:01:00", trace_id="trace-a"),
            _assistant("one done", "2026-10-01T10:01:30"),
            _user("two", "2026-10-01T10:02:00", trace_id="trace-b"),
            _assistant("two done", "2026-10-01T10:02:30"),
            {**_meta(annotated_through=6), "last_consolidated": 4},
            _user("three", "2026-10-01T10:03:00", trace_id="trace-c"),
            _assistant("three done", "2026-10-01T10:03:30"),
        ],
    )
    cut = sel.cut_session_record(path, _plan("trace-a", "trace-c"))
    assert cut.included and cut.unmatched == ()
    assert [m["content"] for m in cut.messages] == ["one", "one done", "three", "three done"]
    assert cut.metadata["last_consolidated"] == 0
    assert cut.metadata["metadata"] == {"title": "trip", "annotated_through": 0}
    rows = [json.loads(line) for line in cut.jsonl().splitlines()]
    assert rows[0]["_type"] == "metadata" and [r["content"] for r in rows[1:]] == [
        "one",
        "one done",
        "three",
        "three done",
    ]


def test_unmarked_blocks_match_by_input_and_the_turns_window(state, tmp_path, utc):
    _write(
        state,
        _turn("trace-a", minute=1, end_minute=5, artifact=_input_artifact(state, "a", "go")),
        _turn("trace-b", minute=6, artifact=_input_artifact(state, "b", "go")),
    )
    path = _session_file(
        tmp_path,
        [
            _meta(),
            _user("go", "2026-10-01T10:04:59"),
            _assistant("first answer", "2026-10-01T10:05:00"),
            _user("go", "2026-10-01T10:06:00"),
            _assistant("second answer", "2026-10-01T10:06:30"),
        ],
    )
    cut = sel.cut_session_record(path, _plan("trace-b"))
    assert [m["content"] for m in cut.messages] == ["go", "second answer"]
    assert cut.unmatched == ()
    cut = sel.cut_session_record(path, _plan("trace-a"))
    assert [m["content"] for m in cut.messages] == ["go", "first answer"]


def test_a_matching_text_outside_the_window_stays_unmatched(state, tmp_path, utc):
    _write(state, _turn("trace-a", minute=1, artifact=_input_artifact(state, "a", "hello")))
    path = _session_file(tmp_path, [_user("hello", "2026-10-01T11:30:00"), _assistant("hi", "2026-10-01T11:30:01")])
    cut = sel.cut_session_record(path, _plan("trace-a"))
    assert cut.messages == () and cut.unmatched == ("trace-a",)


def test_the_preview_stands_in_for_a_missing_input_artifact(state, tmp_path, utc, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING_PREVIEW", "5")
    _write(state, _turn("trace-a", minute=1, preview="hello...", artifact=str(state / "logs" / "gone.json")))
    path = _session_file(tmp_path, [_user("hello world", "2026-10-01T10:01:00")])
    assert [m["content"] for m in sel.cut_session_record(path, _plan("trace-a")).messages] == ["hello world"]


def _turn_at(trace: str, start: str, end: str | None, *, preview: str) -> dict[str, Any]:
    record = _turn(trace, minute=0, preview=preview, checkpoint=end is None)
    record["startTime"] = start
    record["endTime"] = end or start
    return record


def test_an_undone_turn_retyped_inside_its_window_is_left_unmatched(state, tmp_path, utc):
    """The old turn was undone and the same words sent again within the slack:
    the one block left could be either turn, so neither claims it."""
    _write(
        state,
        _turn_at("trace-old", "2026-10-01T10:01:00+00:00", "2026-10-01T10:02:00+00:00", preview="go"),
        _turn_at("trace-new", "2026-10-01T10:02:03+00:00", "2026-10-01T10:03:00+00:00", preview="go"),
    )
    path = _session_file(
        tmp_path, [_user("go", "2026-10-01T10:02:03"), _assistant("new answer only", "2026-10-01T10:03:00")]
    )
    old = sel.cut_session_record(path, _plan("trace-old"))
    assert old.messages == () and old.unmatched == ("trace-old",)
    new = sel.cut_session_record(path, _plan("trace-new"))
    assert new.messages == () and new.unmatched == ("trace-new",)


def test_overlapping_windows_are_resolved_by_the_order_of_the_record(state, tmp_path, utc):
    """The second block also fits the first turn's slack, but the first block
    can only be the first turn, and blocks keep the order of their turns."""
    _write(
        state,
        _turn_at("trace-a", "2026-10-01T10:01:00+00:00", "2026-10-01T10:02:00+00:00", preview="go"),
        _turn_at("trace-b", "2026-10-01T10:02:03+00:00", "2026-10-01T10:03:00+00:00", preview="go"),
    )
    path = _session_file(
        tmp_path,
        [
            _user("go", "2026-10-01T10:01:00"),
            _assistant("first", "2026-10-01T10:02:00"),
            _user("go", "2026-10-01T10:02:03"),
            _assistant("second", "2026-10-01T10:03:00"),
        ],
    )
    assert [m["content"] for m in sel.cut_session_record(path, _plan("trace-a")).messages] == ["go", "first"]
    assert [m["content"] for m in sel.cut_session_record(path, _plan("trace-b")).messages] == ["go", "second"]


def test_two_quick_turns_with_the_same_words_follow_the_record_order(state, tmp_path, utc):
    _write(
        state,
        _turn_at("trace-a", "2026-10-01T10:01:00+00:00", "2026-10-01T10:01:02+00:00", preview="next"),
        _turn_at("trace-b", "2026-10-01T10:01:03+00:00", "2026-10-01T10:01:05+00:00", preview="next"),
    )
    path = _session_file(
        tmp_path,
        [
            _user("next", "2026-10-01T10:01:00"),
            _assistant("one", "2026-10-01T10:01:02"),
            _user("next", "2026-10-01T10:01:03"),
            _assistant("two", "2026-10-01T10:01:05"),
        ],
    )
    assert [m["content"] for m in sel.cut_session_record(path, _plan("trace-b")).messages] == ["next", "two"]


def test_only_the_certain_block_is_claimed_when_a_turn_was_undone_between(state, tmp_path, utc):
    """Three turns, two blocks left: the first block can only be the first turn;
    the second could be either later one, so it is reported, not guessed."""
    _write(
        state,
        _turn_at("trace-a", "2026-10-01T10:01:00+00:00", "2026-10-01T10:01:30+00:00", preview="hello"),
        _turn_at("trace-b", "2026-10-01T10:02:00+00:00", "2026-10-01T10:02:30+00:00", preview="go"),
        _turn_at("trace-c", "2026-10-01T10:02:31+00:00", "2026-10-01T10:03:00+00:00", preview="go"),
    )
    path = _session_file(
        tmp_path,
        [
            _user("hello", "2026-10-01T10:01:00"),
            _user("go", "2026-10-01T10:02:31"),
            _assistant("?", "2026-10-01T10:03:00"),
        ],
    )
    cut = sel.cut_session_record(path, _plan("trace-a", "trace-b", "trace-c"))
    assert [m["content"] for m in cut.messages] == ["hello"]
    assert cut.unmatched == ("trace-b", "trace-c")


def test_a_checkpoint_window_closes_at_the_next_turn_even_when_that_turn_is_marked(state, tmp_path, utc):
    """The marked turn is claimed by its block, but it still ends the window of
    the checkpoint before it: the stray block long after cannot be that turn."""
    _write(
        state,
        _turn_at("trace-old", "2026-10-01T10:01:00+00:00", None, preview="go"),
        _turn_at("trace-marked", "2026-10-01T10:02:00+00:00", "2026-10-01T10:02:30+00:00", preview="other"),
        _turn_at("trace-late", "2026-10-01T10:30:00+00:00", "2026-10-01T10:31:00+00:00", preview="later"),
    )
    path = _session_file(
        tmp_path,
        [
            _user("go", "2026-10-01T10:10:00"),
            _assistant("not the old turn's", "2026-10-01T10:10:30"),
            _user("other", "2026-10-01T10:02:00", trace_id="trace-marked"),
        ],
    )
    cut = sel.cut_session_record(path, _plan("trace-old"))
    assert cut.messages == () and cut.unmatched == ("trace-old",)


def test_marked_blocks_bound_where_an_unmarked_block_may_land(state, tmp_path, utc):
    _write(
        state,
        _turn_at("trace-a", "2026-10-01T10:01:00+00:00", "2026-10-01T10:05:00+00:00", preview="go"),
        _turn_at("trace-m", "2026-10-01T10:02:00+00:00", "2026-10-01T10:02:30+00:00", preview="mid"),
        _turn_at("trace-b", "2026-10-01T10:03:00+00:00", "2026-10-01T10:04:00+00:00", preview="go"),
    )
    path = _session_file(
        tmp_path,
        [_user("mid", "2026-10-01T10:02:00", trace_id="trace-m"), _user("go", "2026-10-01T10:03:00")],
    )
    cut = sel.cut_session_record(path, _plan("trace-a", "trace-b"))
    assert [m["content"] for m in cut.messages] == ["go"]
    assert cut.unmatched == ("trace-a",)


def test_blocks_that_cannot_all_be_placed_claim_nothing(state, tmp_path, utc):
    """Each block fits only a turn on the wrong side of the other: no order-
    preserving assignment exists, so the premise is broken and nothing is claimed."""
    _write(
        state,
        _turn_at("trace-a", "2026-10-01T10:01:00+00:00", "2026-10-01T10:01:30+00:00", preview="first"),
        _turn_at("trace-b", "2026-10-01T10:02:00+00:00", "2026-10-01T10:02:30+00:00", preview="second"),
    )
    path = _session_file(tmp_path, [_user("second", "2026-10-01T10:02:00"), _user("first", "2026-10-01T10:01:00")])
    cut = sel.cut_session_record(path, _plan("trace-a", "trace-b"))
    assert cut.messages == () and cut.unmatched == ("trace-a", "trace-b")


def test_a_broken_stretch_does_not_cost_the_stretch_after_a_mark(state, tmp_path, utc):
    _write(
        state,
        _turn_at("trace-a", "2026-10-01T10:01:00+00:00", "2026-10-01T10:01:30+00:00", preview="first"),
        _turn_at("trace-b", "2026-10-01T10:02:00+00:00", "2026-10-01T10:02:30+00:00", preview="second"),
        _turn_at("trace-m", "2026-10-01T10:03:00+00:00", "2026-10-01T10:03:30+00:00", preview="mark"),
        _turn_at("trace-c", "2026-10-01T10:04:00+00:00", "2026-10-01T10:04:30+00:00", preview="third"),
    )
    path = _session_file(
        tmp_path,
        [
            _user("second", "2026-10-01T10:02:00"),
            _user("first", "2026-10-01T10:01:00"),
            _user("mark", "2026-10-01T10:03:00", trace_id="trace-m"),
            _user("third", "2026-10-01T10:04:00"),
        ],
    )
    cut = sel.cut_session_record(path, _plan("trace-a", "trace-c"))
    assert [m["content"] for m in cut.messages] == ["third"]
    assert cut.unmatched == ("trace-a",)


def test_list_content_is_compared_by_its_text_parts(state, tmp_path, utc):
    _write(state, _turn("trace-a", minute=1, artifact=_input_artifact(state, "a", "look at this")))
    blocks = [{"type": "text", "text": "look at this"}, {"type": "image_url", "image_url": {"url": "x"}}]
    path = _session_file(tmp_path, [_user(blocks, "2026-10-01T10:01:00")])
    assert len(sel.cut_session_record(path, _plan("trace-a")).messages) == 1


def test_messages_inside_a_turn_stay_with_it(state, tmp_path):
    _write(state, _turn("trace-a", minute=1), _turn("trace-b", minute=2), _turn("trace-c", minute=3))
    path = _session_file(
        tmp_path,
        [
            _assistant("before any turn", "2026-10-01T10:00:00"),
            _user("plan a trip", "2026-10-01T10:01:00", trace_id="trace-a"),
            _assistant("Which city?", "2026-10-01T10:01:01"),
            _user("Paris", "2026-10-01T10:02:00", trace_id="trace-b"),
            _user("and cheap", "2026-10-01T10:02:10", mid_turn=True),
            _assistant("(turn failed: provider error)", "2026-10-01T10:02:20", turn_ended={"status": "failed"}),
            _user("retry", "2026-10-01T10:03:00", trace_id="trace-c"),
        ],
    )
    cut = sel.cut_session_record(path, _plan("trace-b"))
    assert [m["content"] for m in cut.messages] == ["Paris", "and cheap", "(turn failed: provider error)"]
    cut = sel.cut_session_record(path, _plan("trace-a"))
    assert [m["content"] for m in cut.messages] == ["plan a trip", "Which city?"]


def test_a_dispatched_turn_in_the_session_follows_its_parent(state, tmp_path):
    _write(
        state,
        _turn("trace-a", minute=1),
        _turn("trace-report", minute=2, dispatched_in="trace-a"),
        _turn("trace-b", minute=3),
    )
    path = _session_file(
        tmp_path,
        [
            _user("research it", "2026-10-01T10:01:00", trace_id="trace-a"),
            _user("[sub-agent result]", "2026-10-01T10:02:00", trace_id="trace-report", origin="subagent"),
            _user("thanks", "2026-10-01T10:03:00", trace_id="trace-b"),
        ],
    )
    assert [m["content"] for m in sel.cut_session_record(path, _plan("trace-a")).messages] == [
        "research it",
        "[sub-agent result]",
    ]
    assert [m["content"] for m in sel.cut_session_record(path, _plan("trace-b")).messages] == ["thanks"]


def test_an_undone_turn_and_forked_history_are_left_out(state, tmp_path):
    _write(state, _turn("trace-a", minute=1), _turn("trace-b", minute=2))
    path = _session_file(
        tmp_path,
        [
            _user("from the parent", "2026-10-01T09:00:00", trace_id="trace-parent"),
            _assistant("parent answer", "2026-10-01T09:00:10"),
            _user("kept", "2026-10-01T10:01:00", trace_id="trace-a"),
        ],
    )
    cut = sel.cut_session_record(path, _plan("trace-a", "trace-b"))
    assert [m["content"] for m in cut.messages] == ["kept"]
    assert cut.unmatched == ("trace-b",)


def test_naive_stamps_are_read_in_local_time(state, tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        _write(state, _turn("trace-a", minute=1, artifact=_input_artifact(state, "a", "hi")))
        local = _session_file(tmp_path, [_user("hi", "2026-10-01T18:01:00")])
        assert len(sel.cut_session_record(local, _plan("trace-a")).messages) == 1
        as_utc = _session_file(tmp_path, [_user("hi", "2026-10-01T10:01:00")])
        assert sel.cut_session_record(as_utc, _plan("trace-a")).messages == ()
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_a_missing_session_file_is_reported_as_not_included(state, tmp_path):
    _write(state, _turn("trace-a", minute=1))
    cut = sel.cut_session_record(tmp_path / "absent.jsonl", _plan("trace-a"))
    assert not cut.included and cut.metadata is None and cut.unmatched == ("trace-a",)
    assert cut.jsonl() == ""


def test_the_mark_shape_is_not_a_redaction_finding(tmp_path):
    from raven.tracing.context import new_trace_id
    from raven.trajectory.redact import redact_bundle

    source = tmp_path / "bundle"
    source.mkdir()
    row = {"role": "user", "content": "hello", "timestamp": "2026-10-01T10:00:00", "trace_id": new_trace_id()}
    (source / "session.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    report = redact_bundle(source, tmp_path / "out", secrets=[])
    assert report.findings == [], report.findings

    control = tmp_path / "control"
    control.mkdir()
    (control / "session.jsonl").write_text(json.dumps({**row, "trace_id": "9f2c4e71b8a3d605e1f7c2b94a6d0e83"}) + "\n")
    assert redact_bundle(control, tmp_path / "control-out", secrets=[]).findings, "the scan must see a bare hex id"
