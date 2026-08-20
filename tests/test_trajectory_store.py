"""Tests for the trajectory layer: verdict sidecar, pin registry, span reader."""

from __future__ import annotations

import json

import pytest

from raven.trajectory import store as tstore
from raven.trajectory import verdict as tverdict


def _write_log(path, spans):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


def _span(trace_id, attempt_id=None, session_key=None, name="session.turn"):
    return {
        "schemaVersion": "audit.span.v1",
        "traceId": trace_id,
        "spanId": f"span-{trace_id}",
        "name": name,
        "attributes": {"attempt.id": attempt_id or trace_id, "session.key": session_key},
    }


class TestVerdicts:
    def test_record_and_read_roundtrip(self, tmp_path):
        v = tverdict.record_verdict("att-1", "fail", source="user", why="wrong answer", state_dir=tmp_path)
        assert v.attempt_id == "att-1" and v.ts
        got = tverdict.read_verdicts(tmp_path)
        assert [x.attempt_id for x in got] == ["att-1"]
        assert got[0].why == "wrong answer"

    def test_verdicts_accumulate_and_latest_wins(self, tmp_path):
        tverdict.record_verdict("att-1", "fail", source="user", state_dir=tmp_path)
        tverdict.record_verdict("att-1", "infra", source="judge", state_dir=tmp_path)
        tverdict.record_verdict("att-2", "pass", source="eval", state_dir=tmp_path)
        assert len(tverdict.read_verdicts(tmp_path)) == 3
        assert len(tverdict.read_verdicts(tmp_path, attempt_id="att-1")) == 2
        assert tverdict.latest_verdict("att-1", tmp_path).status == "infra"
        assert tverdict.latest_verdict("att-missing", tmp_path) is None

    def test_invalid_input_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            tverdict.record_verdict("att-1", "maybe", source="user", state_dir=tmp_path)
        with pytest.raises(ValueError):
            tverdict.record_verdict("", "pass", source="user", state_dir=tmp_path)
        with pytest.raises(ValueError):
            tverdict.record_verdict("att-1", "pass", source="", state_dir=tmp_path)

    def test_bad_lines_are_skipped(self, tmp_path):
        tverdict.record_verdict("att-1", "pass", source="user", state_dir=tmp_path)
        with (tmp_path / "verdicts.jsonl").open("a", encoding="utf-8") as f:
            f.write("not json\n")
            f.write('{"attempt_id": "att-2"}\n')  # missing required fields
        got = tverdict.read_verdicts(tmp_path)
        assert [x.attempt_id for x in got] == ["att-1"]


class TestPins:
    def test_pin_unpin_roundtrip(self, tmp_path):
        tstore.pin("att-1", reason="bug #42", state_dir=tmp_path)
        tstore.pin("trace-9", state_dir=tmp_path)
        registry = tstore.pins(tmp_path)
        assert set(registry) == {"att-1", "trace-9"}
        assert registry["att-1"]["reason"] == "bug #42"
        assert tstore.unpin("trace-9", tmp_path) is True
        assert tstore.unpin("trace-9", tmp_path) is False
        assert set(tstore.pins(tmp_path)) == {"att-1"}

    def test_is_pinned_matches_attempt_or_trace_id(self, tmp_path):
        tstore.pin("att-1", state_dir=tmp_path)
        tstore.pin("trace-2", state_dir=tmp_path)
        assert tstore.is_pinned(_span("trace-1", attempt_id="att-1"), tmp_path)
        assert tstore.is_pinned(_span("trace-2"), tmp_path)
        assert not tstore.is_pinned(_span("trace-3"), tmp_path)

    def test_corrupt_registry_reads_empty(self, tmp_path):
        (tmp_path / "pins.json").write_text("{broken", encoding="utf-8")
        assert tstore.pins(tmp_path) == {}


class TestIterSpans:
    def _populate(self, tmp_path):
        archive = tmp_path / "logs" / "archive"
        _write_log(
            archive / "2026-08-18" / "audit-spans-2026-08-18-010101.log",
            [_span("trace-1", session_key="cli:a")],
        )
        _write_log(
            archive / "2026-08-19" / "audit-spans-2026-08-19-020202.log",
            [_span("trace-2", attempt_id="att-x", session_key="cli:a")],
        )
        _write_log(
            tmp_path / "logs" / "audit-spans.log",
            [
                _span("trace-3", attempt_id="att-x", session_key="cli:a"),
                _span("trace-4", session_key="cli:b"),
            ],
        )

    def test_reads_archive_then_active_in_write_order(self, tmp_path):
        self._populate(tmp_path)
        got = [s["traceId"] for s in tstore.iter_spans(tmp_path)]
        assert got == ["trace-1", "trace-2", "trace-3", "trace-4"]

    def test_attempt_filter_spans_rotation_and_matches_trace_id(self, tmp_path):
        self._populate(tmp_path)
        # A multi-turn attempt is reassembled across archived + active logs.
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path, attempt_id="att-x")] == ["trace-2", "trace-3"]
        # A single-turn attempt is addressed by its trace id.
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path, attempt_id="trace-1")] == ["trace-1"]

    def test_session_and_trace_filters(self, tmp_path):
        self._populate(tmp_path)
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path, session_key="cli:b")] == ["trace-4"]
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path, trace_id="trace-2")] == ["trace-2"]

    def test_bad_lines_skipped_and_missing_dir_empty(self, tmp_path):
        assert list(tstore.iter_spans(tmp_path)) == []
        log = tmp_path / "logs" / "audit-spans.log"
        log.parent.mkdir(parents=True)
        log.write_text('not json\n["a list"]\n' + json.dumps(_span("trace-1")) + "\n", encoding="utf-8")
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path)] == ["trace-1"]


def test_end_to_end_with_real_tracer(tmp_path, monkeypatch):
    """Spans written by the live tracer are addressable through iter_spans."""
    from raven.tracing import spans as _spans
    from raven.tracing import trace

    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))
    _spans._store = None
    try:
        aid = trace.begin_attempt("cli:e2e")
        with trace.span("session.turn", session_key="cli:e2e"):
            with trace.span("tool.call"):
                pass
        trace.end_attempt("cli:e2e")

        got = list(tstore.iter_spans(tmp_path, attempt_id=aid))
        assert {s["name"] for s in got} == {"session.turn", "tool.call"}
        tverdict.record_verdict(aid, "fail", source="user", state_dir=tmp_path)
        assert tverdict.latest_verdict(aid, tmp_path).status == "fail"
        tstore.pin(aid, reason="repro", state_dir=tmp_path)
        assert all(tstore.is_pinned(s, tmp_path) for s in got)
    finally:
        _spans._store = None
