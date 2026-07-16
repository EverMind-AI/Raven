"""Contract tests for the ``raven.tracing.trace`` facade (standard-api.v1).

Exercises the public ``trace.span`` API and asserts it emits well-formed
``audit.span.v1`` records: correct nesting, kinds, attributes, artifact refs,
error status, and no-op when disabled.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from raven.tracing import spans as _spans
from raven.tracing import trace


@pytest.fixture
def trace_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))
    _spans._store = None  # force the store to re-init against the temp dir
    yield tmp_path
    _spans._store = None


def _spans_written(trace_dir):
    log = trace_dir / "logs" / "audit-spans.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_nesting_kinds_and_attributes(trace_dir):
    with trace.span("session.turn", {"turn.input_preview": "hi"}) as root:
        root_id = root.span_id
        with trace.span("llm.call", {"llm.provider": "openrouter", "llm.model": "m"}) as s:
            s.set({"llm.usage.total_tokens": 42})

    spans = _spans_written(trace_dir)
    by = {sp["name"]: sp for sp in spans}
    assert len(spans) == 2
    assert len({sp["traceId"] for sp in spans}) == 1  # one trace
    assert by["session.turn"]["parentSpanId"] is None  # root
    assert by["llm.call"]["parentSpanId"] == root_id  # nests under root
    assert by["session.turn"]["attributes"]["span.type"] == "session"
    assert by["llm.call"]["attributes"]["span.type"] == "model"
    assert by["llm.call"]["attributes"]["llm.provider"] == "openrouter"
    assert by["llm.call"]["attributes"]["llm.usage.total_tokens"] == 42
    assert all(sp["schemaVersion"] == "audit.span.v1" for sp in spans)


def test_invocation_source_derives_from_enclosing_purpose(trace_dir):
    from raven.tracing import semconv

    # A model call nested under a purpose span self-labels with that purpose;
    # a model span never becomes its own source (model-under-model inherits).
    with trace.span("skill.gate", kind="skill"):
        with trace.span("llm.call") as s:
            assert s.invocation_source == "skill.gate"
            semconv.llm_call(s, {"self": None, "messages": [], "tools": None, "model": "openrouter/m"}, None, None)
    sp = next(x for x in _spans_written(trace_dir) if x["name"] == "llm.call")
    assert sp["attributes"]["llm.invocation_source"] == "skill.gate"


def test_invocation_source_is_none_at_root(trace_dir):
    with trace.span("session.turn") as root:
        assert root.invocation_source is None


def test_purpose_spans_record_input_and_output(trace_dir):
    from raven.tracing import semconv

    class _R:
        need_retrieval = True
        rewritten_query = "frontend design skills"

    with trace.span("skill.rewrite", kind="skill") as s:
        semconv.skill_rewrite(s, {"query": "help me install a frontend skill"}, _R(), None)
    sp = _spans_written(trace_dir)[0]
    a = sp["attributes"]
    assert a["skill.rewrite.need_retrieval"] is True
    # Wrapper node self-reports input/output as artifacts, like llm/tool/memory nodes.
    assert "skill.rewrite.input.artifact_path" in a
    assert "skill.rewrite.output.artifact_path" in a


def test_error_marks_status_and_reraises(trace_dir):
    with pytest.raises(ValueError):
        with trace.span("tool.call", {"tool.name": "read_file"}):
            raise ValueError("boom")

    spans = _spans_written(trace_dir)
    assert len(spans) == 1
    assert spans[0]["status"]["code"] == "ERROR"
    assert "boom" in spans[0]["status"]["message"]


def test_artifact_reference_attached(trace_dir):
    with trace.span("llm.call", {"llm.provider": "p", "llm.model": "m"}) as s:
        s.artifact("llm.input", {"messages": [{"role": "user", "content": "hi"}]})

    spans = _spans_written(trace_dir)
    assert "llm.input.artifact_path" in spans[0]["attributes"]


def test_custom_node_uses_explicit_kind(trace_dir):
    with trace.span("raven.sentinel.tick", {"sentinel.reason": "x"}, kind="plugin"):
        pass

    spans = _spans_written(trace_dir)
    assert spans[0]["name"] == "raven.sentinel.tick"
    assert spans[0]["attributes"]["span.type"] == "plugin"


def test_disabled_is_noop(trace_dir, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING", "0")
    with trace.span("should.noop") as n:
        n.set({"x": 1})
    assert _spans_written(trace_dir) == []


def test_tool_call_extractor(trace_dir):
    from raven.tracing import semconv

    with trace.span("tool.call") as s:
        semconv.tool_call(s, {"name": "list_dir", "params": {"path": "."}}, "a\nb", None)
    sp = _spans_written(trace_dir)[0]
    assert sp["name"] == "tool.call"
    assert sp["attributes"]["span.type"] == "tool"
    assert sp["attributes"]["tool.name"] == "list_dir"
    assert "tool.input.artifact_path" in sp["attributes"]
    assert "tool.output.artifact_path" in sp["attributes"]


def test_tool_call_retypes_to_skill(trace_dir):
    from raven.tracing import semconv

    with trace.span("tool.call") as s:
        semconv.tool_call(s, {"name": "use_skill", "params": {"skill_id": "local/weather"}}, "## weather\nbody", None)
    sp = _spans_written(trace_dir)[0]
    assert sp["name"] == "skill.read"  # every skill access retypes to the one skill.read kind
    assert sp["attributes"]["span.type"] == "skill"
    assert sp["attributes"]["skill.id"] == "local/weather"
    assert sp["attributes"]["skill.read.via_tool"] == "use_skill"  # origin preserved
    assert "skill.scripts_dir" not in sp["attributes"]  # body-only read => no bundle


def test_skill_read_reports_materialized_bundle(trace_dir):
    from raven.tracing import semconv

    body = "## weather\nscripts_dir: /ws/skills/local/weather/scripts\ncached: true\n\nrun it"
    with trace.span("tool.call") as s:
        semconv.tool_call(s, {"name": "use_skill", "params": {"skill_id": "local/weather"}}, body, None)
    sp = _spans_written(trace_dir)[0]
    assert sp["name"] == "skill.read"
    # The materialized bundle is the content-driven signal that this access
    # pulled runnable files, not just the instruction body.
    assert sp["attributes"]["skill.scripts_dir"] == "/ws/skills/local/weather/scripts"


def test_tool_error_result_marks_status(trace_dir):
    from raven.tracing import semconv

    with trace.span("tool.call") as s:
        semconv.tool_call(s, {"name": "read_file", "params": {"path": "x"}}, "Error: no such file", None)
    sp = _spans_written(trace_dir)[0]
    assert sp["status"]["code"] == "ERROR"


def test_memory_extract_extractor(trace_dir):
    from raven.tracing import semconv

    with trace.span("memory.extract") as s:
        semconv.memory_extract(
            s, {"messages": [{"role": "user", "content": "x"}], "model": "m", "enable_foresight": True}, True, None
        )
    a = _spans_written(trace_dir)[0]["attributes"]
    assert a["span.type"] == "memory"
    assert a["memory.message_count"] == 1
    assert a["memory.annotated"] is True


def test_memory_consolidate_extractor(trace_dir):
    from raven.tracing import semconv

    class _S:
        key = "cli:abc"
        last_consolidated = 3
        messages = [1, 2, 3]

    with trace.span("memory.consolidate") as s:
        semconv.memory_consolidate(s, {"session": _S()}, None, None)
    a = _spans_written(trace_dir)[0]["attributes"]
    assert a["memory.session_key"] == "cli:abc"
    assert a["memory.message_count"] == 3


def test_subagent_children_nest(trace_dir):
    from raven.tracing import semconv

    # A subagent span; its inner primitives nest under it via context propagation.
    with trace.span("subagent.run") as sa:
        semconv.subagent(
            sa, {"task_id": "t1", "task": "do x", "label": "worker", "origin": {"session_key": "cli:p"}}, None, None
        )
        with trace.span("llm.call", {"llm.provider": "p", "llm.model": "m"}) as inner:
            inner_parent = inner._parent
        sa_id = sa.span_id
    spans = _spans_written(trace_dir)
    by = {sp["name"]: sp for sp in spans}
    assert by["subagent.run"]["attributes"]["span.type"] == "subagent"
    assert by["subagent.run"]["attributes"]["subagent.label"] == "worker"
    assert inner_parent == sa_id  # inner llm.call nests under the subagent node


# ---------------------------------------------------------------------------
# Contract gates (standard-api.v1). These freeze the adopter/viewer contract
# and the "tracing can never break the host" invariant. A change that trips
# them is a deliberate contract change: update the snapshot + bump the schema.
# ---------------------------------------------------------------------------


def test_audit_span_v1_record_shape_is_frozen(trace_dir):
    with trace.span("llm.call", {"llm.provider": "p", "llm.model": "m"}):
        pass
    sp = _spans_written(trace_dir)[0]
    assert set(sp.keys()) == {
        "schemaVersion",
        "traceId",
        "spanId",
        "parentSpanId",
        "name",
        "kind",
        "startTime",
        "endTime",
        "status",
        "events",
        "attributes",
    }
    assert sp["schemaVersion"] == "audit.span.v1"
    assert set(sp["status"].keys()) == {"code", "message"}
    for key in ("span.type", "framework", "session.id", "channel.id", "audit.schema_version"):
        assert key in sp["attributes"]


def test_span_kind_vocabulary_is_frozen():
    from raven.tracing import trace as _t

    assert set(_t._KIND_BY_DOMAIN.values()) == {
        "session",
        "model",
        "tool",
        "subagent",
        "skill",
        "memory",
        "plugin",
    }


def test_standard_span_required_attributes(trace_dir):
    from raven.tracing import semconv

    class _Resp:
        content = "hi"
        tool_calls: list = []
        usage = None
        finish_reason = "stop"
        reasoning_content = None

    with trace.span("llm.call") as s:
        semconv.llm_call(s, {"self": None, "messages": [], "tools": None, "model": "openrouter/x"}, _Resp(), None)
    with trace.span("tool.call") as s:
        semconv.tool_call(s, {"name": "grep", "params": {}}, "ok", None)
    by = {sp["name"]: sp for sp in _spans_written(trace_dir)}
    assert by["llm.call"]["attributes"]["llm.provider"]
    assert by["llm.call"]["attributes"]["llm.model"]
    assert by["tool.call"]["attributes"]["tool.name"] == "grep"


def test_tracing_disabled_is_passthrough(monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING", "0")
    calls = {"n": 0}

    @trace.instrument("llm.call")
    async def f(x):
        calls["n"] += 1
        return x * 2

    assert asyncio.run(f(21)) == 42
    assert calls["n"] == 1
    assert trace.current() is None


def test_tracing_internal_failure_never_breaks_host(trace_dir, monkeypatch):
    from raven.tracing import spans as _spans

    def _boom(*_a, **_k):
        raise RuntimeError("tracing store down")

    monkeypatch.setattr(_spans, "emit", _boom)

    @trace.instrument("llm.call")
    async def ok(x):
        return x + 1

    @trace.instrument("tool.call")
    async def app_error():
        raise ValueError("APP")

    # tracing's own crash must not surface to the host
    assert asyncio.run(ok(41)) == 42
    # the host's own exception must propagate unchanged
    with pytest.raises(ValueError, match="APP"):
        asyncio.run(app_error())
