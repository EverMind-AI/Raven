"""Tests for the structured trajectory projection (`raven.trajectory.entries`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.trajectory import conversation as tconv
from raven.trajectory import entries as tent

_T = "2026-09-01T10:00:"


def _ts(seconds: int) -> str:
    return f"{_T}{seconds:02d}+00:00"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    (path / "logs").mkdir(parents=True)
    return path


def _artifact(state: Path, payload, name: str) -> str:
    directory = state / "logs" / "audit-artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _span(trace, span_id, name, *, parent=None, start=0, end=None, attrs=None, status=None, raw_times=None):
    times = raw_times or {}
    span = {
        "traceId": trace,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "startTime": times.get("start", _ts(start)),
        "endTime": times.get("end", _ts(end if end is not None else start)),
        "attributes": attrs if attrs is not None else {},
    }
    if status != "absent":
        span["status"] = status if status is not None else {"code": "OK", "message": ""}
    return span


def _turn_attrs(state, name, content_in="fix the bug", content_out="done"):
    attrs = {"turn.input_preview": content_in[:20]}
    attrs["turn.input.artifact_path"] = _artifact(state, {"content": content_in}, f"{name}-in")
    if content_out is not None:
        attrs["turn.output_preview"] = content_out[:20]
        attrs["turn.output.artifact_path"] = _artifact(state, {"content": content_out}, f"{name}-out")
    return attrs


def _llm_attrs(state, name, messages, output=None, extra=None):
    attrs = {"llm.input.artifact_path": _artifact(state, {"messages": messages, "tools": []}, f"{name}-in")}
    if output is not None:
        attrs["llm.output.artifact_path"] = _artifact(state, output, f"{name}-out")
        preview = output.get("content") if isinstance(output, dict) else None
        if isinstance(preview, str):
            attrs["llm.output_preview"] = preview[:20]
    if extra:
        attrs.update(extra)
    return attrs


def _tool_attrs(state, name, params, result, *, output=True):
    attrs = {
        "tool.name": name,
        "tool.args_preview": json.dumps(params)[:20],
        "tool.input.artifact_path": _artifact(state, {"name": name, "params": params}, f"{name}-in"),
    }
    if output:
        attrs["tool.result_preview"] = str(result)[:20]
        attrs["tool.output.artifact_path"] = _artifact(state, {"result": result}, f"{name}-out")
    return attrs


def _slots(entries, span_id):
    return [e.slot for e in entries if e.span_id == span_id]


def _one(entries, span_id, slot):
    found = [e for e in entries if e.span_id == span_id and e.slot == slot]
    assert len(found) == 1, (span_id, slot, [(e.span_id, e.slot) for e in entries])
    return found[0]


def _messages(*texts):
    return [{"role": "user", "content": t} for t in texts]


# ── timing ────────────────────────────────────────────────────────────


def test_llm_call_charges_its_duration_once(state):
    output = {"content": "answer", "reasoning_content": "thinking hard"}
    spans = [
        _span("t", "turn", "session.turn", start=0, end=10, attrs=_turn_attrs(state, "turn")),
        _span(
            "t",
            "llm",
            "llm.call",
            parent="turn",
            start=1,
            end=3,
            attrs=_llm_attrs(state, "llm", _messages("hi"), output),
        ),
    ]
    entries = tent.project_entries(spans, state=state, read=True).entries
    assert _slots(entries, "llm") == ["llm.input", "llm.thinking", "llm.output"]
    inp, think, out = (_one(entries, "llm", s) for s in ("llm.input", "llm.thinking", "llm.output"))
    assert (inp.charged_ms, inp.timing_basis) == (0, "zero")
    assert (think.charged_ms, think.timing_basis) == (0, "not_recorded")
    assert (out.charged_ms, out.timing_basis, out.duration_ms) == (2000, "span_full", 2000)
    assert {inp.duration_owner, think.duration_owner, out.duration_owner} == {out.entry_id}
    assert out.entry_id == "t:llm:llm.output"
    # Charged time is a composition sum: the 10 s turn overlaps its 2 s child and both count.
    assert sum(e.charged_ms or 0 for e in entries) == 2000 + 10000


def test_parallel_tools_each_keep_their_full_duration(state):
    spans = [
        _span("t", "turn", "session.turn", start=0, end=5, attrs=_turn_attrs(state, "turn")),
        _span("t", "a", "tool.call", parent="turn", start=1, end=3, attrs=_tool_attrs(state, "a", {"x": 1}, "ok")),
        _span("t", "b", "tool.call", parent="turn", start=1, end=3, attrs=_tool_attrs(state, "b", {"x": 2}, "ok")),
    ]
    entries = tent.project_entries(spans, state=state).entries
    outputs = [e for e in entries if e.slot == "tool.output"]
    assert [e.charged_ms for e in outputs] == [2000, 2000]
    assert sum(e.charged_ms or 0 for e in entries) == 4000 + 5000


def test_in_progress_turn_has_unknown_duration_and_runs(state):
    attrs = {**_turn_attrs(state, "turn", content_out=None), "turn.in_progress": True}
    entries = tent.project_entries(
        [_span("t", "turn", "session.turn", start=0, end=4, attrs=attrs)], state=state
    ).entries
    entry = _one(entries, "turn", "turn.input")
    assert entry.operation_status == "running"
    assert entry.status_evidence == ("turn_in_progress",)
    assert entry.duration_ms is None
    assert entry.charged_ms is None and entry.timing_basis == "unknown"
    assert entry.duration_owner is None


def test_clock_skew_and_bad_timestamps_do_not_become_zero(state):
    spans = [
        _span("t", "skew", "tool.call", start=5, end=2, attrs=_tool_attrs(state, "skew", {}, "ok")),
        _span(
            "t",
            "bad",
            "tool.call",
            attrs=_tool_attrs(state, "bad", {}, "ok"),
            raw_times={"start": "yesterday", "end": "later"},
        ),
    ]
    entries = tent.project_entries(spans, state=state).entries
    skew = _one(entries, "skew", "tool.output")
    assert skew.duration_ms is None and skew.charged_ms is None and skew.timing_basis == "unknown"
    assert "clock_skew" in skew.integrity
    bad = _one(entries, "bad", "tool.output")
    assert bad.duration_ms is None and "bad_timestamp" in bad.integrity
    assert bad.event_time == "later"


# ── status and failure carriers ───────────────────────────────────────


def test_error_without_output_gets_a_failure_carrier(state):
    attrs = _tool_attrs(state, "boom", {"x": 1}, None, output=False)
    span = _span("t", "boom", "tool.call", start=1, end=4, attrs=attrs, status={"code": "ERROR", "message": "boom"})
    entries = tent.project_entries([span], state=state).entries
    assert _slots(entries, "boom") == ["tool.input", "error"]
    inp, err = _one(entries, "boom", "tool.input"), _one(entries, "boom", "error")
    assert inp.operation_status == err.operation_status == "error"
    assert err.status_evidence == ("span_error",)
    assert (inp.failure_entry, err.failure_entry) == (False, True)
    assert (err.charged_ms, err.timing_basis) == (3000, "span_full")
    assert inp.duration_owner == err.entry_id == "t:boom:error"
    assert err.kind == "span.error" and err.preview == "boom"


def test_tool_error_attribute_with_input_only_synthesizes_error_entry(state):
    attrs = {**_tool_attrs(state, "quiet", {"x": 1}, None, output=False), "tool.error": "permission denied"}
    entries = tent.project_entries([_span("t", "quiet", "tool.call", start=0, end=2, attrs=attrs)], state=state).entries
    assert _slots(entries, "quiet") == ["tool.input", "error"]
    err = _one(entries, "quiet", "error")
    assert err.status_evidence == ("tool_error",)
    assert err.failure_entry and err.charged_ms == 2000
    assert err.preview == "permission denied"
    assert sum(e.failure_entry for e in entries) == 1


def test_error_prefixed_preview_without_output_artifact_is_business_failure(state):
    attrs = {**_tool_attrs(state, "prev", {"x": 1}, None, output=False), "tool.result_preview": "Error: nope"}
    entries = tent.project_entries([_span("t", "prev", "tool.call", start=0, end=1, attrs=attrs)], state=state).entries
    assert _slots(entries, "prev") == ["tool.input", "tool.output"]
    out = _one(entries, "prev", "tool.output")
    assert out.status_evidence == ("tool_result_error",)
    assert out.failure_entry and out.charged_ms == 1000
    assert "artifact_missing" in out.integrity and out.preview == "Error: nope"
    assert sum(e.failure_entry for e in entries) == 1


def test_identical_error_texts_keep_evidence_from_payload_not_display(state):
    attrs = _tool_attrs(state, "same", "Error: dup", "Error: dup")
    entries = tent.project_entries(
        [_span("t", "same", "tool.call", start=0, end=1, attrs=attrs)], state=state, read=True
    ).entries
    inp, out = _one(entries, "same", "tool.input"), _one(entries, "same", "tool.output")
    assert out.status_evidence == ("tool_result_error",)
    assert (inp.failure_entry, out.failure_entry) == (False, True)
    assert inp.preview == "same Error: dup" and out.preview == "same: Error: dup"
    assert "same content as" not in (inp.preview or "")


def test_ok_requires_an_explicit_ok_status(state):
    spans = [
        _span("t", "none", "tool.call", start=0, end=1, attrs=_tool_attrs(state, "none", {}, "ok"), status="absent"),
        _span("t", "empty", "tool.call", start=0, end=1, attrs=_tool_attrs(state, "empty", {}, "ok"), status={}),
        _span(
            "t",
            "weird",
            "tool.call",
            start=0,
            end=1,
            attrs=_tool_attrs(state, "weird", {}, "ok"),
            status={"code": "WEIRD"},
        ),
        _span("t", "fine", "tool.call", start=0, end=1, attrs=_tool_attrs(state, "fine", {}, "ok")),
    ]
    entries = tent.project_entries(spans, state=state).entries
    for span_id in ("none", "empty", "weird"):
        entry = _one(entries, span_id, "tool.output")
        assert entry.operation_status == "unknown", span_id
        assert entry.duration_ms == 1000
    assert _one(entries, "fine", "tool.output").operation_status == "ok"


def test_outer_only_evidence_for_swallowing_types(state):
    spans = [
        _span("t", "fb", "memory.feedback", attrs={"memory.session_id": "s", "memory.injected": ["a"]}),
        _span("t", "pz", "personalize.classify", attrs={"personalize.step": "classify", "personalize.ok": True}),
    ]
    entries = tent.project_entries(spans, state=state).entries
    assert _one(entries, "fb", "summary").status_evidence == ("outer_only",)
    assert _one(entries, "pz", "summary").operation_status == "ok"
    assert _one(entries, "pz", "summary").status_evidence == ("outer_only",)


# ── snapshot merging and identity ─────────────────────────────────────


def test_checkpoint_and_final_collapse_into_one_logical_span(state):
    final_attrs = _turn_attrs(state, "turn")
    checkpoint = _span("t", "turn", "session.turn", start=0, end=1, attrs={**final_attrs, "turn.in_progress": True})
    final = _span("t", "turn", "session.turn", start=0, end=6, attrs=final_attrs)
    entries = tent.project_entries([checkpoint, final], state=state).entries
    assert _slots(entries, "turn") == ["turn.input", "turn.output"]
    assert _one(entries, "turn", "turn.output").charged_ms == 6000
    assert _one(entries, "turn", "turn.input").operation_status == "ok"


def test_late_in_progress_snapshot_does_not_revert_terminal_state(state):
    final_attrs = _turn_attrs(state, "turn")
    final = _span("t", "turn", "session.turn", start=0, end=6, attrs=final_attrs)
    late = _span("t", "turn", "session.turn", start=0, end=2, attrs={**final_attrs, "turn.in_progress": True})
    merged = tent.merge_snapshots([final, late])
    assert len(merged) == 1 and merged[0]["endTime"] == _ts(6)
    entries = tent.project_entries([final, late], state=state).entries
    assert _one(entries, "turn", "turn.output").operation_status == "ok"
    assert _one(entries, "turn", "turn.output").charged_ms == 6000


def test_entry_ids_stay_stable_as_data_arrives(state):
    attrs = _tool_attrs(state, "tool", {"x": 1}, "ok")
    first = tent.project_entries([_span("t", "tool", "tool.call", start=0, end=1, attrs=attrs)], state=state).entries
    later = tent.project_entries(
        [
            _span("t", "tool", "tool.call", start=0, end=1, attrs=attrs),
            _span("t", "turn", "session.turn", start=0, end=3, attrs=_turn_attrs(state, "turn")),
        ],
        state=state,
    ).entries
    assert {e.entry_id for e in first} <= {e.entry_id for e in later}
    assert {e.entry_id for e in first} == {"t:tool:tool.input", "t:tool:tool.output"}


def test_missing_artifact_recovery_keeps_identity(state):
    attrs = _tool_attrs(state, "lazy", {"x": 1}, "ok")
    missing = Path(attrs["tool.output.artifact_path"])
    content = missing.read_text(encoding="utf-8")
    missing.unlink()
    span = _span("t", "lazy", "tool.call", start=0, end=1, attrs=attrs)
    before = _one(tent.project_entries([span], state=state, read=True).entries, "lazy", "tool.output")
    assert "artifact_missing" in before.integrity and before.preview == "ok"
    missing.write_text(content, encoding="utf-8")
    after = _one(tent.project_entries([span], state=state, read=True).entries, "lazy", "tool.output")
    assert after.entry_id == before.entry_id
    assert after.integrity == ("turn_unknown",)


def test_broken_records_get_positional_identity(state):
    spans = [
        {"name": "tool.call", "startTime": _ts(0), "endTime": _ts(1), "attributes": {}},
        _span("t", "corrupt", "tool.call", start=0, end=1, attrs="not-a-dict"),
    ]
    entries = tent.project_entries(spans, state=state).entries
    broken = [e for e in entries if e.entry_id.startswith("broken:")]
    assert len(broken) == 1 and broken[0].entry_id == "broken:0:summary"
    assert broken[0].kind == "tool.call.summary"
    assert "malformed_span" in broken[0].integrity
    corrupt = _one(entries, "corrupt", "malformed")
    assert "malformed_span" in corrupt.integrity and corrupt.kind == "span.malformed"
    stamped = tent.merge_snapshots(spans)[0]
    assert stamped[tent.POSITION_KEY] == 0


def test_broken_record_with_several_slots_keeps_unique_ids(state):
    attrs = {"tool.name": "x", "tool.args_preview": "{}", "tool.result_preview": "ok"}
    spans = [
        {
            "traceId": "t",
            "spanId": None,
            "name": "tool.call",
            "startTime": _ts(0),
            "endTime": _ts(1),
            "attributes": attrs,
        },
        {
            "spanId": "s",
            "name": "tool.call",
            "startTime": _ts(0),
            "endTime": _ts(1),
            "attributes": attrs,
            "status": "bad",
        },
    ]
    entries = tent.project_entries(spans, state=state).entries
    ids = [e.entry_id for e in entries]
    assert len(ids) == len(set(ids)), ids
    assert sorted(ids) == [
        "broken:0:tool.input",
        "broken:0:tool.output",
        "broken:1:malformed",
        "broken:1:tool.input",
        "broken:1:tool.output",
    ]
    for entry in entries:
        assert "malformed_span" in entry.integrity
    assert _one([e for e in entries if e.entry_id.startswith("broken:0")], "", "tool.input").duration_owner == (
        "broken:0:tool.output"
    )


def test_merge_snapshots_never_collides_with_stamped_positions():
    base = {"name": "memory.enqueue", "startTime": _ts(0), "endTime": _ts(1), "attributes": {}}
    kept = {**base, tent.POSITION_KEY: 1}
    fresh = {**base}
    merged = tent.merge_snapshots([kept, fresh])
    assert len(merged) == 2
    assert [m[tent.POSITION_KEY] for m in merged] == [1, 0]
    reversed_merge = tent.merge_snapshots([fresh, {**base, tent.POSITION_KEY: 0}])
    assert [m[tent.POSITION_KEY] for m in reversed_merge] == [1, 0]
    again = tent.merge_snapshots([*merged, {**base}])
    assert [m[tent.POSITION_KEY] for m in again] == [1, 0, 2]


# ── turns and origin ───────────────────────────────────────────────────


def test_turn_numbering_and_turn_start(state):
    spans = [
        _span("t3", "c", "session.turn", start=20, end=21, attrs=_turn_attrs(state, "c")),
        _span("t1", "a", "session.turn", start=0, end=1, attrs=_turn_attrs(state, "a")),
        _span("t1", "a-tool", "tool.call", parent="a", start=0, end=1, attrs=_tool_attrs(state, "a-tool", {}, "ok")),
        _span("t2", "b", "session.turn", start=10, end=11, attrs=_turn_attrs(state, "b")),
    ]
    projection = tent.project_entries(spans, state=state)
    assert [(t.turn_span_id, t.number) for t in projection.turns] == [("a", 1), ("b", 2), ("c", 3)]
    starts = [(e.turn_number, e.span_id, e.slot) for e in projection.entries if e.turn_start]
    assert starts == [(1, "a", "turn.input"), (2, "b", "turn.input"), (3, "c", "turn.input")]
    assert _one(projection.entries, "a-tool", "tool.input").turn_number == 1


def test_span_without_turn_ancestor_is_turn_unknown(state):
    entries = tent.project_entries([_span("t", "orphan", "memory.enqueue")], state=state).entries
    entry = _one(entries, "orphan", "summary")
    assert entry.turn_number is None and entry.turn_span_id is None
    assert "turn_unknown" in entry.integrity and not entry.turn_start


def test_dispatched_trace_is_subagent_origin(state):
    spans = [
        _span("main", "turn", "session.turn", start=0, end=9, attrs=_turn_attrs(state, "turn")),
        _span(
            "sub",
            "sub-turn",
            "session.turn",
            start=1,
            end=5,
            attrs={
                **_turn_attrs(state, "sub"),
                "trace.dispatched_in_trace_id": "main",
                "trace.dispatched_by_span_id": "turn",
            },
        ),
        _span(
            "sub",
            "sub-llm",
            "llm.call",
            parent="sub-turn",
            start=2,
            end=3,
            attrs=_llm_attrs(state, "sub-llm", _messages("x"), {"content": "y"}),
        ),
    ]
    entries = tent.project_entries(spans, state=state).entries
    assert {e.origin for e in entries if e.trace_id == "sub"} == {"subagent"}
    assert {e.origin for e in entries if e.trace_id == "main"} == {"main"}


# ── type projection ───────────────────────────────────────────────────


def test_kinds_slots_and_owners_across_span_types(state):
    spans = [
        _span("t", "turn", "session.turn", start=0, end=30, attrs=_turn_attrs(state, "turn")),
        _span(
            "t",
            "llm",
            "llm.call",
            parent="turn",
            start=1,
            end=2,
            attrs=_llm_attrs(state, "llm", _messages("q"), {"content": "a"}),
        ),
        _span("t", "tool", "tool.call", parent="turn", start=2, end=3, attrs=_tool_attrs(state, "tool", {}, "ok")),
        _span(
            "t",
            "read",
            "skill.read",
            parent="turn",
            start=3,
            end=4,
            attrs={"skill.name": "pdf", "tool.output.artifact_path": _artifact(state, {"result": "SKILL"}, "read")},
        ),
        _span(
            "t",
            "inject",
            "skill.inject",
            parent="turn",
            start=4,
            end=5,
            attrs={"skill.inject.artifact_path": _artifact(state, {"via": "x", "skills": [{"name": "s"}]}, "inject")},
        ),
        _span(
            "t",
            "rewrite",
            "skill.rewrite",
            parent="turn",
            start=5,
            end=6,
            attrs={
                "skill.rewrite.input.artifact_path": _artifact(state, {"query": "q"}, "rw-in"),
                "skill.rewrite.output.artifact_path": _artifact(state, {"rewritten_query": "r"}, "rw-out"),
            },
        ),
        _span(
            "t",
            "gate",
            "skill.gate",
            parent="turn",
            start=6,
            end=7,
            attrs={"skill.gate.input.artifact_path": _artifact(state, {"task": "t"}, "gate-in")},
        ),
        _span(
            "t",
            "curate",
            "context.curate",
            parent="turn",
            start=7,
            end=8,
            attrs={"context.curate.output.artifact_path": _artifact(state, {"produced": 1}, "curate-out")},
        ),
        _span(
            "t",
            "sub",
            "subagent.run",
            parent="turn",
            start=8,
            end=9,
            attrs={"subagent.task": "do", "subagent.label": "helper", "subagent.task_id": "k1"},
        ),
        _span(
            "t",
            "recall",
            "memory.recall",
            parent="turn",
            start=9,
            end=10,
            attrs={"memory.recall.artifact_path": _artifact(state, {"hits": []}, "recall")},
        ),
        _span("t", "store", "memory.store", parent="turn", start=10, end=11, attrs={"memory.session_id": "s"}),
        _span("t", "fb", "memory.feedback", parent="turn", start=11, end=12, attrs={"memory.injected": ["a"]}),
        _span(
            "t",
            "pz",
            "personalize.classify",
            parent="turn",
            start=12,
            end=13,
            attrs={
                "personalize.input.artifact_path": _artifact(state, {"message": "m"}, "pz-in"),
                "personalize.output.artifact_path": _artifact(state, {"result": {}}, "pz-out"),
            },
        ),
        _span(
            "t",
            "plugin",
            "plugin.load",
            parent="turn",
            start=13,
            end=14,
            attrs={"plugin.name": "p", "plugin.contribution": "tool"},
        ),
        _span(
            "t",
            "foo",
            "foo.bar",
            parent="turn",
            start=14,
            end=16,
            attrs={
                "foo.alpha.artifact_path": _artifact(state, {"a": 1}, "foo-a"),
                "foo.beta.artifact_path": _artifact(state, {"b": 2}, "foo-b"),
            },
        ),
    ]
    entries = tent.project_entries(spans, state=state, read=True).entries
    expect = {
        "turn": [("turn.input", "user.input"), ("turn.output", "agent.reply")],
        "llm": [("llm.input", "llm.input"), ("llm.output", "llm.output")],
        "tool": [("tool.input", "tool.input"), ("tool.output", "tool.output")],
        "read": [("skill.read", "skill.read")],
        "inject": [("skill.inject", "skill.inject")],
        "rewrite": [("io.input", "skill.rewrite.input"), ("io.output", "skill.rewrite.output")],
        "gate": [("io.input", "skill.gate.input")],
        "curate": [("io.output", "context.curate.output")],
        "sub": [("subagent.run", "subagent.run")],
        "recall": [("artifact:memory.recall", "memory.recall")],
        "store": [("summary", "memory.store.summary")],
        "fb": [("summary", "memory.feedback.summary")],
        "pz": [("io.input", "personalize.classify.input"), ("io.output", "personalize.classify.output")],
        "plugin": [("summary", "plugin.load.summary")],
        "foo": [("artifact:foo.alpha", "foo.alpha"), ("artifact:foo.beta", "foo.beta")],
    }
    for span_id, pairs in expect.items():
        got = sorted((e.slot, e.kind) for e in entries if e.span_id == span_id)
        assert got == sorted(pairs), span_id
    owners = {
        "turn": "turn.output",
        "llm": "llm.output",
        "tool": "tool.output",
        "read": "skill.read",
        "inject": "skill.inject",
        "rewrite": "io.output",
        "curate": "io.output",
        "sub": "subagent.run",
        "recall": "artifact:memory.recall",
        "store": "summary",
        "pz": "io.output",
        "plugin": "summary",
        "foo": "artifact:foo.beta",
    }
    for span_id, slot in owners.items():
        owner = _one(entries, span_id, slot)
        assert owner.timing_basis == "span_full", span_id
        assert all(e.duration_owner == owner.entry_id for e in entries if e.span_id == span_id), span_id
    assert _one(entries, "foo", "artifact:foo.alpha").timing_basis == "shared"
    assert _one(entries, "foo", "artifact:foo.alpha").charged_ms == 0
    gate = _one(entries, "gate", "io.input")
    assert gate.charged_ms is None and gate.timing_basis == "unknown" and gate.duration_owner is None
    assert _one(entries, "sub", "subagent.run").meta == {"label": "helper", "task_id": "k1"}
    assert _one(entries, "read", "skill.read").meta == {"skill": "pdf"}


def test_generic_owner_prefers_output_phase_over_key_order(state):
    both = {
        "foo.output.artifact_path": _artifact(state, {"o": 1}, "g-out"),
        "z.input.artifact_path": _artifact(state, {"i": 1}, "g-in"),
    }
    only_input = {"z.input.artifact_path": _artifact(state, {"i": 1}, "g-in2")}
    spans = [
        _span("t", "both", "foo.bar", start=0, end=2, attrs=both),
        _span("t", "input", "foo.bar", start=0, end=2, attrs=only_input),
        _span("t", "failed", "foo.bar", start=0, end=3, attrs=only_input, status={"code": "ERROR", "message": "x"}),
    ]
    entries = tent.project_entries(spans, state=state).entries
    out = _one(entries, "both", "artifact:foo.output")
    inp = _one(entries, "both", "artifact:z.input")
    assert (out.charged_ms, out.timing_basis) == (2000, "span_full")
    assert (inp.charged_ms, inp.timing_basis) == (0, "zero") and inp.duration_owner == out.entry_id
    lonely = _one(entries, "input", "artifact:z.input")
    assert lonely.charged_ms is None and lonely.timing_basis == "unknown" and lonely.duration_owner is None
    assert _slots(entries, "failed") == ["artifact:z.input", "error"]
    err = _one(entries, "failed", "error")
    assert err.failure_entry and err.charged_ms == 3000
    assert not _one(entries, "failed", "artifact:z.input").failure_entry


def test_in_progress_snapshot_with_error_keeps_unknown_duration(state):
    attrs = {**_turn_attrs(state, "turn", content_out=None), "turn.in_progress": True}
    span = _span("t", "turn", "session.turn", start=0, end=2, attrs=attrs, status={"code": "ERROR", "message": "boom"})
    entries = tent.project_entries([span], state=state).entries
    assert _slots(entries, "turn") == ["turn.input", "error"]
    err = _one(entries, "turn", "error")
    assert err.operation_status == "error" and err.status_evidence == ("span_error",)
    assert err.duration_ms is None and err.charged_ms is None and err.timing_basis == "unknown"
    assert err.failure_entry and err.duration_owner == err.entry_id


def test_base_spans_get_a_summary_entry(state):
    spans = [
        _span("t", "enqueue", "memory.enqueue", start=0, end=2),
        _span("t", "foo", "foo.bar", start=0, end=3),
        _span("t", "rich", "memory.enqueue", start=0, end=1, attrs={"memory.queue": "q", "memory.count": 2}),
    ]
    entries = tent.project_entries(spans, state=state).entries
    for span_id, ms in (("enqueue", 2000), ("foo", 3000)):
        assert _slots(entries, span_id) == ["summary"], span_id
        entry = _one(entries, span_id, "summary")
        assert entry.preview is None
        assert entry.charged_ms == ms and entry.timing_basis == "span_full"
        assert entry.duration_owner == entry.entry_id and entry.integrity == ("turn_unknown",)
    rich = _one(entries, "rich", "summary")
    assert rich.preview is not None and "memory.queue" in rich.preview and "\n" not in rich.preview
    assert rich.operation_status == "ok" and rich.status_evidence == ("outer_only",)


# ── read modes and previews ────────────────────────────────────────────


def test_read_false_touches_no_file_and_keeps_preview_tristate(state, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("artifact read attempted")

    monkeypatch.setattr(tconv, "_read_artifact", _boom)
    spans = [
        _span(
            "t", "turn", "session.turn", start=0, end=9, attrs={**_turn_attrs(state, "turn"), "turn.input_preview": ""}
        ),
        _span(
            "t",
            "tool",
            "tool.call",
            parent="turn",
            start=1,
            end=2,
            attrs={**_tool_attrs(state, "tool", {}, "ok"), "tool.result_preview": "ok"},
        ),
        _span(
            "t",
            "llm",
            "llm.call",
            parent="turn",
            start=2,
            end=3,
            attrs=_llm_attrs(
                state, "llm", _messages("q"), {"content": "a"}, extra={"llm.reasoning_preview": "deep thought"}
            ),
        ),
    ]
    entries = tent.project_entries(spans, state=state, read=False).entries
    assert _one(entries, "tool", "tool.output").preview == "ok"
    assert _one(entries, "turn", "turn.input").preview == ""
    llm_in = _one(entries, "llm", "llm.input")
    assert llm_in.preview is None and llm_in.integrity == ()
    assert _one(entries, "turn", "turn.input").integrity == ()
    assert _one(entries, "tool", "tool.output").integrity == ()
    assert _slots(entries, "llm") == ["llm.input", "llm.thinking", "llm.output"]
    think = _one(entries, "llm", "llm.thinking")
    assert think.preview == "deep thought" and think.timing_basis == "not_recorded"


def test_read_true_previews_come_from_full_content(state):
    long_text = "word " * 100
    spans = [
        _span(
            "t",
            "llm",
            "llm.call",
            start=0,
            end=1,
            attrs=_llm_attrs(state, "llm", _messages(long_text), {"content": "a"}),
        )
    ]
    entry = _one(tent.project_entries(spans, state=state, read=True).entries, "llm", "llm.input")
    assert entry.preview is not None
    assert entry.preview.startswith("[user] word word")
    assert len(entry.preview) == tent.PREVIEW_LIMIT and entry.preview.endswith("…")
    assert "\n" not in entry.preview


def test_preview_text_folds_whitespace_and_caps():
    assert tent.preview_text("a\n\n  b\tc") == "a b c"
    assert tent.preview_text("") == ""
    capped = tent.preview_text("x" * 500)
    assert len(capped) == tent.PREVIEW_LIMIT and capped.endswith("…")


# ── ordering ──────────────────────────────────────────────────────────


def test_causal_order_and_reproducible_sort_keys(state):
    output = {"content": "a", "reasoning_content": "r"}
    spans = [
        _span("t", "turn", "session.turn", start=0, end=10, attrs=_turn_attrs(state, "turn")),
        _span(
            "t",
            "llm",
            "llm.call",
            parent="turn",
            start=1,
            end=4,
            attrs=_llm_attrs(state, "llm", _messages("q"), output),
        ),
        _span("t", "tool", "tool.call", parent="turn", start=5, end=5, attrs=_tool_attrs(state, "tool", {}, "ok")),
    ]
    first = tent.project_entries(spans, state=state, read=True).entries
    second = tent.project_entries(list(reversed(spans)), state=state, read=True).entries
    order = [(e.span_id, e.slot) for e in first]
    assert order == [
        ("turn", "turn.input"),
        ("llm", "llm.input"),
        ("llm", "llm.thinking"),
        ("llm", "llm.output"),
        ("tool", "tool.input"),
        ("tool", "tool.output"),
        ("turn", "turn.output"),
    ]
    assert [e.sort_key for e in first] == [e.sort_key for e in second]
    assert [e.entry_id for e in first] == [e.entry_id for e in second]


def test_sort_key_normalizes_timezones(state):
    spans = [
        _span(
            "t",
            "a",
            "tool.call",
            attrs=_tool_attrs(state, "a", {}, "ok"),
            raw_times={"start": "2026-09-01T12:00:00+02:00", "end": "2026-09-01T12:00:01+02:00"},
        ),
        _span(
            "t",
            "b",
            "tool.call",
            attrs=_tool_attrs(state, "b", {}, "ok"),
            raw_times={"start": "2026-09-01T10:00:00+00:00", "end": "2026-09-01T10:00:02+00:00"},
        ),
    ]
    entries = tent.project_entries(spans, state=state).entries
    assert [e.span_id for e in entries if e.slot == "tool.input"] == ["a", "b"]
    assert _one(entries, "a", "tool.input").sort_key[0] == "2026-09-01T10:00:00+00:00"


# ── budgeted preview reads, caches, skeletons ─────────────────────────


def _v2_shell(state: Path, name: str, count: int) -> str:
    import hashlib

    from raven.tracing import artifact_v2

    refs = []
    for i in range(count):
        message = {"role": "user", "content": f"message {i}"}
        sha1 = hashlib.sha1(json.dumps(message, ensure_ascii=False, default=str).encode()).hexdigest()
        path = artifact_v2.message_path(state / "logs" / "audit-artifacts", sha1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(message), encoding="utf-8")
        refs.append({"$msg": sha1})
    shell = {"artifactFormat": artifact_v2.ARTIFACT_FORMAT, "messages": refs, "prompt": refs[-1], "tools": []}
    return _artifact(state, shell, name)


def test_preview_records_reads_each_slot_once_with_slot_specific_sources(state, monkeypatch):
    reads: list[str] = []
    original = tconv._read_artifact

    def counting(state_dir, path, limit=tconv._ARTIFACT_LIMIT):
        reads.append(path)
        return original(state_dir, path, limit)

    monkeypatch.setattr(tconv, "_read_artifact", counting)
    output = {
        "content": "final",
        "tool_calls": [{"id": "1", "name": "f", "arguments": "{}"}],
        "thinking_blocks": [{"thinking": "ponder"}],
    }
    attrs = {
        "llm.output_preview": "final",
        "llm.output.artifact_path": _artifact(state, output, "p-out"),
        "llm.input.artifact_path": _v2_shell(state, "p-in", 50),
    }
    span = _span("t", "llm", "llm.call", start=0, end=1, attrs=attrs)
    budget = tent.ReadBudget(max_reads=10, max_bytes=10 * 1024 * 1024)
    cache = tent.preview_records(span, state=state, budget=budget)
    assert cache.complete and cache.cursor == 2
    assert len(reads) == 3  # input shell, one prompt blob, output
    by_slot = {r["slot"]: r for r in cache.records}
    assert by_slot["llm.input"]["preview_text"] == "message 49"
    assert by_slot["llm.output"]["preview_text"] == "final f {}"
    assert by_slot["llm.thinking"]["preview_text"] == "ponder"
    assert [r["slot"] for r in cache.records] == ["llm.input", "llm.thinking", "llm.output"]
    assert all(set(r) == set(tent._COMPACT_KEYS) for r in cache.records)
    entries = tent.project_entries([span], state=state, cached_records={("t", "llm"): cache.records}).entries
    assert [e.slot for e in entries] == ["llm.input", "llm.thinking", "llm.output"]
    assert _one(entries, "llm", "llm.thinking").preview == "ponder"


def test_preview_records_stops_when_budget_is_taken_and_resumes(state):
    attrs = {f"foo.k{i}.artifact_path": _artifact(state, {"k": i}, f"pr-{i}") for i in range(5)}
    span = _span("t", "foo", "foo.bar", start=0, end=1, attrs=attrs)
    first = tent.preview_records(span, state=state, budget=tent.ReadBudget(max_reads=2, max_bytes=10**7))
    assert not first.complete and first.cursor == 2
    loaded = [r for r in first.records if r["degraded"] is None]
    assert len(loaded) == 2 and loaded[0]["preview_text"] == '{"k": 0}'
    second = tent.preview_records(span, state=state, budget=tent.ReadBudget(max_reads=10, max_bytes=10**7), cache=first)
    assert second is first and second.complete and second.cursor == 5
    denied = tent.preview_records(span, state=state, budget=tent.ReadBudget(max_reads=0, max_bytes=0, minimum_reads=0))
    assert not denied.complete and denied.cursor == 0


def test_preview_records_tool_result_payload_keeps_error_evidence(state):
    attrs = _tool_attrs(state, "t1", {"x": 1}, "Error: nope")
    attrs.pop("tool.result_preview")
    span = _span("t", "t1", "tool.call", start=0, end=1, attrs=attrs)
    cache = tent.preview_records(span, state=state, budget=tent.ReadBudget(10, 10**7))
    output = next(r for r in cache.records if r["slot"] == "tool.output")
    assert output["payload"] == {"result": "Error: nope"[:16]}
    entries = tent.project_entries([span], state=state, cached_records={("t", "t1"): cache.records}).entries
    out = _one(entries, "t1", "tool.output")
    assert out.operation_status == "error" and out.status_evidence == ("tool_result_error",) and out.failure_entry


def test_cached_records_skip_span_expansion(state, monkeypatch):
    attrs = _tool_attrs(state, "t2", {"x": 1}, "ok")
    span = _span("t", "t2", "tool.call", start=0, end=1, attrs=attrs)
    cache = tent.preview_records(span, state=state, budget=tent.ReadBudget(10, 10**7))
    calls = {"n": 0}
    original = tconv.span_records

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tconv, "span_records", counting)
    entries = tent.project_entries([span], state=state, cached_records={("t", "t2"): cache.records}).entries
    assert calls["n"] == 0
    assert _one(entries, "t2", "tool.output").preview == "t2: ok"
    direct = tent.project_entries([span], state=state, read=True).entries
    assert [(e.entry_id, e.preview) for e in entries] == [(e.entry_id, e.preview) for e in direct]


def test_extra_turns_and_skeletons_shape_numbering_without_entries(state):
    skeleton = {
        "traceId": "t",
        "spanId": "turn",
        "parentSpanId": None,
        "name": "session.turn",
        "startTime": _ts(0),
        "endTime": _ts(9),
        "status": {"code": "OK", "message": ""},
        "attributes": {"session.key": "s"},
        tent.SKELETON_KEY: True,
    }
    sub_skeleton = {
        **skeleton,
        "traceId": "S",
        "spanId": "sub",
        "attributes": {"trace.dispatched_in_trace_id": "t"},
        "startTime": _ts(2),
    }
    spans = [
        skeleton,
        sub_skeleton,
        _span("t", "tool", "tool.call", parent="turn", start=1, end=2, attrs=_tool_attrs(state, "tool", {}, "ok")),
        _span(
            "S", "sub-tool", "tool.call", parent="sub", start=3, end=4, attrs=_tool_attrs(state, "sub-tool", {}, "ok")
        ),
    ]
    projection = tent.project_entries(spans, state=state, extra_turns=[("gone", "t", _ts(1)), ("turn", "t", _ts(0))])
    assert [(t.turn_span_id, t.number) for t in projection.turns] == [("turn", 1), ("gone", 2), ("sub", 3)]
    assert not any(e.span_id in ("turn", "sub") for e in projection.entries)
    tool = _one(projection.entries, "tool", "tool.output")
    assert tool.turn_span_id == "turn" and tool.turn_number == 1 and "turn_unknown" not in tool.integrity
    sub_tool = _one(projection.entries, "sub-tool", "tool.output")
    assert sub_tool.origin == "subagent" and sub_tool.turn_number == 3


def test_preview_records_keep_failure_reasons_and_cap_marker(state, tmp_path):
    attrs = {
        "turn.input_preview": "ask",
        "turn.input.artifact_path": str(state / "logs" / "audit-artifacts" / "nowhere.json"),
        "turn.output.artifact_path": str(tmp_path / "outside.json"),
    }
    (tmp_path / "outside.json").write_text("{}", encoding="utf-8")
    span = _span("t", "turn", "session.turn", start=0, end=1, attrs=attrs)
    cache = tent.preview_records(span, state=state, budget=tent.ReadBudget(10, 10**7))
    assert cache.complete
    entries = tent.project_entries([span], state=state, cached_records={("t", "turn"): cache.records}).entries
    inp = _one(entries, "turn", "turn.input")
    assert inp.preview == "ask" and "artifact_missing" in inp.integrity
    out = _one(entries, "turn", "turn.output")
    assert out.preview is None and "artifact_outside_store" in out.integrity


def test_preview_records_bad_json_dir_and_capped_reads(state):
    folder = state / "logs" / "audit-artifacts" / "folder"
    folder.mkdir(parents=True)
    attrs = {
        "foo.raw.artifact_path": _artifact(state, "not json at all", "raw"),
        "foo.dir.artifact_path": str(folder),
        "foo.big.artifact_path": _artifact(state, {"pad": "x" * (100 * 1024)}, "big"),
    }
    span = _span("t", "foo", "foo.bar", start=0, end=1, attrs=attrs)
    cache = tent.preview_records(span, state=state, budget=tent.ReadBudget(10, 10**7))
    entries = tent.project_entries([span], state=state, cached_records={("t", "foo"): cache.records}).entries
    raw = _one(entries, "foo", "artifact:foo.raw")
    assert raw.preview == "not json at all" and "artifact_unreadable" not in raw.integrity
    tool_attrs = {"tool.name": "t", "tool.output.artifact_path": _artifact(state, "not json either", "raw-tool")}
    tool_span = _span("t", "tool", "tool.call", start=0, end=1, attrs=tool_attrs)
    tool_cache = tent.preview_records(tool_span, state=state, budget=tent.ReadBudget(10, 10**7))
    tool_out = _one(
        tent.project_entries([tool_span], state=state, cached_records={("t", "tool"): tool_cache.records}).entries,
        "tool",
        "tool.output",
    )
    assert "artifact_unreadable" in tool_out.integrity and tool_out.preview == "not json either"
    folder_entry = _one(entries, "foo", "artifact:foo.dir")
    assert "artifact_unreadable" in folder_entry.integrity
    big = _one(entries, "foo", "artifact:foo.big")
    assert big.preview is not None and big.preview.startswith('{ "pad"')
    assert "artifact_truncated" not in big.integrity
    assert next(r for r in cache.records if r["slot"] == "artifact:foo.big")["degraded"] == tconv.NOT_LOADED


def test_preview_records_missing_blob_is_reported(state):
    from raven.tracing import artifact_v2

    shell = {
        "artifactFormat": artifact_v2.ARTIFACT_FORMAT,
        "messages": [{"$msg": "a" * 40}],
        "prompt": {"$msg": "a" * 40},
    }
    span = _span(
        "t", "llm", "llm.call", start=0, end=1, attrs={"llm.input.artifact_path": _artifact(state, shell, "shell")}
    )
    cache = tent.preview_records(span, state=state, budget=tent.ReadBudget(10, 10**7))
    entry = _one(
        tent.project_entries([span], state=state, cached_records={("t", "llm"): cache.records}).entries,
        "llm",
        "llm.input",
    )
    assert "blob_missing" in entry.integrity
    assert entry.preview is not None and "missing" in entry.preview


def test_preview_records_resume_v2_blob_with_single_read_budgets(state, monkeypatch):
    reads = {"n": 0}
    original = tconv._read_artifact

    def counting(state_dir, path, limit=tconv._ARTIFACT_LIMIT):
        reads["n"] += 1
        return original(state_dir, path, limit)

    monkeypatch.setattr(tconv, "_read_artifact", counting)
    span = _span("t", "llm", "llm.call", start=0, end=1, attrs={"llm.input.artifact_path": _v2_shell(state, "two", 3)})
    cache = None
    rounds = 0
    while cache is None or not cache.complete:
        rounds += 1
        cache = tent.preview_records(span, state=state, budget=tent.ReadBudget(1, 10**7), cache=cache)
        assert rounds <= 3
    assert rounds == 2 and reads["n"] == 2
    assert cache.pending is None and cache.cursor == 1
    entry = _one(
        tent.project_entries([span], state=state, cached_records={("t", "llm"): cache.records}).entries,
        "llm",
        "llm.input",
    )
    assert entry.preview == "message 2"


# ── tool result errors ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("result", "code"),
    [
        ({"error": "URL validation failed", "detail": "blocked", "url": "https://x"}, "result_error_key"),
        (json.dumps({"error": "URL validation failed", "detail": "blocked"}), "result_error_key"),
        ({"errors": ["a"]}, "result_error_key"),
        ({"is_error": True, "content": "x"}, "result_error_flag"),
        ({"isError": True}, "result_error_flag"),
        ({"ok": False}, "result_error_flag"),
        ({"success": False}, "result_error_flag"),
        ({"status": "failed", "data": 1}, "result_error_status"),
        ({"status": "ERROR"}, "result_error_status"),
        ("Traceback (most recent call last):\n  boom", "tool_result_error"),
        ("Error: nope", "tool_result_error"),
    ],
)
def test_structured_tool_results_are_read_for_errors(state, result, code):
    attrs = _tool_attrs(state, "web_fetch", {"url": "https://x"}, result)
    span = _span("t", "web_fetch", "tool.call", start=0, end=1, attrs=attrs)
    direct = tent.project_entries([span], state=state, read=True).entries
    out = _one(direct, "web_fetch", "tool.output")
    assert out.status_evidence == (code,) and out.failure_entry and out.operation_status == "error"
    cache = tent.preview_records(span, state=state, budget=tent.ReadBudget(10, 10**7))
    cached = tent.project_entries([span], state=state, cached_records={("t", "web_fetch"): cache.records}).entries
    assert _one(cached, "web_fetch", "tool.output").status_evidence == (code,)


@pytest.mark.parametrize(
    "result",
    [
        {"data": {"error": "nested errors are the payload's business"}},
        {"error": None},
        {"errors": []},
        {"ok": True, "status": "done"},
        "all good",
        json.dumps({"results": [{"error": "inside a list"}]}),
    ],
)
def test_tool_results_without_a_top_level_error_stay_ok(state, result):
    attrs = _tool_attrs(state, "search", {"q": 1}, result)
    span = _span("t", "search", "tool.call", start=0, end=1, attrs=attrs)
    for entries in (
        tent.project_entries([span], state=state, read=True).entries,
        tent.project_entries(
            [span],
            state=state,
            cached_records={
                ("t", "search"): tent.preview_records(span, state=state, budget=tent.ReadBudget(10, 10**7)).records
            },
        ).entries,
    ):
        out = _one(entries, "search", "tool.output")
        assert out.status_evidence == () and not out.failure_entry and out.operation_status == "ok"


# ── row spellings ─────────────────────────────────────────────────────


def test_tool_rows_lead_with_the_tool_name_and_model_rows_name_their_calls(state):
    tool_attrs = _tool_attrs(state, "web_fetch", {"url": "https://x"}, {"status": 200})
    output = {"content": "final", "tool_calls": [{"id": "call_9", "name": "web_fetch", "arguments": '{"url": "u"}'}]}
    spans = [
        _span("t", "turn", "session.turn", start=0, end=10, attrs=_turn_attrs(state, "turn")),
        _span(
            "t",
            "llm",
            "llm.call",
            parent="turn",
            start=1,
            end=2,
            attrs=_llm_attrs(state, "llm", _messages("hi"), output),
        ),
        _span("t", "web_fetch", "tool.call", parent="turn", start=3, end=4, attrs=tool_attrs),
    ]
    for entries in (
        tent.project_entries(spans, state=state, read=True).entries,
        tent.project_entries(
            spans,
            state=state,
            cached_records={
                (s["traceId"], s["spanId"]): tent.preview_records(
                    s, state=state, budget=tent.ReadBudget(10, 10**7)
                ).records
                for s in spans
            },
        ).entries,
    ):
        assert _one(entries, "web_fetch", "tool.input").preview == 'web_fetch { "url": "https://x" }'
        assert _one(entries, "web_fetch", "tool.output").preview == 'web_fetch: { "status": 200 }'
        assert _one(entries, "llm", "llm.output").preview == 'final web_fetch {"url": "u"}'


def test_memory_store_rows_show_the_first_question_and_the_last_answer(state):
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "What is X?\nmore detail"},
        {"role": "assistant", "content": "X is a thing."},
        {"role": "user", "content": "And Y?"},
        {"role": "assistant", "content": "Y is another."},
    ]
    attrs = {
        "memory.session_id": "tui:a",
        "memory.message_count": 5,
        "memory.store.artifact_path": _artifact(state, {"session_id": "tui:a", "messages": messages}, "store"),
    }
    span = _span("t", "store", "memory.store", start=0, end=1, attrs=attrs)
    direct = tent.project_entries([span], state=state, read=True).entries
    cached = tent.project_entries(
        [span],
        state=state,
        cached_records={
            ("t", "store"): tent.preview_records(span, state=state, budget=tent.ReadBudget(10, 10**7)).records
        },
    ).entries
    for entries in (direct, cached):
        assert _one(entries, "store", "artifact:memory.store").preview == "What is X? → Y is another."


def test_memory_feedback_rows_count_injected_and_used_skills(state):
    attrs = {"memory.session_id": "tui:a", "memory.injected": ["a", "b"], "memory.used": ["a"]}
    span = _span("t", "fb", "memory.feedback", start=0, end=1, attrs=attrs)
    entry = _one(tent.project_entries([span], state=state, read=True).entries, "fb", "summary")
    assert entry.preview == "injected 2 · used 1"
    assert entry.meta == {"injected": ["a", "b"], "used": ["a"]}
    assert "hidden" not in entry.meta


# ── the message delta ─────────────────────────────────────────────────


def _call(state, trace, span_id, parent, *, start, messages, purpose=None, content="ok"):
    extra = {"llm.purpose": purpose} if purpose else None
    attrs = _llm_attrs(state, span_id, messages, {"content": content}, extra=extra)
    return _span(trace, span_id, "llm.call", parent=parent, start=start, end=start + 1, attrs=attrs)


def _m(role, text):
    return {"role": role, "content": text}


def _delta(entries, span_id):
    meta = _one(entries, span_id, "llm.input").meta
    return meta.get("delta"), meta.get("new_from"), meta.get("message_count")


def test_model_inputs_say_what_they_add_only_when_the_history_proves_it(state):
    s, u1, a1, u2, a2, u3 = (
        _m("system", "rules"),
        _m("user", "q1"),
        _m("assistant", "a1"),
        _m("user", "q2"),
        _m("assistant", "a2"),
        _m("user", "q3"),
    )
    spans = [
        _span("A", "tA", "session.turn", start=0, end=10, attrs=_turn_attrs(state, "tA")),
        _call(state, "A", "a1", "tA", start=1, messages=[s, u1], purpose="main"),
        _span("B", "tB", "session.turn", start=20, end=30, attrs=_turn_attrs(state, "tB")),
        _call(state, "B", "b1", "tB", start=21, messages=[s, u1, a1, u2], purpose="main"),
        _call(state, "B", "b2", "tB", start=22, messages=[_m("user", "is this watched work?")], purpose="watch_work"),
        _call(state, "B", "b3", "tB", start=23, messages=[s, u1, a1, u2], purpose="main"),
        _span("C", "tC", "session.turn", start=40, end=50, attrs=_turn_attrs(state, "tC")),
        _call(state, "C", "c1", "tC", start=41, messages=[s, _m("user", "compacted"), u1], purpose="main"),
        _call(state, "C", "c2", "tC", start=42, messages=[s, u1, a1, u2, a2, u3]),
        _call(state, "C", "c3", "tC", start=43, messages=[_m("user", "A"), _m("user", "B")], purpose="dup"),
        _call(
            state,
            "C",
            "c4",
            "tC",
            start=44,
            messages=[_m("user", "A"), _m("user", "B"), _m("user", "A")],
            purpose="dup",
        ),
        _call(
            state,
            "C",
            "c5",
            "tC",
            start=45,
            messages=[_m("user", "A"), _m("user", "X"), _m("user", "B")],
            purpose="dup",
        ),
    ]
    entries = tent.project_entries(spans, state=state, read=True).entries
    assert _delta(entries, "a1") == ("first", 0, 2)
    assert _delta(entries, "b1") == ("continued", 2, 4)
    # The watch-work question shares nothing with the dialogue: its own conversation, all new.
    assert _delta(entries, "b2") == ("independent", 0, 1)
    assert _delta(entries, "b3") == ("continued", 4, 4)
    # Compaction rewrote the history: no prefix, so nothing is called old.
    assert _delta(entries, "c1") == ("independent", 0, 3)
    # A call that recorded no purpose continues a main call when the prefix holds.
    assert _delta(entries, "c2") == ("continued", 4, 6)
    assert _delta(entries, "c3") == ("independent", 0, 2)
    assert _delta(entries, "c4") == ("continued", 2, 3)
    assert _delta(entries, "c5") == ("independent", 0, 3)
    assert _one(entries, "b1", "llm.input").meta["purpose"] == "main"
    assert _one(entries, "b2", "llm.input").meta["purpose"] == "watch_work"
    assert "purpose" not in _one(entries, "c2", "llm.input").meta


def test_sub_agent_inputs_are_measured_against_their_own_trace(state):
    s, u1, a1, u2 = _m("system", "rules"), _m("user", "q1"), _m("assistant", "a1"), _m("user", "q2")
    spans = [
        _span("A", "tA", "session.turn", start=0, end=30, attrs=_turn_attrs(state, "tA")),
        _call(state, "A", "a1", "tA", start=1, messages=[s, u1, a1, u2], purpose="main"),
        _span(
            "S",
            "sub",
            "session.turn",
            start=2,
            end=20,
            attrs={**_turn_attrs(state, "sub"), "trace.dispatched_in_trace_id": "A"},
        ),
        _call(state, "S", "s1", "sub", start=3, messages=[s, u1], purpose="main"),
        _call(state, "S", "s2", "sub", start=4, messages=[s, u1, a1], purpose="main"),
    ]
    entries = tent.project_entries(spans, state=state, read=True).entries
    assert _one(entries, "s1", "llm.input").origin == "subagent"
    assert _delta(entries, "s1") == ("first", 0, 2)
    assert _delta(entries, "s2") == ("continued", 2, 3)


def test_delta_is_unknown_until_the_predecessor_is_read(state):
    s, u1, a1, u2 = _m("system", "rules"), _m("user", "q1"), _m("assistant", "a1"), _m("user", "q2")
    a = _call(state, "A", "a1", None, start=1, messages=[s, u1], purpose="main")
    b = _call(state, "B", "b1", None, start=2, messages=[s, u1, a1, u2], purpose="main")
    budget = tent.ReadBudget(10, 10**7)
    b_cache = tent.preview_records(b, state=state, budget=budget)
    unread = tent.project_entries([a, b], state=state, cached_records={("B", "b1"): b_cache.records}).entries
    assert _delta(unread, "b1") == ("unknown", None, 4)
    a_cache = tent.preview_records(a, state=state, budget=budget)
    read = tent.project_entries(
        [a, b], state=state, cached_records={("A", "a1"): a_cache.records, ("B", "b1"): b_cache.records}
    ).entries
    assert _delta(read, "b1") == ("continued", 2, 4)
    assert _delta(read, "a1") == ("first", 0, 2)


# ── rows the list need not show ───────────────────────────────────────


def test_replies_repeating_the_last_model_output_are_marked_hidden(state):
    spans = [
        _span("t", "turn", "session.turn", start=0, end=10, attrs=_turn_attrs(state, "turn", content_out="Done.")),
        _call(state, "t", "l1", "turn", start=1, messages=[_m("user", "go")], content="working"),
        _call(state, "t", "l2", "turn", start=2, messages=[_m("user", "go")], content="Done. "),
        _span(
            "u",
            "turn2",
            "session.turn",
            start=20,
            end=30,
            attrs=_turn_attrs(state, "turn2", content_out="Summary of two outputs"),
        ),
        _call(state, "u", "l3", "turn2", start=21, messages=[_m("user", "go")], content="first"),
        _call(state, "u", "l4", "turn2", start=22, messages=[_m("user", "go")], content="second"),
        _span("t", "enq", "memory.enqueue", parent="turn", start=3, end=3),
        _span(
            "t",
            "fb0",
            "memory.feedback",
            parent="turn",
            start=4,
            end=4,
            attrs={"memory.injected": [], "memory.used": []},
        ),
        _span(
            "t",
            "fb1",
            "memory.feedback",
            parent="turn",
            start=5,
            end=5,
            attrs={"memory.injected": ["a"], "memory.used": []},
        ),
    ]
    entries = tent.project_entries(spans, state=state, read=True).entries
    assert _one(entries, "turn", "turn.output").meta.get("hidden") == "redundant_reply"
    assert "hidden" not in _one(entries, "turn2", "turn.output").meta
    assert _one(entries, "enq", "summary").meta.get("hidden") == "empty_internal"
    assert _one(entries, "fb0", "summary").meta.get("hidden") == "empty_internal"
    assert "hidden" not in _one(entries, "fb1", "summary").meta
    # The hidden reply still owns the turn's clock.
    assert _one(entries, "turn", "turn.output").charged_ms == 10000


def test_a_reply_is_not_hidden_while_either_side_is_unread(state):
    turn = _span("t", "turn", "session.turn", start=0, end=10, attrs=_turn_attrs(state, "turn", content_out="Done."))
    call = _call(state, "t", "l1", "turn", start=1, messages=[_m("user", "go")], content="Done.")
    budget = tent.ReadBudget(10, 10**7)
    turn_cache = tent.preview_records(turn, state=state, budget=budget)
    half = tent.project_entries([turn, call], state=state, cached_records={("t", "turn"): turn_cache.records}).entries
    assert "hidden" not in _one(half, "turn", "turn.output").meta
    call_cache = tent.preview_records(call, state=state, budget=budget)
    both = tent.project_entries(
        [turn, call], state=state, cached_records={("t", "turn"): turn_cache.records, ("t", "l1"): call_cache.records}
    ).entries
    assert _one(both, "turn", "turn.output").meta.get("hidden") == "redundant_reply"
