"""Tests for the trajectory bundle collector (`raven.trajectory.bundle`)."""

from __future__ import annotations

import json
import shutil

import pytest

from raven.session.manager import SessionManager
from raven.trajectory import bundle as tbundle
from raven.trajectory import store as tstore
from raven.trajectory import verdict as tverdict


def _write_log(path, spans):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


def _span(trace_id, *, attempt_id=None, session_key=None, name="session.turn", start=None, end=None, attrs=None):
    attributes = {"attempt.id": attempt_id or trace_id, "session.key": session_key}
    if attrs:
        attributes.update(attrs)
    return {
        "schemaVersion": "audit.span.v1",
        "traceId": trace_id,
        "spanId": f"span-{trace_id}-{name}",
        "name": name,
        "startTime": start or "2026-08-20T10:00:00+00:00",
        "endTime": end or "2026-08-20T10:00:01+00:00",
        "attributes": attributes,
    }


@pytest.fixture
def state(tmp_path):
    return tmp_path / "traces"


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _make_artifact(state, name, content):
    path = state / "logs" / "audit-artifacts" / "tool.output" / "2026-08-20" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_bundle_roundtrip(state, workspace):
    artifact = _make_artifact(state, "output.json", '{"result": 42}')
    manager = SessionManager(workspace)
    session = manager.get_or_create("cli:chat1")
    session.add_message("user", "hello")
    manager.save(session)
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:chat1", start="2026-08-20T10:00:00+00:00"),
            _span(
                "trace-1",
                session_key="cli:chat1",
                name="tool.call",
                end="2026-08-20T10:00:05+00:00",
                attrs={"tool.output.artifact_path": str(artifact)},
            ),
        ],
    )
    tverdict.record_verdict("trace-1", "fail", source="user", why="wrong answer", state_dir=state)

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    assert bundle_dir == (state / "bundles" / "trace-1").resolve()
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format_version"] == tbundle.BUNDLE_FORMAT_VERSION
    assert manifest["attempt_id"] == "trace-1"
    assert manifest["session_key"] == "cli:chat1"
    assert manifest["time_range"] == {"start": "2026-08-20T10:00:00+00:00", "end": "2026-08-20T10:00:05+00:00"}
    assert manifest["span_count"] == 2
    assert manifest["artifact_count"] == 1
    assert manifest["verdict_count"] == 1
    assert manifest["session_included"] is True
    assert manifest["missing_artifacts"] == []
    assert manifest["raven_version"]

    spans = _read_lines(bundle_dir / "spans.jsonl")
    assert [s["name"] for s in spans] == ["session.turn", "tool.call"]
    verdicts = _read_lines(bundle_dir / "verdicts.jsonl")
    assert verdicts[0]["status"] == "fail" and verdicts[0]["why"] == "wrong answer"
    source_session = manager._get_session_path("cli:chat1")
    assert (bundle_dir / "session.jsonl").read_text(encoding="utf-8") == source_session.read_text(encoding="utf-8")


def test_artifact_paths_rewritten_offline(state, workspace, tmp_path):
    artifact = _make_artifact(state, "big-output.json", '{"tokens": [1, 2, 3]}')
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", name="tool.call", attrs={"tool.output.artifact_path": str(artifact)})],
    )

    bundle_dir = tbundle.collect_bundle("trace-1", out_dir=tmp_path / "out", state_dir=state, workspace=workspace)

    span = _read_lines(bundle_dir / "spans.jsonl")[0]
    rel = span["attributes"]["tool.output.artifact_path"]
    assert rel == "artifacts/big-output.json"
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["rewritten_artifact_paths"] == 1

    # The bundle must read on its own after the live store is gone.
    moved = tmp_path / "elsewhere"
    shutil.move(str(bundle_dir), str(moved))
    shutil.rmtree(state)
    assert (moved / rel).read_text(encoding="utf-8") == '{"tokens": [1, 2, 3]}'


def test_missing_artifact_not_fatal(state, workspace):
    gone = str(state / "logs" / "audit-artifacts" / "purged.json")
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", name="tool.call", attrs={"tool.output.artifact_path": gone})],
    )

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["missing_artifacts"] == [gone]
    assert manifest["artifact_count"] == 0
    span = _read_lines(bundle_dir / "spans.jsonl")[0]
    assert span["attributes"]["tool.output.artifact_path"] == gone


def test_artifact_basename_collision_disambiguated(state, workspace):
    a = _make_artifact(state, "output.json", "first")
    b = state / "other" / "output.json"
    b.parent.mkdir(parents=True)
    b.write_text("second", encoding="utf-8")
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", name="tool.call", attrs={"tool.output.artifact_path": str(a)}),
            _span("trace-1", name="llm.call", attrs={"llm.output.artifact_path": str(b)}),
        ],
    )

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    spans = _read_lines(bundle_dir / "spans.jsonl")
    rel_a = spans[0]["attributes"]["tool.output.artifact_path"]
    rel_b = spans[1]["attributes"]["llm.output.artifact_path"]
    assert rel_a != rel_b
    assert (bundle_dir / rel_a).read_text(encoding="utf-8") == "first"
    assert (bundle_dir / rel_b).read_text(encoding="utf-8") == "second"


def test_bundle_spans_rotation(state, workspace):
    archive = state / "logs" / "archive"
    _write_log(
        archive / "2026-08-18" / "audit-spans-2026-08-18-010101.log",
        [_span("trace-1", attempt_id="att-x", start="2026-08-18T09:00:00+00:00")],
    )
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-2", attempt_id="att-x", end="2026-08-20T11:00:00+00:00"),
            _span("trace-9"),
        ],
    )

    bundle_dir = tbundle.collect_bundle("att-x", state_dir=state, workspace=workspace)

    spans = _read_lines(bundle_dir / "spans.jsonl")
    assert [s["traceId"] for s in spans] == ["trace-1", "trace-2"]
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["time_range"] == {"start": "2026-08-18T09:00:00+00:00", "end": "2026-08-20T11:00:00+00:00"}


def test_save_auto_pins(state, workspace):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)
    registry = tstore.pins(state)
    assert registry["trace-1"]["reason"] == "bundled"


def test_missing_session_recorded(state, workspace):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:never-saved")])

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_included"] is False
    assert not (bundle_dir / "session.jsonl").exists()


def test_unknown_id_raises(state, workspace):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    with pytest.raises(LookupError):
        tbundle.collect_bundle("no-such-id", state_dir=state, workspace=workspace)
    with pytest.raises(ValueError):
        tbundle.collect_bundle("", state_dir=state, workspace=workspace)


def test_trace_id_resolves_to_full_attempt(state, workspace):
    """One turn's trace id must pack the whole multi-turn attempt it belongs to."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-x", session_key="cli:a"),
            _span("trace-2", attempt_id="att-x", session_key="cli:a", name="tool.call"),
        ],
    )
    tverdict.record_verdict("att-x", "fail", source="user", state_dir=state)

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    assert bundle_dir.name == "att-x"
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["attempt_id"] == "att-x"
    assert manifest["span_count"] == 2
    assert manifest["verdict_count"] == 1
    assert [s["traceId"] for s in _read_lines(bundle_dir / "spans.jsonl")] == ["trace-1", "trace-2"]
    registry = tstore.pins(state)
    assert "att-x" in registry and "trace-1" not in registry


def test_unsafe_id_rejected(state, workspace, tmp_path):
    """An attempt id (user-mintable) must not escape the output root as a path."""
    for evil in ("../escaped", str(tmp_path / "abs-escaped"), "a/b"):
        _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", attempt_id=evil)])
        with pytest.raises(ValueError):
            tbundle.collect_bundle(evil, state_dir=state, workspace=workspace)
    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "abs-escaped").exists()
    assert not (state / "escaped").exists()


def test_resave_drops_stale_artifacts(state, workspace):
    """A re-pack must not keep artifacts whose source was purged since last pack."""
    artifact = _make_artifact(state, "secret.json", '{"api_key": "sk-live"}')
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", name="tool.call", attrs={"tool.output.artifact_path": str(artifact)})],
    )
    first = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)
    assert (first / "artifacts" / "secret.json").exists()

    artifact.unlink()
    second = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    assert not (second / "artifacts" / "secret.json").exists()
    manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_count"] == 0
    assert manifest["missing_artifacts"] == [str(artifact)]
    leftovers = [p.name for p in second.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_default_workspace_reads_config(state, tmp_path, monkeypatch):
    """Without an explicit workspace, the configured agent workspace is used."""
    import types

    ws = tmp_path / "custom-ws"
    ws.mkdir()
    manager = SessionManager(ws)
    session = manager.get_or_create("cli:chat1")
    session.add_message("user", "hello")
    manager.save(session)
    monkeypatch.setattr(
        "raven.config.loader.load_config",
        lambda config_path=None: types.SimpleNamespace(workspace_path=ws),
    )
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:chat1")])

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state)

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_included"] is True
    assert (bundle_dir / "session.jsonl").exists()


def test_end_to_end_with_real_tracer(tmp_path, monkeypatch):
    """A trajectory recorded by the live tracer bundles into an offline dir."""
    from raven.tracing import spans as _spans
    from raven.tracing import trace

    state = tmp_path / "traces"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    _spans._store = None
    try:
        aid = trace.begin_attempt("cli:e2e")
        with trace.span("session.turn", session_key="cli:e2e"):
            with trace.span("tool.call") as s:
                s.artifact("tool.output", {"result": "ok", "items": list(range(50))})
        trace.end_attempt("cli:e2e")

        bundle_dir = tbundle.collect_bundle(aid, state_dir=state, workspace=workspace)

        spans = _read_lines(bundle_dir / "spans.jsonl")
        tool_span = next(s for s in spans if s["name"] == "tool.call")
        rel = tool_span["attributes"]["tool.output.artifact_path"]
        assert rel.startswith("artifacts/")
        payload = json.loads((bundle_dir / rel).read_text(encoding="utf-8"))
        assert payload["result"] == "ok" and len(payload["items"]) == 50
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["attempt_id"] == aid
        assert manifest["artifact_count"] == 1
        assert tstore.pins(state)[aid]["reason"] == "bundled"
    finally:
        _spans._store = None


def test_bundle_of_merged_attempt_collects_all_member_traces(state):
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:chat1", start="2026-08-20T10:00:00+00:00"),
            _span("trace-2", session_key="cli:chat1", start="2026-08-20T11:00:00+00:00"),
        ],
    )
    tverdict.record_verdict("trace-1", "fail", source="user", state_dir=state)
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    tverdict.record_verdict(aid, "pass", source="user", state_dir=state)

    bundle_dir = tbundle.collect_bundle("trace-2", state_dir=state)

    assert bundle_dir == (state / "bundles" / aid).resolve()
    spans = _read_lines(bundle_dir / "spans.jsonl")
    assert [s["traceId"] for s in spans] == ["trace-1", "trace-2"]
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["attempt_id"] == aid
    assert manifest["span_count"] == 2
    verdicts = _read_lines(bundle_dir / "verdicts.jsonl")
    assert {v["attempt_id"] for v in verdicts} == {"trace-1", aid}


def test_bundle_of_definition_with_purged_members_raises_lookup_error(state):
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1"), _span("trace-2")],
    )
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    (state / "logs" / "audit-spans.log").unlink()

    with pytest.raises(LookupError, match="no spans found for attempt"):
        tbundle.collect_bundle(aid, state_dir=state)


def test_bundle_pin_falls_back_to_member_traces_when_attempt_vanishes(state, monkeypatch):
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1"), _span("trace-2")],
    )
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    def vanished(id_, *, reason="", state_dir=None):
        raise LookupError(f"no spans found for id {id_!r}")

    monkeypatch.setattr(tbundle, "pin_attempt", vanished)
    tbundle.collect_bundle(aid, state_dir=state)

    registry = tstore.pins(state)
    assert registry["trace-1"]["reason"] == "bundled"
    assert registry["trace-2"]["reason"] == "bundled"
