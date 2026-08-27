"""Tests for the trajectory layer: verdict sidecar, pin registry, attempt
definitions, and the span reader."""

from __future__ import annotations

import json
import multiprocessing

import pytest

from raven.trajectory import store as tstore
from raven.trajectory import verdict as tverdict
from raven.utils import atomic_io


def _pin_after_barrier(state_dir, id_, barrier):
    barrier.wait()
    tstore.pin(id_, state_dir=state_dir)


def _unpin_after_barrier(state_dir, id_, barrier):
    barrier.wait()
    if not tstore.unpin(id_, state_dir):
        raise AssertionError(f"missing pin {id_}")


def _merge_after_barrier(state_dir, ids, barrier):
    barrier.wait()
    try:
        tstore.merge_attempts(ids, state_dir=state_dir)
    except ValueError:
        pass


def _split_after_barrier(state_dir, id_, barrier):
    barrier.wait()
    tstore.split_attempt(id_, state_dir=state_dir)


def _write_log(path, spans):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


def _span(trace_id, attempt_id=None, session_key=None, name="session.turn"):
    """Span in the current format; pass ``attempt_id`` for a legacy-format span."""
    attributes = {"session.key": session_key}
    if attempt_id is not None:
        attributes["attempt.id"] = attempt_id
    return {
        "schemaVersion": "audit.span.v1",
        "traceId": trace_id,
        "spanId": f"span-{trace_id}",
        "name": name,
        "attributes": attributes,
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

    def test_filter_params_are_mutually_exclusive(self, tmp_path):
        with pytest.raises(ValueError, match="mutually exclusive"):
            tverdict.read_verdicts(tmp_path, attempt_id="att-1", attempt_ids=("att-2",))
        tverdict.record_verdict("att-1", "pass", source="user", state_dir=tmp_path)
        with pytest.raises(ValueError, match="mutually exclusive"):
            tverdict.read_verdicts(tmp_path, attempt_id="att-1", attempt_ids=("att-1",))

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

    def test_concurrent_processes_do_not_lose_pin_updates(self, tmp_path):
        ctx = multiprocessing.get_context("spawn")
        ids = [f"att-{index}" for index in range(8)]
        barrier = ctx.Barrier(len(ids))
        processes = [ctx.Process(target=_pin_after_barrier, args=(tmp_path, id_, barrier)) for id_ in ids]

        for process in processes:
            process.start()
        for process in processes:
            process.join(15)

        assert all(process.exitcode == 0 for process in processes)
        assert set(tstore.pins(tmp_path)) == set(ids)

    def test_concurrent_processes_do_not_lose_unpin_updates(self, tmp_path):
        ctx = multiprocessing.get_context("spawn")
        ids = [f"att-{index}" for index in range(8)]
        for id_ in ids:
            tstore.pin(id_, state_dir=tmp_path)
        barrier = ctx.Barrier(len(ids))
        processes = [ctx.Process(target=_unpin_after_barrier, args=(tmp_path, id_, barrier)) for id_ in ids]

        for process in processes:
            process.start()
        for process in processes:
            process.join(15)

        assert all(process.exitcode == 0 for process in processes)
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


class TestResolveAttemptId:
    def test_trace_id_resolves_to_canonical_attempt(self, tmp_path):
        _write_log(
            tmp_path / "logs" / "audit-spans.log",
            [_span("trace-1", attempt_id="att-x"), _span("trace-2", attempt_id="att-x")],
        )
        assert tstore.resolve_attempt_id("trace-1", tmp_path) == "att-x"
        assert tstore.resolve_attempt_id("att-x", tmp_path) == "att-x"

    def test_identity_when_spans_lack_attempt_attr(self, tmp_path):
        span = {"traceId": "trace-1", "spanId": "s1", "name": "session.turn", "attributes": {}}
        _write_log(tmp_path / "logs" / "audit-spans.log", [span])
        assert tstore.resolve_attempt_id("trace-1", tmp_path) == "trace-1"

    def test_none_when_no_span_matches(self, tmp_path):
        _write_log(tmp_path / "logs" / "audit-spans.log", [_span("trace-1")])
        assert tstore.resolve_attempt_id("missing", tmp_path) is None


class TestAttemptDefinitions:
    def _log(self, tmp_path, spans):
        _write_log(tmp_path / "logs" / "audit-spans.log", spans)

    def _traces(self, tmp_path, *trace_ids, session_key=None):
        self._log(tmp_path, [_span(t, session_key=session_key) for t in trace_ids])

    def test_merge_creates_definition_and_resolves_members(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        aid = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        assert aid.startswith("att-")
        assert tstore.resolve_attempt_id(aid, tmp_path) == aid
        assert tstore.resolve_attempt_id("trace-a", tmp_path) == aid
        assert tstore.attempt_members(aid, tmp_path) == ("trace-a", "trace-b")
        assert tstore.owning_attempt("trace-b", tmp_path) == aid

    def test_merge_of_merged_attempts_unions_and_accumulates_aliases(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b", "trace-c")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        y = tstore.merge_attempts([x, "trace-c"], state_dir=tmp_path)
        defs = tstore.definitions(tmp_path)
        assert set(defs) == {y}
        assert defs[y]["traces"] == ["trace-a", "trace-b", "trace-c"]
        assert defs[y]["aliases"] == [x]
        assert tstore.resolve_attempt_id(x, tmp_path) == y

    def test_merge_by_member_trace_id_absorbs_owning_definition(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b", "trace-c")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        z = tstore.merge_attempts(["trace-a", "trace-c"], state_dir=tmp_path)
        defs = tstore.definitions(tmp_path)
        assert set(defs) == {z}
        assert defs[z]["traces"] == ["trace-a", "trace-b", "trace-c"]
        assert defs[z]["aliases"] == [x]

    def test_merge_accepts_stale_alias_id_as_input(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b", "trace-c", "trace-d")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        y = tstore.merge_attempts([x, "trace-c"], state_dir=tmp_path)
        z = tstore.merge_attempts([x, "trace-d"], state_dir=tmp_path)
        defs = tstore.definitions(tmp_path)
        assert set(defs) == {z}
        assert defs[z]["traces"] == ["trace-a", "trace-b", "trace-c", "trace-d"]
        assert set(defs[z]["aliases"]) == {x, y}
        assert tstore.resolve_attempt_id(x, tmp_path) == z
        assert tstore.resolve_attempt_id(y, tmp_path) == z

    def test_split_reverts_members_to_per_trace_attempts(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b", "trace-c")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        assert tstore.split_attempt(x, tmp_path) == ("trace-a", "trace-b")
        assert tstore.definitions(tmp_path) == {}
        assert tstore.resolve_attempt_id("trace-a", tmp_path) == "trace-a"

        x2 = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        assert tstore.split_attempt("trace-a", tmp_path) == ("trace-a", "trace-b")
        assert x2 not in tstore.definitions(tmp_path)

        x3 = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        tstore.merge_attempts([x3, "trace-c"], state_dir=tmp_path)
        assert tstore.split_attempt(x3, tmp_path) == ("trace-a", "trace-b", "trace-c")
        assert tstore.split_attempt("nope", tmp_path) is None

    def _legacy_log(self, tmp_path):
        self._log(
            tmp_path,
            [
                _span("trace-l1", attempt_id="att-x"),
                _span("trace-l2", attempt_id="att-x"),
                _span("trace-c"),
            ],
        )

    def test_merge_expands_legacy_attempt_to_real_member_traces(self, tmp_path):
        self._legacy_log(tmp_path)
        y = tstore.merge_attempts(["att-x", "trace-c"], state_dir=tmp_path)
        defs = tstore.definitions(tmp_path)
        assert defs[y]["traces"] == ["trace-l1", "trace-l2", "trace-c"]
        assert defs[y]["aliases"] == ["att-x"]
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path, attempt_id=y)] == [
            "trace-l1",
            "trace-l2",
            "trace-c",
        ]

    def test_merge_preserves_legacy_verdict_and_pin_via_alias(self, tmp_path):
        self._legacy_log(tmp_path)
        tverdict.record_verdict("att-x", "fail", source="user", state_dir=tmp_path)
        tstore.pin("att-x", reason="repro", state_dir=tmp_path)
        y = tstore.merge_attempts(["att-x", "trace-c"], state_dir=tmp_path)
        alias = tstore.attempt_alias_ids(y, tmp_path)
        assert "att-x" in alias
        assert [v.attempt_id for v in tverdict.read_verdicts(tmp_path, attempt_ids=alias)] == ["att-x"]
        assert tstore.is_pinned(_span("trace-l1", attempt_id="att-x"), tmp_path)

    def test_split_of_pure_legacy_attempt_returns_none(self, tmp_path):
        self._legacy_log(tmp_path)
        assert tstore.split_attempt("att-x", tmp_path) is None
        assert tstore.split_attempt("trace-l1", tmp_path) is None

    def test_split_of_definition_with_legacy_members_restores_legacy_grouping(self, tmp_path):
        self._legacy_log(tmp_path)
        y = tstore.merge_attempts(["att-x", "trace-c"], state_dir=tmp_path)
        assert tstore.split_attempt(y, tmp_path) == ("trace-l1", "trace-l2", "trace-c")
        assert tstore.resolve_attempt_id("trace-l1", tmp_path) == "att-x"
        assert tstore.resolve_attempt_id("trace-c", tmp_path) == "trace-c"

    def test_merge_member_of_legacy_group_absorbs_whole_group(self, tmp_path):
        self._legacy_log(tmp_path)
        y = tstore.merge_attempts(["trace-l1", "trace-c"], state_dir=tmp_path)
        defs = tstore.definitions(tmp_path)
        assert defs[y]["traces"] == ["trace-l1", "trace-l2", "trace-c"]
        assert defs[y]["aliases"] == ["att-x"]

    def test_unpin_attempt_clears_legacy_group_after_split(self, tmp_path):
        self._legacy_log(tmp_path)
        y = tstore.merge_attempts(["att-x", "trace-c"], state_dir=tmp_path)
        tstore.pin(y, reason="keep", state_dir=tmp_path)
        tstore.split_attempt(y, tmp_path)
        assert set(tstore.pins(tmp_path)) == {"trace-l1", "trace-l2", "trace-c"}

        assert tstore.unpin_attempt("att-x", tmp_path) is True
        assert set(tstore.pins(tmp_path)) == {"trace-c"}

        tstore.pin("trace-l1", state_dir=tmp_path)
        tstore.pin("trace-l2", state_dir=tmp_path)
        assert tstore.unpin_attempt("trace-l1", tmp_path) is True
        assert set(tstore.pins(tmp_path)) == {"trace-c"}

    def test_merge_rejects_unsafe_legacy_alias_id(self, tmp_path):
        self._log(
            tmp_path,
            [
                _span("trace-l1", attempt_id="../evil"),
                _span("trace-l2", attempt_id="../evil"),
                _span("trace-c"),
            ],
        )
        with pytest.raises(ValueError, match="not a safe identifier"):
            tstore.merge_attempts(["../evil", "trace-c"], state_dir=tmp_path)
        assert tstore.definitions(tmp_path) == {}

    def test_merge_requires_two_distinct_traces(self, tmp_path):
        self._traces(tmp_path, "trace-a")
        with pytest.raises(ValueError):
            tstore.merge_attempts(["trace-a"], state_dir=tmp_path)
        with pytest.raises(ValueError):
            tstore.merge_attempts(["trace-a", "trace-a"], state_dir=tmp_path)
        assert tstore.definitions(tmp_path) == {}

    def test_merge_requires_two_distinct_input_groups(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b", "trace-c")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        y = tstore.merge_attempts([x, "trace-c"], state_dir=tmp_path)
        before = tstore.definitions(tmp_path)
        with pytest.raises(ValueError, match="two distinct attempts"):
            tstore.merge_attempts([y], state_dir=tmp_path)
        with pytest.raises(ValueError, match="two distinct attempts"):
            tstore.merge_attempts([y, x], state_dir=tmp_path)
        with pytest.raises(ValueError, match="two distinct attempts"):
            tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        assert tstore.definitions(tmp_path) == before

    def test_merge_input_resolution_is_order_independent(self, tmp_path):
        results = []
        for order, state in (("forward", tmp_path / "s1"), ("reverse", tmp_path / "s2")):
            _write_log(state / "logs" / "audit-spans.log", [_span(t) for t in ("trace-a", "trace-b", "trace-c")])
            x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=state)
            ids = [x, "trace-c"] if order == "forward" else ["trace-c", x]
            merged = tstore.merge_attempts(ids, state_dir=state)
            entry = tstore.definitions(state)[merged]
            results.append((set(entry["traces"]), entry["aliases"] == [x]))
        assert results[0] == results[1] == ({"trace-a", "trace-b", "trace-c"}, True)

    def test_merge_rejects_unknown_input_id(self, tmp_path):
        self._traces(tmp_path, "trace-a")
        with pytest.raises(ValueError, match="unknown id"):
            tstore.merge_attempts(["trace-a", "nope"], state_dir=tmp_path)
        assert tstore.definitions(tmp_path) == {}

    def test_merge_rejects_empty_input_id(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        with pytest.raises(ValueError, match="non-empty"):
            tstore.merge_attempts(["trace-a", "", "trace-b"], state_dir=tmp_path)
        assert not (tmp_path / "attempts.json").exists()
        assert not (tmp_path / "pins.json").exists()

    def test_merge_rejects_cross_session_members(self, tmp_path):
        self._log(
            tmp_path,
            [
                _span("trace-a", session_key="cli:a"),
                _span("trace-b", session_key="cli:b"),
                _span("trace-c"),
            ],
        )
        with pytest.raises(ValueError, match="different sessions"):
            tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        assert tstore.definitions(tmp_path) == {}
        aid = tstore.merge_attempts(["trace-a", "trace-c"], state_dir=tmp_path)
        assert tstore.attempt_members(aid, tmp_path) == ("trace-a", "trace-c")

    def test_remerge_preserves_pin_and_verdict_of_absorbed_definition(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b", "trace-c")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        tstore.pin(x, reason="px", state_dir=tmp_path)
        tverdict.record_verdict(x, "fail", source="user", state_dir=tmp_path)
        y = tstore.merge_attempts([x, "trace-c"], state_dir=tmp_path)
        assert tstore.is_pinned(_span("trace-a"), tmp_path)
        alias = tstore.attempt_alias_ids(y, tmp_path)
        assert [v.attempt_id for v in tverdict.read_verdicts(tmp_path, attempt_ids=alias)] == [x]

    def test_merge_migrates_member_pins_up_with_joined_reason(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        tstore.pin("trace-a", reason="ra", state_dir=tmp_path)
        tstore.pin("trace-b", reason="rb", state_dir=tmp_path)
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        registry = tstore.pins(tmp_path)
        assert set(registry) == {x}
        assert registry[x]["reason"] == "ra; rb"

    def test_is_pinned_extends_to_definition_members(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        tstore.pin(x, state_dir=tmp_path)
        assert tstore.is_pinned(_span("trace-a"), tmp_path)
        assert not tstore.is_pinned(_span("trace-z"), tmp_path)

    def test_alias_ids_and_member_verdicts_surface(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        tverdict.record_verdict("trace-a", "fail", source="user", state_dir=tmp_path)
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        assert tstore.attempt_alias_ids(x, tmp_path) == (x, "trace-a", "trace-b")
        tverdict.record_verdict(x, "pass", source="user", state_dir=tmp_path)
        got = tverdict.read_verdicts(tmp_path, attempt_ids=tstore.attempt_alias_ids(x, tmp_path))
        assert [v.attempt_id for v in got] == ["trace-a", x]
        assert got[-1].status == "pass"

    def test_split_migrates_pins_to_members_and_clears_old(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        tstore.pin(x, reason="defr", state_dir=tmp_path)
        tstore.split_attempt(x, tmp_path)
        registry = tstore.pins(tmp_path)
        assert set(registry) == {"trace-a", "trace-b"}
        assert registry["trace-a"]["reason"] == "defr"

    def test_split_migration_reads_pins_under_lock_not_stale_snapshot(self, tmp_path, monkeypatch):
        self._traces(tmp_path, "trace-a", "trace-b")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        tstore.pin(x, reason="keep", state_dir=tmp_path)
        # A lock-free pins() reader can be arbitrarily stale; the migration
        # must not depend on it, only on the locked registry.
        monkeypatch.setattr(tstore, "pins", lambda state_dir=None: {})
        assert tstore.split_attempt(x, tmp_path) == ("trace-a", "trace-b")
        monkeypatch.undo()

        registry = tstore.pins(tmp_path)
        assert registry["trace-a"]["reason"] == "keep"
        assert registry["trace-b"]["reason"] == "keep"
        assert x not in registry

    def test_split_preserves_existing_member_pin_reason(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        tstore.pin(x, reason="defr", state_dir=tmp_path)
        tstore.pin("trace-a", reason="mine", state_dir=tmp_path)
        tstore.split_attempt(x, tmp_path)
        registry = tstore.pins(tmp_path)
        assert registry["trace-a"]["reason"] == "mine; defr"
        assert registry["trace-b"]["reason"] == "defr"

    def test_split_does_not_inherit_merged_verdict(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        tverdict.record_verdict(x, "fail", source="user", state_dir=tmp_path)
        tstore.split_attempt(x, tmp_path)
        assert tverdict.read_verdicts(tmp_path, attempt_ids=("trace-a",)) == []
        assert len(tverdict.read_verdicts(tmp_path, attempt_id=x)) == 1

    def test_pin_attempt_resolves_owner_under_attempts_lock(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        assert tstore.pin_attempt("trace-a", reason="keep", state_dir=tmp_path) == x
        assert tstore.pins(tmp_path)[x]["reason"] == "keep"
        assert tstore.is_pinned(_span("trace-b"), tmp_path)

        tstore.split_attempt(x, tmp_path)
        assert tstore.pin_attempt("trace-a", state_dir=tmp_path) == "trace-a"

    def test_pin_attempt_pins_legacy_canonical(self, tmp_path):
        self._legacy_log(tmp_path)
        assert tstore.pin_attempt("trace-l1", reason="repro", state_dir=tmp_path) == "att-x"
        assert tstore.is_pinned(_span("trace-l2", attempt_id="att-x"), tmp_path)

    def test_pin_attempt_refuses_stale_or_unknown_id(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        tstore.split_attempt(x, tmp_path)
        # x is a minted definition id: after the split nothing owns it, so a
        # pin on it would protect nothing — the address must be refused.
        with pytest.raises(LookupError):
            tstore.pin_attempt(x, state_dir=tmp_path)
        with pytest.raises(LookupError):
            tstore.pin_attempt("nope", state_dir=tmp_path)
        assert tstore.pins(tmp_path) == {}

    def test_unpin_attempt_clears_definition_aliases_and_member_pins(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        tstore.pin(x, reason="r", state_dir=tmp_path)
        tstore.split_attempt(x, tmp_path)
        y = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        tstore.pin("trace-a", reason="manual", state_dir=tmp_path)
        assert tstore.unpin_attempt(y, tmp_path) is True
        assert tstore.pins(tmp_path) == {}
        assert not tstore.is_pinned(_span("trace-a"), tmp_path)

    def test_corrupt_definitions_file_reads_empty(self, tmp_path):
        self._traces(tmp_path, "trace-a")
        (tmp_path / "attempts.json").write_text("{broken", encoding="utf-8")
        assert tstore.definitions(tmp_path) == {}
        assert tstore.resolve_attempt_id("trace-a", tmp_path) == "trace-a"

    def test_parse_defs_drops_schema_invalid_entries(self, tmp_path):
        (tmp_path / "attempts.json").write_text(
            json.dumps(
                {
                    "att-ok": {"traces": ["trace-a", "trace-b"]},
                    "att-bad-shape": "not a dict",
                    "att-bad-traces": {"traces": "not-a-list"},
                    "att-too-few": {"traces": ["trace-only", "trace-only"]},
                    "att-empty-member": {"traces": ["trace-x", ""]},
                    "bad/id": {"traces": ["trace-y1", "trace-y2"]},
                    "att-alias-dup": {"traces": ["trace-z1", "trace-z2"], "aliases": ["att-d", "att-d"]},
                    "att-alias-type": {"traces": ["trace-w1", "trace-w2"], "aliases": [123]},
                }
            ),
            encoding="utf-8",
        )
        assert set(tstore.definitions(tmp_path)) == {"att-ok"}

    def test_namespace_conflicts_read_whole_file_empty(self, tmp_path):
        conflicts = [
            {"att-a": {"traces": ["trace-1", "trace-2"]}, "att-b": {"traces": ["trace-2", "trace-3"]}},
            {
                "att-a": {"traces": ["trace-1", "trace-2"], "aliases": ["att-old"]},
                "att-b": {"traces": ["trace-3", "trace-4"], "aliases": ["att-old"]},
            },
            {
                "att-a": {"traces": ["trace-1", "trace-2"], "aliases": ["att-b"]},
                "att-b": {"traces": ["trace-3", "trace-4"]},
            },
            {
                "att-a": {"traces": ["trace-1", "trace-2"], "aliases": ["trace-3"]},
                "att-b": {"traces": ["trace-3", "trace-4"]},
            },
            {"trace-1": {"traces": ["trace-5", "trace-6"]}, "att-b": {"traces": ["trace-1", "trace-4"]}},
            {"att-self": {"traces": ["trace-1", "trace-2"], "aliases": ["att-self"]}},
        ]
        for content in conflicts:
            (tmp_path / "attempts.json").write_text(json.dumps(content), encoding="utf-8")
            assert tstore.definitions(tmp_path) == {}, content

    def test_merge_construction_rejects_invariant_violation(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        with (tmp_path / "logs" / "audit-spans.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(_span("trace-c", attempt_id="trace-a")) + "\n")
            f.write(json.dumps(_span("trace-d")) + "\n")
        with pytest.raises(ValueError, match="invariants"):
            tstore.merge_attempts(["trace-c", "trace-d"], state_dir=tmp_path)
        assert set(tstore.definitions(tmp_path)) == {x}

    def test_iter_spans_attempt_filter_matches_definition_members(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b", "trace-c")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path, attempt_id=x)] == ["trace-a", "trace-b"]
        tstore.merge_attempts([x, "trace-c"], state_dir=tmp_path)
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path, attempt_id=x)] == [
            "trace-a",
            "trace-b",
            "trace-c",
        ]

    def test_iter_spans_member_trace_id_matches_whole_definition(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path, attempt_id="trace-a")] == [
            "trace-a",
            "trace-b",
        ]
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path, trace_id="trace-a")] == ["trace-a"]

    def test_iter_spans_undefined_id_keeps_legacy_behavior(self, tmp_path):
        self._legacy_log(tmp_path)
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path, attempt_id="att-x")] == [
            "trace-l1",
            "trace-l2",
        ]
        assert [s["traceId"] for s in tstore.iter_spans(tmp_path, attempt_id="trace-l1")] == ["trace-l1"]

    def test_resolve_prefers_definition_over_identity_and_legacy(self, tmp_path):
        self._log(
            tmp_path,
            [
                _span("trace-l1", attempt_id="att-x"),
                _span("trace-l2", attempt_id="att-x"),
                _span("trace-z1", attempt_id="att-z"),
                _span("trace-c"),
            ],
        )
        y = tstore.merge_attempts(["att-x", "trace-c"], state_dir=tmp_path)
        assert tstore.resolve_attempt_id("att-x", tmp_path) == y
        assert tstore.resolve_attempt_id("trace-l1", tmp_path) == y
        assert tstore.resolve_attempt_id(y, tmp_path) == y
        assert tstore.resolve_attempt_id("trace-z1", tmp_path) == "att-z"

    def test_concurrent_disjoint_merges_do_not_lose_updates(self, tmp_path):
        trace_ids = [f"trace-{index}" for index in range(16)]
        self._traces(tmp_path, *trace_ids)
        ctx = multiprocessing.get_context("spawn")
        pairs = [[trace_ids[2 * i], trace_ids[2 * i + 1]] for i in range(8)]
        barrier = ctx.Barrier(len(pairs))
        processes = [ctx.Process(target=_merge_after_barrier, args=(tmp_path, pair, barrier)) for pair in pairs]

        for process in processes:
            process.start()
        for process in processes:
            process.join(30)

        assert all(process.exitcode == 0 for process in processes)
        defs = tstore.definitions(tmp_path)
        assert len(defs) == 8
        assert {t for entry in defs.values() for t in entry["traces"]} == set(trace_ids)

    def test_concurrent_overlapping_merges_converge(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b", "trace-c")
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        processes = [
            ctx.Process(target=_merge_after_barrier, args=(tmp_path, ids, barrier))
            for ids in (["trace-a", "trace-b"], ["trace-b", "trace-c"])
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(30)

        assert all(process.exitcode == 0 for process in processes)
        defs = tstore.definitions(tmp_path)
        assert len(defs) == 1
        (entry,) = defs.values()
        assert set(entry["traces"]) == {"trace-a", "trace-b", "trace-c"}
        (absorbed,) = entry["aliases"]
        assert tstore.resolve_attempt_id(absorbed, tmp_path) == next(iter(defs))

    def test_split_between_merge_publish_and_cleanup_keeps_protection(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        tstore.pin("trace-a", reason="ra", state_dir=tmp_path)
        tstore.pin("trace-b", reason="rb", state_dir=tmp_path)
        y, snapshot = tstore._merge_publish(["trace-a", "trace-b"], tmp_path)
        assert set(snapshot) == {"trace-a", "trace-b"}
        assert y in tstore.pins(tmp_path)

        assert tstore.split_attempt(y, tmp_path) == ("trace-a", "trace-b")
        tstore._merge_cleanup(y, snapshot, tmp_path)

        registry = tstore.pins(tmp_path)
        assert "trace-a" in registry and "trace-b" in registry
        assert y not in registry
        assert tstore.definitions(tmp_path) == {}

    def test_merge_cleanup_skipped_when_definition_absorbed(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b", "trace-c")
        tstore.pin("trace-a", reason="ra", state_dir=tmp_path)
        y, snapshot = tstore._merge_publish(["trace-a", "trace-b"], tmp_path)
        z = tstore.merge_attempts([y, "trace-c"], state_dir=tmp_path)
        registry_before = tstore.pins(tmp_path)
        tstore._merge_cleanup(y, snapshot, tmp_path)

        assert tstore.pins(tmp_path) == registry_before
        assert set(tstore.definitions(tmp_path)) == {z}
        assert "trace-a" not in registry_before
        assert z in registry_before and y in registry_before

    def test_merge_cleanup_keeps_repinned_member(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b")
        tstore.pin("trace-a", reason="old", state_dir=tmp_path)
        tstore.pin("trace-b", reason="oldb", state_dir=tmp_path)
        y, snapshot = tstore._merge_publish(["trace-a", "trace-b"], tmp_path)
        tstore.pin("trace-a", reason="new", state_dir=tmp_path)
        tstore._merge_cleanup(y, snapshot, tmp_path)

        registry = tstore.pins(tmp_path)
        assert registry["trace-a"]["reason"] == "new"
        assert "trace-b" not in registry
        assert y in registry

    def test_concurrent_merge_and_split_multiprocess_final_state(self, tmp_path):
        self._traces(tmp_path, "trace-a", "trace-b", "trace-c")
        tstore.pin("trace-a", state_dir=tmp_path)
        tstore.pin("trace-b", state_dir=tmp_path)
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        processes = [
            ctx.Process(target=_split_after_barrier, args=(tmp_path, x, barrier)),
            ctx.Process(target=_merge_after_barrier, args=(tmp_path, [x, "trace-c"], barrier)),
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(30)

        assert all(process.exitcode == 0 for process in processes)
        assert tstore.definitions(tmp_path) == {}
        registry = tstore.pins(tmp_path)
        assert "trace-a" in registry and "trace-b" in registry

    def test_merge_returns_id_when_cleanup_fails(self, tmp_path, monkeypatch):
        self._traces(tmp_path, "trace-a", "trace-b")
        tstore.pin("trace-a", reason="ra", state_dir=tmp_path)
        real = tstore._update_pins
        calls = {"n": 0}

        def flaky(state_dir, mutate):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("pins store unavailable")
            return real(state_dir, mutate)

        monkeypatch.setattr(tstore, "_update_pins", flaky)
        aid = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)

        assert aid in tstore.definitions(tmp_path)
        registry = tstore.pins(tmp_path)
        assert aid in registry
        assert "trace-a" in registry

    def test_merge_validation_failure_writes_no_pins(self, tmp_path):
        self._traces(tmp_path, "trace-a")
        tstore.pin("trace-a", reason="ra", state_dir=tmp_path)
        before = (tmp_path / "pins.json").read_text(encoding="utf-8")
        with pytest.raises(ValueError):
            tstore.merge_attempts(["trace-a", "nope"], state_dir=tmp_path)
        assert (tmp_path / "pins.json").read_text(encoding="utf-8") == before
        assert tstore.definitions(tmp_path) == {}

    def test_merge_rolls_back_definition_pin_when_commit_fails(self, tmp_path, monkeypatch):
        self._traces(tmp_path, "trace-a", "trace-b")
        tstore.pin("trace-a", reason="ra", state_dir=tmp_path)
        real = atomic_io._replace_unlocked

        def failing(path, data):
            if "attempts.json" in path.name:
                raise OSError("disk full")
            real(path, data)

        monkeypatch.setattr(atomic_io, "_replace_unlocked", failing)
        with pytest.raises(OSError, match="disk full"):
            tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)

        assert set(tstore.pins(tmp_path)) == {"trace-a"}
        assert tstore.definitions(tmp_path) == {}

    def test_split_commit_failure_keeps_protection_and_convergence(self, tmp_path, monkeypatch):
        self._traces(tmp_path, "trace-a", "trace-b")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        tstore.pin(x, reason="keep", state_dir=tmp_path)
        real = atomic_io._replace_unlocked

        def failing(path, data):
            if "attempts.json" in path.name:
                raise OSError("disk full")
            real(path, data)

        monkeypatch.setattr(atomic_io, "_replace_unlocked", failing)
        with pytest.raises(OSError, match="disk full"):
            tstore.split_attempt(x, tmp_path)
        monkeypatch.undo()

        assert set(tstore.definitions(tmp_path)) == {x}
        registry = tstore.pins(tmp_path)
        assert registry["trace-a"]["reason"] == "keep"
        assert registry["trace-b"]["reason"] == "keep"
        assert x not in registry
        assert tstore.unpin_attempt(x, tmp_path) is True
        assert tstore.pins(tmp_path) == {}

    def test_new_attempt_id_collision_regenerates(self, tmp_path, monkeypatch):
        self._traces(tmp_path, "trace-a", "trace-b", "trace-c", "trace-d")
        x = tstore.merge_attempts(["trace-a", "trace-b"], state_dir=tmp_path)
        minted = iter([x, "att-fresh-1"])
        monkeypatch.setattr(tstore, "new_attempt_id", lambda: next(minted))
        aid = tstore.merge_attempts(["trace-c", "trace-d"], state_dir=tmp_path)

        assert aid == "att-fresh-1"
        defs = tstore.definitions(tmp_path)
        assert defs[x]["traces"] == ["trace-a", "trace-b"]
        assert defs["att-fresh-1"]["traces"] == ["trace-c", "trace-d"]


def test_end_to_end_with_real_tracer(tmp_path, monkeypatch):
    """Two live-tracer turns merged into one definition are addressable as one attempt."""
    from raven.tracing import spans as _spans
    from raven.tracing import trace

    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))
    _spans._store = None
    try:
        with trace.span("session.turn", session_key="cli:e2e") as t1:
            with trace.span("tool.call"):
                pass
        with trace.span("session.turn", session_key="cli:e2e") as t2:
            pass
        aid = tstore.merge_attempts([t1.trace_id, t2.trace_id], state_dir=tmp_path)

        got = list(tstore.iter_spans(tmp_path, attempt_id=aid))
        assert {s["name"] for s in got} == {"session.turn", "tool.call"}
        assert {s["traceId"] for s in got} == {t1.trace_id, t2.trace_id}
        tverdict.record_verdict(aid, "fail", source="user", state_dir=tmp_path)
        assert tverdict.latest_verdict(aid, tmp_path).status == "fail"
        tstore.pin(aid, reason="repro", state_dir=tmp_path)
        assert all(tstore.is_pinned(s, tmp_path) for s in got)
    finally:
        _spans._store = None
