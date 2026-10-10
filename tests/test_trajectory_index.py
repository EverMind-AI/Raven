"""Tests for the per-session trajectory index (`raven.trajectory.index`)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from raven.trajectory import conversation as tconv
from raven.trajectory import entries as tent
from raven.trajectory import index as tidx
from raven.trajectory import policy as tpol

_T = "2026-09-01T10:00:"
SESSION = "tui:main"


def _ts(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"2026-09-01T10:{minutes:02d}:{secs:02d}+00:00"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    (path / "logs").mkdir(parents=True)
    return path


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _log(state: Path) -> Path:
    return state / "logs" / "audit-spans.log"


def _append(state: Path, spans: list[dict], *, path: Path | None = None) -> None:
    target = path or _log(state)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("ab") as fh:
        for span in spans:
            fh.write(json.dumps(span).encode() + b"\n")


def _rotate(state: Path, name: str = "2026-09-01-100000000000") -> Path:
    archive = state / "logs" / "archive" / "2026-09-01"
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / f"audit-spans-{name}.log"
    os.rename(_log(state), target)
    return target


def _artifact(state: Path, payload, name: str) -> str:
    directory = state / "logs" / "audit-artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return str(path)


def _span(trace, span_id, name, *, parent=None, start=0, end=None, attrs=None, status=None, session=SESSION):
    attributes = {"session.key": session} if session is not None else {}
    attributes.update(attrs or {})
    return {
        "traceId": trace,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "startTime": _ts(start),
        "endTime": _ts(end if end is not None else start),
        "status": status if status is not None else {"code": "OK", "message": ""},
        "attributes": attributes,
    }


def _turn(trace, span_id, *, start, end, session=SESSION, extra=None, in_progress=False):
    attrs = {"turn.input_preview": f"ask {span_id}", "turn.output_preview": f"reply {span_id}"}
    if in_progress:
        attrs["turn.in_progress"] = True
        attrs.pop("turn.output_preview")
    attrs.update(extra or {})
    return _span(trace, span_id, "session.turn", start=start, end=end, attrs=attrs, session=session)


def _tool(trace, span_id, parent, *, start, end, result="ok", session=SESSION):
    attrs = {"tool.name": span_id, "tool.args_preview": "{}", "tool.result_preview": result}
    return _span(trace, span_id, "tool.call", parent=parent, start=start, end=end, attrs=attrs, session=session)


def _index(state: Path, clock: Clock, **limits) -> tidx.SessionIndex:
    return tidx.SessionIndex(SESSION, state, limits=tidx.Limits(**limits), now=clock)


def _refresh_until_ready(index: tidx.SessionIndex, clock: Clock, *, rounds: int = 200) -> int:
    for used in range(1, rounds + 1):
        index.refresh_sync(None)
        if index.index_state().phase == tidx.PHASE_READY and index.index_state().recovering_traces == 0:
            return used
        clock.advance(1)
    raise AssertionError("index never became ready")


def _ids(index: tidx.SessionIndex) -> list[str]:
    return [e.entry_id for e in index.entries()]


# ── line reading ──────────────────────────────────────────────────────


def test_half_line_waits_for_completion(state, clock):
    index = _index(state, clock)
    full = json.dumps(_turn("t", "a", start=0, end=1)).encode() + b"\n"
    with _log(state).open("ab") as fh:
        fh.write(full[:20])
    index.refresh_sync(None)
    assert index.entries() == ()
    with _log(state).open("ab") as fh:
        fh.write(full[20:])
    index.refresh_sync(None)
    assert _ids(index) == ["t:a:turn.input", "t:a:turn.output"]
    index.refresh_sync(None)
    assert len(_ids(index)) == 2


def test_long_line_across_chunks_and_bad_lines_skip(state, clock):
    big = _turn("t", "big", start=0, end=1, extra={"pad": "x" * (3 * tidx.SCAN_CHUNK)})
    _append(state, [big])
    with _log(state).open("ab") as fh:
        fh.write(b"\xff\xfe not utf8\n")
        fh.write(b"{not json}\n")
        fh.write(b"[1, 2]\n")
    _append(state, [_turn("t", "after", start=5, end=6)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    assert {e.span_id for e in index.entries()} == {"big", "after"}
    assert index.index_state().scanned_bytes == _log(state).stat().st_size


def test_oversized_unterminated_line_is_bounded_and_dropped(state, clock):
    limit = 8 * 1024
    _append(state, [_turn("t", "before", start=0, end=1)])
    with _log(state).open("ab") as fh:
        fh.write(b"x" * (6 * limit))
        fh.write(b"\n")
    _append(state, [_turn("t", "after", start=2, end=3)])
    index = _index(state, clock, budget_bytes=limit, max_line_bytes=limit)
    for _ in range(20):
        index.refresh_sync(None)
        partials = [len(c.partial) for c in index.scanner.cursors.values()]
        assert all(p <= limit for p in partials)
        if index.index_state().phase == tidx.PHASE_READY:
            break
    assert {e.span_id for e in index.entries()} == {"before", "after"}
    assert index.index_state().oversized_lines_dropped == 1


def test_broken_record_position_survives_rename(state, clock):
    broken = {"name": "memory.enqueue", "startTime": _ts(0), "endTime": _ts(1), "attributes": {"session.key": SESSION}}
    _append(state, [broken, _turn("t", "a", start=1, end=2)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    before = [i for i in _ids(index) if i.startswith("broken:")]
    assert len(before) == 1
    _rotate(state)
    _append(state, [_turn("t", "b", start=3, end=4)])
    _refresh_until_ready(index, clock)
    after = [i for i in _ids(index) if i.startswith("broken:")]
    assert after == before


# ── budgets ───────────────────────────────────────────────────────────


def _bulk_logs(state: Path, *, files: int, per_file: int) -> int:
    total = 0
    for f in range(files):
        spans = [_turn("t", f"f{f}-s{i}", start=f * 100 + i, end=f * 100 + i) for i in range(per_file)]
        archive = state / "logs" / "archive" / "2026-09-01" / f"audit-spans-2026-09-01-10000000000{f}.log"
        _append(state, spans, path=archive)
        total += per_file
    _append(state, [_turn("t", "active", start=5000, end=5001)])
    return total + 1


def test_byte_budget_spreads_a_scan_over_rounds(state, clock):
    count = _bulk_logs(state, files=3, per_file=200)
    index = _index(state, clock, budget_bytes=16 * 1024)
    index.refresh_sync(None)
    first = index.index_state()
    assert first.phase == tidx.PHASE_SCANNING
    assert 0 < first.scanned_bytes < first.total_bytes
    page = index.list_page(None, 50)
    assert page.index_state.phase == tidx.PHASE_SCANNING
    rounds = _refresh_until_ready(index, clock)
    assert rounds > 1
    assert len(_ids(index)) == count * 2
    assert index.index_state().scanned_bytes == index.index_state().total_bytes


def test_time_budget_stops_a_round(state, clock):
    _bulk_logs(state, files=2, per_file=300)
    index = _index(state, clock)
    original_allow = tidx.ScanBudget.allow
    calls = {"n": 0}

    def ticking_allow(self, nbytes):
        calls["n"] += 1
        clock.advance(0.1)
        return original_allow(self, nbytes)

    tidx.ScanBudget.allow = ticking_allow  # type: ignore[method-assign]
    try:
        index.refresh_sync(clock() + 0.15)
    finally:
        tidx.ScanBudget.allow = original_allow  # type: ignore[method-assign]
    state_after = index.index_state()
    assert state_after.phase == tidx.PHASE_SCANNING
    assert 0 < state_after.scanned_bytes < state_after.total_bytes


# ── rotation ──────────────────────────────────────────────────────────


def test_append_after_eof_then_rotate_is_not_lost(state, clock):
    _append(state, [_turn("t", "a", start=0, end=1)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    _append(state, [_turn("t", "x", start=2, end=3)])
    _rotate(state)
    _append(state, [_turn("t", "y", start=4, end=5)])
    _refresh_until_ready(index, clock)
    assert [e.span_id for e in index.entries() if e.slot == "turn.input"] == ["a", "x", "y"]
    assert len(_ids(index)) == 6


def test_empty_active_file_then_write_then_rotate_inherits_identity(state, clock):
    _log(state).touch()
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    _append(state, [_turn("t", "a", start=0, end=1)])
    _rotate(state)
    _append(state, [_turn("t", "b", start=2, end=3)])
    _refresh_until_ready(index, clock)
    assert [e.span_id for e in index.entries() if e.slot == "turn.input"] == ["a", "b"]
    assert index.index_state().scanned_bytes == index.index_state().total_bytes


def test_short_partial_then_complete_then_rotate_yields_one_record(state, clock):
    full = json.dumps(_turn("t", "a", start=0, end=1)).encode() + b"\n"
    with _log(state).open("ab") as fh:
        fh.write(full[:30])
    index = _index(state, clock)
    index.refresh_sync(None)
    with _log(state).open("ab") as fh:
        fh.write(full[30:])
    _rotate(state)
    _append(state, [_turn("t", "b", start=2, end=3)])
    _refresh_until_ready(index, clock)
    assert [e.span_id for e in index.entries() if e.slot == "turn.input"] == ["a", "b"]


def test_checkpoint_in_old_file_final_in_new_does_not_regress(state, clock):
    _append(state, [_turn("t", "a", start=0, end=1, in_progress=True)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    assert index.entries()[0].operation_status == "running"
    _rotate(state)
    _append(state, [_turn("t", "a", start=0, end=9)])
    _refresh_until_ready(index, clock)
    outputs = [e for e in index.entries() if e.slot == "turn.output"]
    assert len(outputs) == 1 and outputs[0].operation_status == "ok" and outputs[0].charged_ms == 9000
    assert sum(1 for e in index.entries() if e.span_id == "a") == 2


def test_late_archive_discovery_does_not_replay(state, clock):
    _append(state, [_turn("t", "a", start=0, end=1)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    scanned = index.index_state().scanned_bytes
    rotated = _rotate(state)
    _append(state, [_turn("t", "b", start=2, end=3)])
    _refresh_until_ready(index, clock)
    assert index.index_state().scanned_bytes == rotated.stat().st_size + _log(state).stat().st_size
    assert index.index_state().scanned_bytes > scanned
    assert len(_ids(index)) == 4


def test_truncation_rebuilds_with_a_new_epoch(state, clock):
    _append(state, [_turn("t", "a", start=0, end=1), _turn("t", "b", start=2, end=3)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    epoch = index.epoch
    page = index.list_page(None, 10)
    with _log(state).open("r+b") as fh:
        fh.truncate(10)
    _log(state).write_bytes(json.dumps(_turn("t", "c", start=4, end=5)).encode() + b"\n")
    _refresh_until_ready(index, clock)
    assert index.epoch != epoch
    assert [e.span_id for e in index.entries() if e.slot == "turn.input"] == ["c"]
    batch = index.changes(epoch, page.snapshot_revision, 100)
    assert batch.reset_required and batch.epoch == index.epoch
    with pytest.raises(tidx.CursorExpiredError):
        index.list_page(page.next_cursor or tidx._encode_cursor(SESSION, epoch, "snap-1", 1), 10)


def test_deleting_an_unread_archive_file_rebuilds(state, clock):
    _bulk_logs(state, files=2, per_file=50)
    index = _index(state, clock, budget_bytes=4 * 1024)
    index.refresh_sync(None)
    epoch = index.epoch
    archive = sorted((state / "logs" / "archive").glob("*/*.log"))
    archive[0].unlink()
    index.refresh_sync(None)
    assert index.epoch != epoch
    _refresh_until_ready(index, clock)
    assert index.index_state().phase == tidx.PHASE_READY


def test_fingerprint_mismatch_means_generation_change(state, clock, monkeypatch):
    _append(state, [_turn("t", "a", start=0, end=1)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    epoch = index.epoch
    monkeypatch.setattr(tidx.SpanLogScanner, "_fingerprint", staticmethod(lambda handle: "different"))
    _append(state, [_turn("t", "b", start=2, end=3)])
    index.refresh_sync(None)
    assert index.epoch != epoch


# ── membership ────────────────────────────────────────────────────────


def test_nested_dispatch_resolves_when_grandparent_root_arrives(state, clock):
    c_child = _tool("C", "c-tool", "c-root", start=3, end=4, session="sub:c")
    c_root = _span(
        "C", "c-root", "session.turn", start=2, end=5, session="sub:c", attrs={"trace.dispatched_in_trace_id": "B"}
    )
    b_root = _span(
        "B", "b-root", "session.turn", start=1, end=6, session="sub:b", attrs={"trace.dispatched_in_trace_id": "A"}
    )
    a_root = _turn("A", "a-root", start=0, end=7)
    _append(state, [a_root, c_child, c_root])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    assert {e.trace_id for e in index.entries()} == {"A"}
    assert index.membership["C"] == tidx.PENDING
    _append(state, [b_root])
    _refresh_until_ready(index, clock)
    traces = {e.trace_id for e in index.entries()}
    assert traces == {"A", "B", "C"}
    assert {e.origin for e in index.entries() if e.trace_id in ("B", "C")} == {"subagent"}
    assert any(e.span_id == "c-tool" for e in index.entries())


def test_foreign_root_drops_its_pending_descendants(state, clock):
    other_child = _tool("X", "x-tool", "x-root", start=1, end=2, session="other")
    other_root = _span("X", "x-root", "session.turn", start=0, end=3, session="other")
    sub_child = _tool("Y", "y-tool", "y-root", start=1, end=2, session="sub")
    sub_root = _span(
        "Y", "y-root", "session.turn", start=0, end=3, session="sub", attrs={"trace.dispatched_in_trace_id": "X"}
    )
    _append(state, [other_child, sub_child, sub_root, other_root, _turn("t", "a", start=5, end=6)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    assert {e.trace_id for e in index.entries()} == {"t"}
    assert index.membership["X"] == tidx.FOREIGN and index.membership["Y"] == tidx.FOREIGN
    assert index.pending == {} and index.pending_count == 0


def test_unresolved_trace_is_counted_not_shown(state, clock):
    orphan_child = _tool("Z", "z-tool", "z-root", start=1, end=2, session="sub")
    orphan_root = _span(
        "Z", "z-root", "session.turn", start=0, end=3, session="sub", attrs={"trace.dispatched_in_trace_id": "never"}
    )
    _append(state, [orphan_child, orphan_root, _turn("t", "a", start=5, end=6)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    assert {e.trace_id for e in index.entries()} == {"t"}
    assert index.index_state().unresolved_traces == 1


def test_spilled_member_trace_is_recovered_by_rescan(state, clock):
    children = [_tool("S", f"s-{i}", "s-root", start=1, end=2, session="sub") for i in range(12)]
    _append(state, children)
    index = _index(state, clock, pending_spans=5)
    index.refresh_sync(None)
    assert "S" in index.spilled and index.pending_count <= 5
    _append(state, [_turn("t", "a", start=0, end=9)])
    _append(
        state,
        [
            _span(
                "S",
                "s-root",
                "session.turn",
                start=0,
                end=3,
                session="sub",
                attrs={"trace.dispatched_in_trace_id": "t"},
            )
        ],
    )
    index.refresh_sync(None)
    assert index.index_state().recovering_traces >= 0
    _refresh_until_ready(index, clock)
    recovered = {e.span_id for e in index.entries() if e.trace_id == "S"}
    assert recovered == {f"s-{i}" for i in range(12)} | {"s-root"}
    assert index.index_state().recovering_traces == 0 and index.spilled == {}


def test_spilled_foreign_trace_is_forgotten(state, clock):
    children = [_tool("F", f"f-{i}", "f-root", start=1, end=2, session="other") for i in range(12)]
    _append(state, children)
    index = _index(state, clock, pending_spans=5)
    index.refresh_sync(None)
    assert "F" in index.spilled
    _append(state, [_span("F", "f-root", "session.turn", start=0, end=3, session="other")])
    _refresh_until_ready(index, clock)
    assert index.spilled == {} and index.recovery_queue == deque_empty()


def deque_empty():
    from collections import deque

    return deque()


def test_dependency_limit_prunes_foreign_then_pending(state, clock):
    spans = []
    for i in range(6):
        spans.append(_span(f"F{i}", f"f{i}", "session.turn", start=i, end=i + 1, session="other"))
    for i in range(6):
        spans.append(_tool(f"P{i}", f"p{i}", f"root{i}", start=i, end=i + 1, session="sub"))
    spans.append(_turn("t", "a", start=50, end=51))
    _append(state, spans)
    index = _index(state, clock, dependencies=5)
    _refresh_until_ready(index, clock)
    assert index._dependency_size() <= 5
    assert not any(s == tidx.FOREIGN for s in index.membership.values())
    assert index.index_state().unresolved_dropped >= 1
    assert {e.trace_id for e in index.entries()} == {"t"}


# ── change protocol ───────────────────────────────────────────────────


def test_changes_page_through_a_large_batch_without_gaps(state, clock):
    _append(state, [_turn("t", "seed", start=0, end=1)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    page = index.list_page(None, 10)
    watermark = page.snapshot_revision
    _append(state, [_turn("t", f"s{i}", start=10 + i, end=10 + i) for i in range(600)])
    _refresh_until_ready(index, clock)
    seen: dict[str, int] = {}
    batches = 0
    while True:
        batch = index.changes(index.epoch, watermark, 500)
        batches += 1
        assert not batch.reset_required
        for entry in batch.upserts:
            assert entry.entry_id not in seen
            seen[entry.entry_id] = entry.revision
        assert batch.to_revision >= watermark
        watermark = batch.to_revision
        if not batch.has_more:
            break
    assert batches == 3
    assert len(seen) == 1200
    assert index.changes(index.epoch, watermark, 500).upserts == ()


def test_update_during_paging_reappears_with_higher_revision(state, clock):
    _append(state, [_turn("t", "a", start=0, end=1, in_progress=True), _turn("t", "b", start=10, end=11)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    first = index.changes(index.epoch, 0, 1)
    assert first.has_more
    _append(state, [_turn("t", "a", start=0, end=5)])
    _refresh_until_ready(index, clock)
    rest = index.changes(index.epoch, first.to_revision, 10)
    ids = [e.entry_id for e in rest.upserts]
    assert "t:a:turn.input" in ids or "t:a:turn.output" in ids
    assert all(e.revision > first.to_revision for e in rest.upserts)
    assert index.entry("t:a:turn.output").operation_status == "ok"


def test_error_placeholder_replacement_points_to_new_output(state, clock):
    failing = _span(
        "t",
        "tool",
        "tool.call",
        start=1,
        end=2,
        attrs={"tool.name": "x", "tool.args_preview": "{}"},
        status={"code": "ERROR", "message": "boom"},
    )
    _append(state, [_turn("t", "a", start=0, end=9), failing])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    assert "t:tool:error" in _ids(index)
    watermark = index.list_page(None, 10).snapshot_revision
    fixed = _tool("t", "tool", "a", start=1, end=2)
    _append(state, [fixed])
    _refresh_until_ready(index, clock)
    batch = index.changes(index.epoch, watermark, 1)
    assert len(batch.removed) == 1 and batch.removed[0].entry_id == "t:tool:error"
    assert batch.removed[0].replaced_by == "t:tool:tool.output"
    assert batch.has_more
    follow = index.changes(index.epoch, batch.to_revision, 10)
    assert "t:tool:tool.output" in [e.entry_id for e in follow.upserts]


def test_removed_window_overflow_and_epoch_mismatch_require_reset(state, clock):
    _append(state, [_turn("t", "a", start=0, end=1)])
    index = _index(state, clock, removed_window=2)
    _refresh_until_ready(index, clock)
    assert index.changes("other-epoch", 0, 10).reset_required
    for i in range(3):
        failing = _span(
            "t",
            f"x{i}",
            "tool.call",
            start=1,
            end=2,
            attrs={"tool.name": "x"},
            status={"code": "ERROR", "message": "boom"},
        )
        _append(state, [failing])
        _refresh_until_ready(index, clock)
        _append(state, [_tool("t", f"x{i}", "a", start=1, end=2)])
        _refresh_until_ready(index, clock)
    assert len(index._removed) == 2
    assert index.changes(index.epoch, 0, 10).reset_required
    assert not index.changes(index.epoch, index._removed[0].revision - 1, 10).reset_required


# ── list snapshot ─────────────────────────────────────────────────────


def test_list_snapshot_is_frozen_against_updates_deletes_and_reorders(state, clock):
    failing = _span(
        "t", "tool", "tool.call", start=5, end=6, attrs={"tool.name": "x"}, status={"code": "ERROR", "message": "boom"}
    )
    _append(state, [_turn("t", "a", start=0, end=9, in_progress=True), failing, _turn("t", "b", start=20, end=21)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    first = index.list_page(None, 2)
    frozen_rest = tuple(index.entries())[2:]
    _append(
        state,
        [_turn("t", "a", start=0, end=9), _tool("t", "tool", "a", start=5, end=6), _turn("t", "early", start=2, end=3)],
    )
    _refresh_until_ready(index, clock)
    second = index.list_page(first.next_cursor, 10)
    assert second.entries == frozen_rest
    assert all(e.revision <= first.snapshot_revision for e in second.entries)
    assert second.complete
    batch = index.changes(index.epoch, first.snapshot_revision, 100)
    changed = {e.entry_id for e in batch.upserts}
    assert "t:early:turn.input" in changed and "t:tool:tool.output" in changed
    assert any(r.entry_id == "t:tool:error" for r in batch.removed)


def test_cursor_rejects_other_session_epoch_and_expiry(state, clock):
    _append(state, [_turn("t", f"s{i}", start=i, end=i) for i in range(5)])
    index = _index(state, clock, snapshot_ttl=10)
    _refresh_until_ready(index, clock)
    page = index.list_page(None, 2)
    with pytest.raises(tidx.CursorExpiredError):
        index.list_page(tidx._encode_cursor("someone-else", index.epoch, "snap-1", 2), 2)
    with pytest.raises(tidx.CursorExpiredError):
        index.list_page(tidx._encode_cursor(SESSION, "old-epoch", "snap-1", 2), 2)
    with pytest.raises(tidx.CursorExpiredError):
        index.list_page("not base64 json", 2)
    clock.advance(11)
    with pytest.raises(tidx.CursorExpiredError):
        index.list_page(page.next_cursor, 2)


def test_new_first_page_replaces_the_old_snapshot(state, clock):
    _append(state, [_turn("t", f"s{i}", start=i, end=i) for i in range(5)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    old = index.list_page(None, 2)
    index.list_page(None, 2)
    with pytest.raises(tidx.CursorExpiredError):
        index.list_page(old.next_cursor, 2)


# ── resource limits ───────────────────────────────────────────────────


def test_entry_limit_trims_turn_roots_and_keeps_numbering(state, clock):
    _append(state, [_turn("t", f"s{i}", start=i, end=i) for i in range(30)])
    index = _index(state, clock, entries=10)
    _refresh_until_ready(index, clock)
    entries = index.entries()
    assert len(entries) <= 10
    assert index.index_state().head_truncated == 60 - len(entries)
    numbers = sorted({e.turn_number for e in entries})
    assert numbers[-1] == 30 and numbers[0] == 30 - len(numbers) + 1
    assert index.preview_cache.keys() <= set(index.spans)


def test_trimming_keeps_skeletons_for_surviving_descendants(state, clock):
    spans = [_turn("t", "turn", start=0, end=100)]
    spans.append(_span("t", "llm", "llm.call", parent="turn", start=1, end=2, attrs={"llm.output_preview": "o"}))
    sub_root = _span(
        "S",
        "sub-root",
        "session.turn",
        start=10,
        end=20,
        session="sub",
        attrs={"trace.dispatched_in_trace_id": "t", "turn.input_preview": "sub"},
    )
    spans.append(sub_root)
    spans.append(_tool("S", "sub-tool", "sub-root", start=15, end=16, session="sub"))
    spans.append(_tool("t", "tool", "llm", start=50, end=51))
    spans += [_turn("t", f"later{i}", start=200 + i, end=200 + i) for i in range(6)]
    _append(state, spans)
    index = _index(state, clock, entries=17)
    _refresh_until_ready(index, clock)
    entries = index.entries()
    assert len(entries) <= 17
    assert not any(e.span_id in ("turn", "llm", "sub-root") for e in entries)
    tool = next(e for e in entries if e.span_id == "tool" and e.slot == "tool.output")
    assert tool.turn_span_id == "turn" and tool.turn_number == 1 and "turn_unknown" not in tool.integrity
    assert tool.sort_key[2] == -2
    sub_tool = next(e for e in entries if e.span_id == "sub-tool" and e.slot == "tool.output")
    assert sub_tool.origin == "subagent" and sub_tool.turn_span_id == "sub-root" and sub_tool.turn_number == 2
    assert {("t", "turn"), ("t", "llm"), ("S", "sub-root")} <= set(index.skeletons)
    later = [e for e in entries if e.span_id.startswith("later") and e.turn_start]
    assert [e.turn_number for e in later] == [3, 4, 5, 6, 7, 8]


def test_skeleton_disappears_when_all_descendants_are_trimmed(state, clock):
    spans = [_turn("t", "turn", start=0, end=100), _tool("t", "tool", "turn", start=1, end=2)]
    spans += [_turn("t", f"later{i}", start=200 + i, end=200 + i) for i in range(10)]
    _append(state, spans)
    index = _index(state, clock, entries=4)
    _refresh_until_ready(index, clock)
    assert ("t", "turn") not in index.skeletons
    assert ("t", "turn") not in index.spans
    assert all(e.turn_number is not None for e in index.entries())


# ── eviction, restart, refresh ─────────────────────────────────────────


def test_eviction_rebuilds_with_new_epoch_and_full_history(state, clock):
    _append(state, [_turn("t", f"s{i}", start=i, end=i) for i in range(5)])
    indexer = tidx.TrajectoryIndexer(state, limits=tidx.Limits(max_sessions=2), now=clock)

    async def run():
        first = await indexer.refresh(SESSION)
        epoch = first.epoch
        for other in ("tui:two", "tui:three"):
            await indexer.refresh(other)
        clock.advance(1)
        again = await indexer.refresh(SESSION)
        assert again is not first and again.epoch != epoch
        assert len(again.entries()) == 10
        assert again.changes(epoch, 0, 10).reset_required

    asyncio.run(run())


def test_idle_sessions_are_evicted(state, clock):
    indexer = tidx.TrajectoryIndexer(state, limits=tidx.Limits(idle_seconds=5), now=clock)

    async def run():
        await indexer.refresh(SESSION)
        clock.advance(6)
        await indexer.refresh("tui:other")
        assert SESSION not in indexer._slots

    asyncio.run(run())


def test_restart_rejects_old_cursor_and_epoch(state, clock):
    _append(state, [_turn("t", f"s{i}", start=i, end=i) for i in range(5)])
    old = _index(state, clock)
    _refresh_until_ready(old, clock)
    page = old.list_page(None, 2)
    fresh = _index(state, clock)
    _refresh_until_ready(fresh, clock)
    with pytest.raises(tidx.CursorExpiredError):
        fresh.list_page(page.next_cursor, 2)
    assert fresh.changes(old.epoch, page.snapshot_revision, 10).reset_required


def test_refresh_is_throttled_single_flight_and_failure_safe(state, clock):
    _append(state, [_turn("t", "a", start=0, end=1)])
    calls = {"n": 0}

    async def fake_to_thread(fn, *args):
        calls["n"] += 1
        await asyncio.sleep(0)
        return fn(*args)

    indexer = tidx.TrajectoryIndexer(
        state, limits=tidx.Limits(throttle_seconds=0.3), now=clock, to_thread=fake_to_thread
    )

    async def run():
        await asyncio.gather(*(indexer.refresh(SESSION) for _ in range(5)))
        assert calls["n"] == 1
        await indexer.refresh(SESSION)
        assert calls["n"] == 1
        clock.advance(1)
        await indexer.refresh(SESSION)
        assert calls["n"] == 2

        def boom(_deadline):
            raise RuntimeError("disk on fire")

        indexer.session(SESSION).refresh_sync = boom  # type: ignore[method-assign]
        clock.advance(1)
        index = await indexer.refresh(SESSION)
        assert index.index_state().phase == tidx.PHASE_FAILED
        assert index.index_state().failure == "RuntimeError"

    asyncio.run(run())


def test_a_cancelled_refresh_finishes_before_the_next_one_scans(state):
    """The awaiting task can be cancelled, its worker thread cannot: the next
    refresh must not scan the same index while that thread is still in it."""
    import threading

    turns = 40
    _append(state, [_turn("t", f"s{i}", start=i, end=i + 1) for i in range(turns)])
    indexer = tidx.TrajectoryIndexer(state, limits=tidx.Limits(throttle_seconds=0, budget_bytes=512))
    index = indexer.session(SESSION)
    scan = index.scanner.scan
    entered, release = threading.Event(), threading.Event()
    seen = {"active": 0, "most": 0, "calls": 0}
    guard = threading.Lock()

    def gated_scan(budget):
        with guard:
            seen["active"] += 1
            seen["most"] = max(seen["most"], seen["active"])
            seen["calls"] += 1
            first = seen["calls"] == 1
        try:
            if first:
                entered.set()
                assert release.wait(5)
            return scan(budget)
        finally:
            with guard:
                seen["active"] -= 1

    index.scanner.scan = gated_scan  # type: ignore[method-assign]

    async def run():
        first = asyncio.create_task(indexer.refresh(SESSION))
        assert await asyncio.to_thread(entered.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(indexer.refresh(SESSION))
        await asyncio.sleep(0.1)
        assert seen["most"] == 1
        release.set()
        await second
        for _ in range(500):
            if index.index_state().phase == tidx.PHASE_READY:
                break
            await indexer.refresh(SESSION)
        assert index.index_state().phase == tidx.PHASE_READY

    asyncio.run(run())
    assert seen["most"] == 1
    assert len([e for e in index.entries() if e.kind == "user.input"]) == turns


def _tool_with_output(state, trace, span_id, parent, *, start, end, pointer):
    attrs = {"tool.name": span_id, "tool.args_preview": "{}", "tool.output.artifact_path": pointer}
    return _span(trace, span_id, "tool.call", parent=parent, start=start, end=end, attrs=attrs)


@pytest.mark.parametrize("bad", ["deep", "nul"])
def test_one_unreadable_tool_result_leaves_the_rest_of_the_session_indexed(state, clock, bad):
    """A result nested deeper than the parser can go, or a pointer that cannot
    name a file, marks its own entry; every other entry still projects."""
    if bad == "deep":
        pointer = _artifact(state, {"result": "[" * 20000 + "]" * 20000}, "deep")
    else:
        pointer = str(state / "logs" / "audit-artifacts" / "bad\x00name.json")
    _append(
        state,
        [
            _turn("t", "a", start=0, end=10),
            _tool_with_output(state, "t", "bad", "a", start=1, end=2, pointer=pointer),
            _tool("t", "fine", "a", start=3, end=4),
        ],
    )
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    _append(state, [_turn("t2", "b", start=20, end=30)])
    _refresh_until_ready(index, clock)
    assert index.index_state().phase == tidx.PHASE_READY and index.index_state().failure is None
    spans = {e.span_id for e in index.entries()}
    assert {"a", "bad", "fine", "b"} <= spans
    bad_entry = next(e for e in index.entries() if e.span_id == "bad" and e.kind == "tool.output")
    if bad == "nul":
        assert "artifact_unreadable" in bad_entry.integrity
    else:
        assert bad_entry.operation_status == "ok"


def test_a_preview_pass_that_raises_marks_its_span_and_the_index_goes_on(state, clock, monkeypatch):
    real = tent.preview_records

    def flaky(span, **kwargs):
        if span.get("spanId") == "bad":
            raise RecursionError("maximum recursion depth exceeded")
        return real(span, **kwargs)

    monkeypatch.setattr(tent, "preview_records", flaky)
    pointer = _artifact(state, {"result": "fine"}, "bad-out")
    _append(
        state,
        [
            _turn("t", "a", start=0, end=10),
            _tool_with_output(state, "t", "bad", "a", start=1, end=2, pointer=pointer),
            _tool("t", "fine", "a", start=3, end=4),
        ],
    )
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    assert index.index_state().phase == tidx.PHASE_READY and index.index_state().failure is None
    assert {"a", "bad", "fine"} <= {e.span_id for e in index.entries()}
    bad_entry = next(e for e in index.entries() if e.span_id == "bad" and e.kind == "tool.output")
    assert "artifact_unreadable" in bad_entry.integrity
    calls = {"n": 0}

    def counting(span, **kwargs):
        calls["n"] += span.get("spanId") == "bad"
        return real(span, **kwargs)

    monkeypatch.setattr(tent, "preview_records", counting)
    _append(state, [_turn("t2", "b", start=20, end=30)])
    _refresh_until_ready(index, clock)
    assert calls["n"] == 0


# ── lazy previews ─────────────────────────────────────────────────────


def _v2_input(state: Path, name: str, count: int) -> str:
    from raven.tracing import artifact_v2

    messages_dir = state / "logs" / "audit-artifacts" / "_messages"
    refs = []
    for i in range(count):
        message = {"role": "user", "content": f"message {i}"}
        sha1 = __import__("hashlib").sha1(json.dumps(message, ensure_ascii=False, default=str).encode()).hexdigest()
        path = artifact_v2.message_path(state / "logs" / "audit-artifacts", sha1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(message), encoding="utf-8")
        refs.append({"$msg": sha1})
    shell = {"artifactFormat": artifact_v2.ARTIFACT_FORMAT, "messages": refs, "prompt": refs[-1], "tools": []}
    del messages_dir
    return _artifact(state, shell, name)


def test_preview_reads_are_bounded_and_resume(state, clock, monkeypatch):
    reads = {"n": 0, "bytes": 0}
    original = tconv._read_artifact

    def counting(state_dir, path, limit=tconv._ARTIFACT_LIMIT):
        reads["n"] += 1
        text, reason = original(state_dir, path, limit)
        reads["bytes"] += len(text.encode()) if text else 0
        return text, reason

    monkeypatch.setattr(tconv, "_read_artifact", counting)
    many = {f"foo.k{i:02d}.artifact_path": _artifact(state, {"k": i, "pad": "x" * 100}, f"foo-{i}") for i in range(40)}
    spans = [
        _turn("t", "turn", start=0, end=100),
        _span("t", "foo", "foo.bar", parent="turn", start=1, end=2, attrs=many),
        _span(
            "t",
            "llm",
            "llm.call",
            parent="turn",
            start=3,
            end=4,
            attrs={"llm.input.artifact_path": _v2_input(state, "llm-in", 500)},
        ),
    ]
    _append(state, spans)
    index = _index(state, clock, preview_reads=32, preview_bytes=2 * 1024 * 1024)
    index.refresh_sync(None)
    assert reads["n"] <= 32
    assert reads["bytes"] <= 2 * 1024 * 1024
    assert index.index_state().preview_pending >= 1
    assert index.entry("t:llm:llm.input").preview == "message 499"
    foo_cache = index.preview_cache[("t", "foo")]
    assert not foo_cache.complete and 0 < foo_cache.cursor < 40
    before_cursor = foo_cache.cursor
    index.refresh_sync(None)
    assert index.preview_cache[("t", "foo")].cursor > before_cursor
    for _ in range(5):
        index.refresh_sync(None)
    assert index.preview_cache[("t", "foo")].complete
    assert index.index_state().preview_pending == 0
    total = reads["n"]
    index.refresh_sync(None)
    assert reads["n"] == total


def test_thinking_blocks_are_discovered_despite_output_preview(state, clock):
    output = {"content": "answer", "thinking_blocks": [{"type": "thinking", "thinking": "deep reasoning"}]}
    attrs = {
        "llm.output_preview": "answer",
        "llm.output.artifact_path": _artifact(state, output, "llm-out"),
        "llm.input.artifact_path": _artifact(state, {"messages": [{"role": "user", "content": "q"}]}, "llm-in"),
    }
    _append(
        state,
        [_turn("t", "turn", start=0, end=9), _span("t", "llm", "llm.call", parent="turn", start=1, end=2, attrs=attrs)],
    )
    index = _index(state, clock)
    index.refresh_sync(None)
    _refresh_until_ready(index, clock)
    ids = _ids(index)
    assert "t:llm:llm.thinking" in ids
    thinking = index.entry("t:llm:llm.thinking")
    assert thinking.preview == "deep reasoning" and thinking.timing_basis == "not_recorded"
    assert index.entry("t:llm:llm.input").preview == "q"


def test_deadline_still_allows_one_preview_read_per_round(state, clock, monkeypatch):
    reads = {"n": 0}
    original = tconv._read_artifact

    def counting(state_dir, path, limit=tconv._ARTIFACT_LIMIT):
        reads["n"] += 1
        return original(state_dir, path, limit)

    monkeypatch.setattr(tconv, "_read_artifact", counting)
    attrs = {f"foo.k{i}.artifact_path": _artifact(state, {"k": i}, f"g-{i}") for i in range(3)}
    _append(state, [_span("t", "foo", "foo.bar", start=0, end=1, attrs=attrs)])
    index = _index(state, clock)
    index.refresh_sync(clock() - 1)
    assert reads["n"] == 1
    index.refresh_sync(clock() - 1)
    assert reads["n"] == 2


def test_checkpoint_upgrade_invalidates_preview_cache(state, clock, monkeypatch):
    reads = {"n": 0}
    original = tconv._read_artifact

    def counting(state_dir, path, limit=tconv._ARTIFACT_LIMIT):
        reads["n"] += 1
        return original(state_dir, path, limit)

    monkeypatch.setattr(tconv, "_read_artifact", counting)
    attrs = {
        "turn.input.artifact_path": _artifact(state, {"content": "hello world"}, "turn-in"),
        "turn.in_progress": True,
    }
    _append(state, [_span("t", "turn", "session.turn", start=0, end=1, attrs=attrs)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    assert index.entry("t:turn:turn.input").preview == "hello world"
    first = reads["n"]
    final_attrs = {
        "turn.input.artifact_path": attrs["turn.input.artifact_path"],
        "turn.output.artifact_path": _artifact(state, {"content": "done"}, "turn-out"),
    }
    _append(state, [_span("t", "turn", "session.turn", start=0, end=5, attrs=final_attrs)])
    _refresh_until_ready(index, clock)
    assert reads["n"] > first
    assert index.entry("t:turn:turn.output").preview == "done"


# ── policy ────────────────────────────────────────────────────────────


def test_policy_defaults_and_replacement():
    policy = tpol.TrajectoryPolicy()
    assert policy.enabled() is False and policy.revision == 0
    assert tpol.TrajectoryPolicy.fixed(True).enabled() is True
    policy.replace_source(lambda: True)
    assert policy.enabled() is True and policy.revision == 1


# ── review regressions ────────────────────────────────────────────────


def _run_with_timeout(fn, seconds: float = 5.0) -> None:
    import threading

    worker = threading.Thread(target=fn, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "refresh did not terminate"


def test_broken_multi_slot_record_is_trimmed_without_looping(state, clock):
    attrs = {"session.key": SESSION, "tool.name": "x", "tool.args_preview": "{}", "tool.result_preview": "ok"}
    broken = {
        "traceId": "t",
        "spanId": None,
        "name": "tool.call",
        "startTime": _ts(0),
        "endTime": _ts(1),
        "attributes": attrs,
    }
    no_trace = {"spanId": "s", "name": "tool.call", "startTime": _ts(2), "endTime": _ts(3), "attributes": attrs}
    _append(state, [broken, no_trace, _turn("t", "later", start=10, end=11)])
    index = _index(state, clock, entries=1)
    _run_with_timeout(lambda: index.refresh_sync(None))
    entries = index.entries()
    assert len(entries) <= 1
    assert not any(e.entry_id.startswith("broken:") for e in entries)
    assert index.index_state().head_truncated >= 4
    assert not any(key[0] == "" for key in index.spans)


def test_trim_stall_terminates_with_warning(state, clock, monkeypatch, caplog):
    _append(state, [_turn("t", f"s{i}", start=i, end=i) for i in range(6)])
    index = _index(state, clock, entries=2)
    monkeypatch.setattr(index, "_trim_span", lambda key: None)
    with caplog.at_level("WARNING", logger="raven.trajectory.index"):
        _run_with_timeout(lambda: index.refresh_sync(None))
    assert "made no progress" in caplog.text
    assert len(index.entries()) == 12
    assert index.index_state().head_truncated == 0


def test_rotation_between_listing_and_open_loses_nothing(state, clock, monkeypatch):
    _append(state, [_turn("t", "old", start=0, end=1)])
    index = _index(state, clock)
    original = tidx.SpanLogScanner._read
    armed = {"fire": True}

    def racing_read(self, cursor, budget, batch):
        if armed["fire"] and cursor.path == _log(state):
            armed["fire"] = False
            _rotate(state)
            _append(state, [_turn("t", "new", start=2, end=3)])
        return original(self, cursor, budget, batch)

    monkeypatch.setattr(tidx.SpanLogScanner, "_read", racing_read)
    rounds = 0
    while rounds < 10:
        rounds += 1
        index.refresh_sync(None)
        if index.index_state().phase == tidx.PHASE_READY and len(index.entries()) == 4:
            break
    assert [e.span_id for e in index.entries() if e.slot == "turn.input"] == ["old", "new"]
    assert len(index.entries()) == 4
    assert index.index_state().scanned_bytes == index.index_state().total_bytes


def test_reads_stay_consistent_while_a_refresh_runs(state, clock):
    import threading

    _append(state, [_turn("t", f"s{i}", start=i, end=i) for i in range(50)])
    index = _index(state, clock)
    index.refresh_sync(None)
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader():
        try:
            while not stop.is_set():
                index.index_state()
                page = index.list_page(None, 20)
                batch = index.changes(page.epoch, 0, 1000)
                assert not batch.reset_required
                for entry in page.entries:
                    index.span(entry.trace_id, entry.span_id)
                    index.entry(entry.entry_id)
        except BaseException as exc:  # noqa: BLE001 — the test records any reader failure
            errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for i in range(30):
            _append(state, [_turn("t", f"n{i}", start=100 + i, end=100 + i, extra={"turn.in_progress": i % 2 == 0})])
            index.refresh_sync(None)
    finally:
        stop.set()
        thread.join(5)
    assert errors == []
    published = index.entries()
    assert all(index.span(e.trace_id, e.span_id) is not None for e in published)
    assert {e.revision for e in published} == {e.revision for e in index.changes(index.epoch, 0, 10_000).upserts}


def test_same_size_rewrite_is_detected_without_new_bytes(state, clock):
    old = json.dumps(_turn("t", "old", start=0, end=1)) + "\n"
    _log(state).write_text(old, encoding="utf-8")
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    epoch = index.epoch
    page = index.list_page(None, 10)
    assert [e.span_id for e in page.entries] == ["old", "old"]
    new = old.replace('"old"', '"new"')
    assert len(new) == len(old)
    os.utime(_log(state), ns=(1, 1))
    _log(state).write_text(new, encoding="utf-8")
    _refresh_until_ready(index, clock)
    assert index.epoch != epoch
    assert [e.span_id for e in index.entries() if e.slot == "turn.input"] == ["new"]
    assert index.changes(epoch, page.snapshot_revision, 10).reset_required


def test_unchanged_archive_file_is_not_reopened(state, clock, monkeypatch):
    _append(state, [_turn("t", "a", start=0, end=1)])
    archived = _rotate(state)
    _append(state, [_turn("t", "b", start=2, end=3)])
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    opens = {"archive": 0, "active": 0}
    original = Path.open

    def counting_open(self, *args, **kwargs):
        if self == archived:
            opens["archive"] += 1
        elif self == _log(state):
            opens["active"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    index.refresh_sync(None)
    index.refresh_sync(None)
    assert opens["archive"] == 0
    assert opens["active"] == 2
    assert len(index.entries()) == 4


def test_capture_returns_one_consistent_view(state, clock):
    import threading

    _append(
        state,
        [
            _turn("t", "turn", start=0, end=100),
            _span("t", "llm", "llm.call", parent="turn", start=1, end=2, attrs={"llm.output_preview": "o"}),
            _tool("t", "tool", "turn", start=3, end=4),
            _tool("t", "tool2", "turn", start=5, end=6),
        ],
    )
    index = _index(state, clock)
    _refresh_until_ready(index, clock)
    view = index.capture("t:tool:tool.input")
    assert view is not None and view.epoch == index.epoch
    assert view.entry.entry_id == "t:tool:tool.input" and view.span["spanId"] == "tool"
    assert [s.entry_id for s in view.siblings] == ["t:tool:tool.output"]
    assert view.turn is not None and view.turn.number == 1
    assert [s["spanId"] for s in view.parent_llm_calls] == ["llm"]
    assert view.owner is not None and view.owner.entry_id == "t:tool:tool.output"
    assert index.capture("missing") is None
    stop = threading.Event()
    mismatches: list[str] = []

    def reader():
        while not stop.is_set():
            captured = index.capture("t:turn:turn.input")
            if captured is None:
                continue
            published = index.entry("t:turn:turn.input")
            if captured.span is None or captured.entry.revision > (published.revision if published else -1):
                mismatches.append("span/entry drift")
            if (
                captured.span.get("attributes", {}).get("turn.in_progress") is True
                and captured.entry.operation_status != "running"
            ):
                mismatches.append("in_progress span paired with non-running entry")

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for i in range(20):
            _append(state, [_turn("t", "turn", start=0, end=100 + i, in_progress=i % 2 == 0)])
            index.refresh_sync(None)
    finally:
        stop.set()
        thread.join(5)
    assert mismatches == []


# ── the repair read behind a continued model input ────────────────────


def _v2_shell(state: Path, name: str, messages: list[dict]) -> str:
    from raven.tracing import artifact_v2

    refs = []
    for message in messages:
        sha1 = artifact_v2.message_sha1(message)
        path = artifact_v2.message_path(state / "logs" / "audit-artifacts", sha1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(artifact_v2.message_text(message), encoding="utf-8")
        refs.append({"$msg": sha1})
    shell = {"artifactFormat": artifact_v2.ARTIFACT_FORMAT, "messages": refs, "prompt": refs[-1], "tools": []}
    return _artifact(state, shell, name)


def _dialogue(state: Path) -> list[dict]:
    s, u1, a1, u2 = (
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "the answer"},
        {"role": "user", "content": "next question"},
    )
    return [
        _turn("a", "turnA", start=0, end=10),
        _span(
            "a",
            "a1",
            "llm.call",
            parent="turnA",
            start=1,
            end=2,
            attrs={
                "llm.purpose": "main",
                "llm.input.artifact_path": _v2_shell(state, "a1-in", [s, u1]),
                "llm.output.artifact_path": _artifact(state, {"content": "the answer"}, "a1-out"),
            },
        ),
        _turn("b", "turnB", start=20, end=30),
        _span(
            "b",
            "b1",
            "llm.call",
            parent="turnB",
            start=21,
            end=22,
            attrs={
                "llm.purpose": "main",
                "llm.input.artifact_path": _v2_shell(state, "b1-in", [s, u1, a1, u2]),
                "llm.output.artifact_path": _artifact(state, {"content": "second answer"}, "b1-out"),
            },
        ),
    ]


def _refresh_until(index: tidx.SessionIndex, clock: Clock, condition, *, rounds: int = 40) -> int:
    for used in range(1, rounds + 1):
        index.refresh_sync(None)
        clock.advance(1)
        if condition():
            return used
    raise AssertionError("condition never met")


def test_a_late_predecessor_repairs_the_successor_row_and_bumps_its_revision(state, clock):
    _append(state, _dialogue(state))
    index = _index(state, clock, preview_reads=2)
    b1 = lambda: index.entry("b:b1:llm.input")  # noqa: E731 - a re-read of the published row
    # Newest first: the second call's own files are read while the first call's are still unread.
    _refresh_until(index, clock, lambda: b1() is not None and b1().meta.get("message_count") == 4)
    assert b1().meta["delta"] == "unknown" and b1().preview == "next question"
    unknown_revision = b1().revision
    # The predecessor arrives: the prefix is proven; whether the message after it is that call's
    # own output is not known yet, so what b1 adds starts right after the prefix for now.
    _refresh_until(index, clock, lambda: b1().meta.get("delta") == "continued")
    assert b1().meta["new_from"] == 2 and b1().meta["echo_at"] is None and b1().revision > unknown_revision
    assert index.preview_cache[("b", "b1")].pending["probe_index"] == 2
    assert index.index_state().preview_pending >= 1
    continued_revision = b1().revision
    # The probe lands: the message there is the predecessor's answer, so b1 adds from one later.
    _refresh_until(index, clock, lambda: b1().meta.get("echo_at") == 2)
    assert b1().meta["new_from"] == 3 and b1().revision > continued_revision
    batch = index.changes(index.epoch, continued_revision, 50)
    assert "b:b1:llm.input" in {e.entry_id for e in batch.upserts}
    # The next probe reads the question b1 adds, and then nothing is left to read.
    _refresh_until(index, clock, lambda: index.index_state().preview_pending == 0)
    assert b1().preview == "next question"
    record = next(r for r in index.preview_cache[("b", "b1")].records if r["slot"] == "llm.input")
    assert record["probes"] == {
        "2": {"role": "assistant", "text": "the answer", "failed": None},
        "3": {"role": "user", "text": "next question", "failed": None},
    }
    assert index.preview_cache[("b", "b1")].complete and index.preview_cache[("b", "b1")].pending is None
    assert index.entry("a:a1:llm.input").meta["delta"] == "first"


def test_tool_results_after_the_echo_are_what_a_call_adds_each_probe_under_its_own_revision(state, clock):
    s, u1 = {"role": "system", "content": "rules"}, {"role": "user", "content": "check both"}
    echo = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}, {"id": "c2"}]}
    r1 = {"role": "tool", "tool_call_id": "c1", "content": "result one"}
    r2 = {"role": "tool", "tool_call_id": "c2", "content": "result two"}

    def call(span_id, start, messages):
        return _span(
            "a",
            span_id,
            "llm.call",
            parent="turnA",
            start=start,
            end=start + 1,
            attrs={
                "llm.purpose": "main",
                "llm.input.artifact_path": _v2_shell(state, f"{span_id}-in", messages),
                "llm.output.artifact_path": _artifact(state, {"content": ""}, f"{span_id}-out"),
            },
        )

    _append(state, [_turn("a", "turnA", start=0, end=10), call("a1", 1, [s, u1]), call("a2", 3, [s, u1, echo, r1, r2])])
    index = _index(state, clock, preview_reads=1)
    a2 = lambda: index.entry("a:a2:llm.input")  # noqa: E731
    _refresh_until(index, clock, lambda: a2() is not None and a2().meta.get("delta") == "continued", rounds=200)
    revisions = [a2().revision]
    _refresh_until(index, clock, lambda: a2().meta.get("echo_at") == 2, rounds=200)
    revisions.append(a2().revision)
    _refresh_until(index, clock, lambda: a2().preview == "result one", rounds=200)
    revisions.append(a2().revision)
    # One read per pass: the echo and the first result land in different passes, each a new revision.
    assert revisions == sorted(set(revisions)) and len(revisions) == 3
    # Both results are what a2 adds; the row names the first.
    assert a2().meta["new_from"] == 3 and a2().meta["message_count"] == 5
    _refresh_until(index, clock, lambda: index.index_state().preview_pending == 0, rounds=200)
    assert a2().preview == "result one"


def test_probes_stay_within_the_scan_window_and_the_echo_when_the_decision_moves(state, clock):
    s, u1, a1, u2 = (
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "x" * 5000},
        {"role": "user", "content": "next question"},
    )

    def call(trace, span_id, start, messages):
        return _span(
            trace,
            span_id,
            "llm.call",
            parent=f"turn{trace.upper()}",
            start=start,
            end=start + 1,
            attrs={
                "llm.purpose": "main",
                "llm.input.artifact_path": _v2_shell(state, f"{span_id}-in", messages),
                "llm.output.artifact_path": _artifact(state, {"content": "ok"}, f"{span_id}-out"),
            },
        )

    # The later call alone first: with no predecessor it is all new, searched from message 0.
    _append(state, [_turn("b", "turnB", start=20, end=30), call("b", "b1", 21, [s, u1, a1, u2])])
    index = _index(state, clock, preview_reads=2)
    b1 = lambda: index.entry("b:b1:llm.input")  # noqa: E731
    probes = lambda: next(r for r in index.preview_cache[("b", "b1")].records if r["slot"] == "llm.input").get("probes")  # noqa: E731
    _refresh_until(index, clock, lambda: index.index_state().preview_pending == 0 and b1() is not None, rounds=200)
    assert b1().meta["delta"] == "first" and b1().preview == "first question"
    assert set(probes()) == {"0", "1"}
    # The predecessor turns up: the search now starts after it, and the old window's probes go.
    _append(state, [_turn("a", "turnA", start=0, end=10), call("a", "a1", 1, [s, u1])])
    _refresh_until(
        index, clock, lambda: b1().meta.get("echo_at") == 2 and index.index_state().preview_pending == 0, rounds=200
    )
    assert b1().meta["new_from"] == 3 and b1().preview == "next question"
    kept = probes()
    assert set(kept) == {"2", "3"} and len(kept) <= 5
    # The echo's text is kept at the row's preview length, not whole.
    assert len(kept["2"]["text"]) <= tent.PREVIEW_LIMIT
    # Settled: further passes queue nothing.
    for _ in range(3):
        index.refresh_sync(None)
        clock.advance(1)
    assert index.preview_cache[("b", "b1")].pending is None and set(probes()) == {"2", "3"}


def test_first_and_independent_inputs_preview_their_first_non_system_message_once_read(state, clock):
    s = {"role": "system", "content": "rules"}
    u1, a1, u2 = (
        {"role": "user", "content": "FIRST NEW QUESTION"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "LAST PROMPT"},
    )
    j, w1, w2 = (
        {"role": "system", "content": "judge rules"},
        {"role": "user", "content": "FIRST INDEPENDENT"},
        {"role": "user", "content": "LAST INDEPENDENT"},
    )
    s2, u3 = {"role": "system", "content": "a reminder slipped in"}, {"role": "user", "content": "AFTER THE REMINDER"}

    def call(span_id, start, purpose, messages, answer):
        return _span(
            "a",
            span_id,
            "llm.call",
            parent="turnA",
            start=start,
            end=start + 1,
            attrs={
                "llm.purpose": purpose,
                "llm.input.artifact_path": _v2_shell(state, f"{span_id}-in", messages),
                "llm.output.artifact_path": _artifact(state, {"content": answer}, f"{span_id}-out"),
            },
        )

    _append(
        state,
        [
            _turn("a", "turnA", start=0, end=10),
            call("a1", 1, "main", [s, u1, a1, u2], "answer"),
            call("a2", 3, "watch_work", [j, w1, w2], "no"),
            call("a3", 5, "main", [s, u1, a1, u2, s2, u3], "ok"),
        ],
    )
    index = _index(state, clock, preview_reads=2)
    row = lambda span_id: index.entry(f"a:{span_id}:llm.input")  # noqa: E731
    _refresh_until(
        index,
        clock,
        lambda: all(row(x) is not None for x in ("a1", "a2", "a3")) and index.index_state().preview_pending == 0,
        rounds=200,
    )
    # The prompt is the last message; the row names the first message that is not a system one.
    assert row("a1").meta["delta"] == "first" and row("a1").preview == "FIRST NEW QUESTION"
    assert row("a2").meta["delta"] == "independent" and row("a2").preview == "FIRST INDEPENDENT"
    # A continued call whose added messages begin with a system message: the search moves one on.
    assert row("a3").meta["delta"] == "continued" and row("a3").meta["new_from"] == 4
    assert row("a3").preview == "AFTER THE REMINDER"
    record = next(r for r in index.preview_cache[("a", "a3")].records if r["slot"] == "llm.input")
    assert record["probes"] == {
        "4": {"role": "system", "text": None, "failed": None},
        "5": {"role": "user", "text": "AFTER THE REMINDER", "failed": None},
    }
    assert row("a3").meta["echo_at"] is None
    # Settled: nothing is read again.
    pending_after = [index.preview_cache[("a", x)].pending for x in ("a1", "a2", "a3")]
    assert pending_after == [None, None, None]


def test_a_failed_repair_read_is_terminal(state, clock, monkeypatch):
    from raven.tracing import artifact_v2

    _append(state, _dialogue(state))
    index = _index(state, clock, preview_reads=2)
    b1 = lambda: index.entry("b:b1:llm.input")  # noqa: E731
    _refresh_until(index, clock, lambda: b1() is not None and b1().meta.get("delta") == "continued")
    answer_sha1 = artifact_v2.message_sha1({"role": "assistant", "content": "the answer"})
    artifact_v2.message_path(state / "logs" / "audit-artifacts", answer_sha1).unlink()
    reads = {"n": 0}
    original = tconv._read_artifact

    def counting(state_dir, path, limit=tconv._ARTIFACT_LIMIT):
        reads["n"] += 1
        return original(state_dir, path, limit)

    monkeypatch.setattr(tconv, "_read_artifact", counting)
    _refresh_until(index, clock, lambda: index.preview_cache[("b", "b1")].pending is None)
    record = next(r for r in index.preview_cache[("b", "b1")].records if r["slot"] == "llm.input")
    assert record["probes"]["2"]["text"] is None and record["probes"]["2"]["failed"]
    # The message after the predecessor could not be read: whether it is an echo stays unknown, for good.
    assert b1().preview == "next question" and b1().meta["delta"] == "continued"
    assert b1().meta["new_from"] == 2 and b1().meta["echo_at"] is None
    _refresh_until(index, clock, lambda: index.index_state().preview_pending == 0)
    settled = reads["n"]
    for _ in range(3):
        index.refresh_sync(None)
        clock.advance(1)
    assert reads["n"] == settled and index.index_state().preview_pending == 0


def test_a_reply_hides_once_the_model_output_it_repeats_is_read(state, clock):
    spans = [
        _turn(
            "t",
            "turn",
            start=0,
            end=10,
            extra={
                "turn.input.artifact_path": _artifact(state, {"content": "go"}, "turn-in"),
                "turn.output.artifact_path": _artifact(state, {"content": "Done."}, "turn-out"),
            },
        ),
        _span(
            "t",
            "l1",
            "llm.call",
            parent="turn",
            start=1,
            end=2,
            attrs={
                "llm.input.artifact_path": _v2_shell(state, "l1-in", [{"role": "user", "content": "go"}]),
                "llm.output.artifact_path": _artifact(state, {"content": "Done."}, "l1-out"),
            },
        ),
    ]
    _append(state, spans)
    index = _index(state, clock, preview_reads=1)
    reply = lambda: index.entry("t:turn:turn.output")  # noqa: E731
    _refresh_until(index, clock, lambda: reply() is not None)
    first_revision = reply().revision
    assert "hidden" not in reply().meta
    _refresh_until(index, clock, lambda: reply().meta.get("hidden") == "redundant_reply")
    assert reply().revision > first_revision and reply().charged_ms == 10000
    assert "t:turn:turn.output" in {e.entry_id for e in index.changes(index.epoch, first_revision, 50).upserts}
