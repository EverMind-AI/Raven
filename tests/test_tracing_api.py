"""Contract tests for the ``raven.tracing.trace`` facade (standard-api.v1).

Exercises the public ``trace.span`` API and asserts it emits well-formed
``audit.span.v1`` records: correct nesting, kinds, attributes, artifact refs,
error status, and no-op when disabled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from raven.tracing import spans as _spans
from raven.tracing import store as _store_mod
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


def test_a_root_span_starts_its_own_trace_and_links_back(trace_dir):
    """The shape a turn needs, and the one the viewer's grouping assumes.

    A turn opened while another span is still active -- a dispatch reporting
    back, a follow-up driven by a tool's own completion -- must not become that
    span's child. Observed before this existed: a whole second turn sat at depth
    2 under the first turn's tool call, so one trace held two turns and the
    reader could not tell where one ended.
    """
    with trace.span("session.turn") as first:
        with trace.span("tool.call", {"tool.name": "load_playbook"}) as dispatch:
            with trace.span("session.turn", root=True) as second:
                pass

    written = {sp["spanId"]: sp for sp in _spans_written(trace_dir)}
    outer, inner = written[first.span_id], written[second.span_id]

    assert inner["parentSpanId"] is None, "a root span must not inherit a parent"
    assert inner["traceId"] != outer["traceId"], "a root span must begin its own trace"
    # Where it came from is kept, as a link rather than as parentage.
    attrs = inner["attributes"]
    assert attrs["trace.dispatched_by_span_id"] == dispatch.span_id
    assert attrs["trace.dispatched_in_trace_id"] == outer["traceId"]


def test_a_root_span_at_the_top_carries_no_dispatch_link(trace_dir):
    with trace.span("session.turn", root=True) as only:
        pass

    written = {sp["spanId"]: sp for sp in _spans_written(trace_dir)}[only.span_id]
    assert written["parentSpanId"] is None
    assert "trace.dispatched_by_span_id" not in written["attributes"]


def test_invocation_source_derives_from_enclosing_purpose(trace_dir):
    from raven.observability import semconv

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
    from raven.observability import semconv

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


def test_personalize_extractor_records_step_io(trace_dir):
    from raven.observability import semconv

    with trace.span("personalize.classify", kind="memory") as s:
        semconv.personalize(
            s, {"self": object(), "message": "hi", "history": None}, {"needs_clarification": False}, None
        )
    a = _spans_written(trace_dir)[0]["attributes"]
    assert a["span.type"] == "memory"
    assert a["personalize.step"] == "classify"
    assert a["personalize.ok"] is True
    assert "personalize.input.artifact_path" in a
    assert "personalize.output.artifact_path" in a


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
    from raven.observability import semconv

    with trace.span("tool.call") as s:
        semconv.tool_call(s, {"name": "list_dir", "params": {"path": "."}}, "a\nb", None)
    sp = _spans_written(trace_dir)[0]
    assert sp["name"] == "tool.call"
    assert sp["attributes"]["span.type"] == "tool"
    assert sp["attributes"]["tool.name"] == "list_dir"
    assert "tool.input.artifact_path" in sp["attributes"]
    assert "tool.output.artifact_path" in sp["attributes"]


def test_tool_call_retypes_to_skill(trace_dir):
    from raven.observability import semconv

    with trace.span("tool.call") as s:
        semconv.tool_call(s, {"name": "use_skill", "params": {"skill_id": "local/weather"}}, "## weather\nbody", None)
    sp = _spans_written(trace_dir)[0]
    assert sp["name"] == "skill.read"  # every skill access retypes to the one skill.read kind
    assert sp["attributes"]["span.type"] == "skill"
    assert sp["attributes"]["skill.id"] == "local/weather"
    assert sp["attributes"]["skill.read.via_tool"] == "use_skill"  # origin preserved
    assert "skill.scripts_dir" not in sp["attributes"]  # body-only read => no bundle


def test_skill_read_reports_materialized_bundle(trace_dir):
    from raven.observability import semconv

    body = "## weather\nscripts_dir: /ws/skills/local/weather/scripts\ncached: true\n\nrun it"
    with trace.span("tool.call") as s:
        semconv.tool_call(s, {"name": "use_skill", "params": {"skill_id": "local/weather"}}, body, None)
    sp = _spans_written(trace_dir)[0]
    assert sp["name"] == "skill.read"
    # The materialized bundle is the content-driven signal that this access
    # pulled runnable files, not just the instruction body.
    assert sp["attributes"]["skill.scripts_dir"] == "/ws/skills/local/weather/scripts"


def test_tool_error_result_marks_status(trace_dir):
    from raven.observability import semconv

    with trace.span("tool.call") as s:
        semconv.tool_call(s, {"name": "read_file", "params": {"path": "x"}}, "Error: no such file", None)
    sp = _spans_written(trace_dir)[0]
    assert sp["status"]["code"] == "ERROR"


def test_memory_extract_extractor(trace_dir):
    from raven.observability import semconv

    with trace.span("memory.extract") as s:
        semconv.memory_extract(
            s, {"messages": [{"role": "user", "content": "x"}], "model": "m", "enable_foresight": True}, True, None
        )
    a = _spans_written(trace_dir)[0]["attributes"]
    assert a["span.type"] == "memory"
    assert a["memory.message_count"] == 1
    assert a["memory.annotated"] is True


def test_memory_consolidate_extractor(trace_dir):
    from raven.observability import semconv

    class _S:
        key = "cli:abc"
        last_consolidated = 3
        messages = [1, 2, 3]

    with trace.span("memory.consolidate") as s:
        semconv.memory_consolidate(s, {"session": _S()}, None, None)
    a = _spans_written(trace_dir)[0]["attributes"]
    assert a["memory.session_key"] == "cli:abc"
    assert a["memory.message_count"] == 3


def test_skill_gate_extractor_records_the_subagent_roster(trace_dir):
    """The roster is a gate input like the tool list: a trace that omits it
    cannot say whether a candidate was dropped for overlapping a sub-agent."""
    from raven.observability import semconv

    with trace.span("skill.gate") as s:
        semconv.skill_gate(
            s,
            {
                "task": "t",
                "candidates": [],
                "available_tools": ["exec"],
                "available_subagents": "Scribe [stateless] (writes decks)",
            },
            [],
            None,
        )
    a = _spans_written(trace_dir)[0]["attributes"]
    with open(a["skill.gate.input.artifact_path"], encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["available_subagents"] == "Scribe [stateless] (writes decks)"


def test_subagent_children_nest(trace_dir):
    from raven.observability import semconv

    # A subagent span; its inner primitives nest under it via context propagation.
    with trace.span("subagent.run") as sa:
        semconv.subagent(
            sa,
            {"task_id": "t1", "task": "do x", "task_summary": "worker", "origin": {"session_key": "cli:p"}},
            None,
            None,
        )
        with trace.span("llm.call", {"llm.provider": "p", "llm.model": "m"}) as inner:
            inner_parent = inner._parent
        sa_id = sa.span_id
    spans = _spans_written(trace_dir)
    by = {sp["name"]: sp for sp in spans}
    assert by["subagent.run"]["attributes"]["span.type"] == "subagent"
    assert by["subagent.run"]["attributes"]["subagent.label"] == "worker"
    assert inner_parent == sa_id  # inner llm.call nests under the subagent node


def test_a_mutated_artifact_does_not_corrupt_the_next_record(trace_dir):
    """An edit through one hard link must not follow the payload forward.

    Every published artifact path is a writable link to the shared blob, so
    without a check before linking, one mutated file makes every later record
    of the same payload report a sha1 its own bytes do not have.
    """
    store = _spans._get_store()
    payload = {"m": "good"}

    first = store.persist_artifact("llm.input", {"traceId": "t1"}, payload)
    Path(first["path"]).write_text("CORRUPT", encoding="utf-8")

    second = store.persist_artifact("tool.output", {"traceId": "t2"}, payload)
    text = Path(second["path"]).read_text(encoding="utf-8")

    assert json.loads(text) == payload
    assert hashlib.sha1(text.encode("utf-8")).hexdigest() == second["sha1"]
    assert second["sha1"] == first["sha1"]


def test_repairing_a_blob_leaves_the_edited_artifact_as_it_is(trace_dir):
    """The damaged record keeps its bytes; only later references are repaired.

    Rewriting an artifact somebody edited would be the audit trail lying a
    second time. What must not survive is the damage spreading.
    """
    store = _spans._get_store()
    payload = {"m": "good"}

    first = store.persist_artifact("llm.input", {"traceId": "t1"}, payload)
    mutated = Path(first["path"])
    mutated.write_text("CORRUPT", encoding="utf-8")

    second = Path(store.persist_artifact("tool.output", {"traceId": "t2"}, payload)["path"])
    third = Path(store.persist_artifact("memory.recall", {"traceId": "t3"}, payload)["path"])

    assert mutated.read_text(encoding="utf-8") == "CORRUPT"
    assert mutated.stat().st_ino != second.stat().st_ino
    assert second.stat().st_ino == third.stat().st_ino  # dedup resumes after the repair


def test_an_intact_blob_is_not_rehashed_for_every_span(trace_dir, monkeypatch):
    """The check costs one read per distinct payload per process, not per span.

    Without the cached (inode, mtime, size) the write path would read the
    whole blob on every reference, which is the cost the layout exists to
    avoid.
    """
    store = _spans._get_store()
    blob_reads: list[Path] = []
    real_open = Path.open

    def counting_open(self, mode="r", *args, **kwargs):
        if _store_mod.BLOBS_DIR_NAME in self.parts and "b" in mode:
            blob_reads.append(self)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    for i in range(4):
        store.persist_artifact("llm.input", {"traceId": f"t{i}"}, {"m": "same"})

    assert blob_reads == []


def test_identical_payloads_share_one_inode(trace_dir):
    store = _spans._get_store()
    payload = {"messages": ["a" * 5000]}

    first = store.persist_artifact("llm.input", {"traceId": "t1"}, payload)
    second = store.persist_artifact("llm.input", {"traceId": "t2"}, payload)

    p1, p2 = Path(first["path"]), Path(second["path"])
    assert p1 != p2
    assert p1.stat().st_ino == p2.stat().st_ino
    assert json.loads(p1.read_text(encoding="utf-8")) == payload
    assert json.loads(p2.read_text(encoding="utf-8")) == payload
    assert first["sha1"] == second["sha1"]
    assert first["bytes"] == second["bytes"]


def test_one_blob_backs_every_reference(trace_dir):
    store = _spans._get_store()
    payload = {"messages": ["b" * 100]}

    store.persist_artifact("llm.input", {"traceId": "t1"}, payload)
    art = store.persist_artifact("tool.input", {"traceId": "t2"}, payload)

    blobs = sorted(store.blobs_dir.rglob("*.json"))
    assert len(blobs) == 1
    assert blobs[0].name == f"{art['sha1']}.json"
    assert blobs[0].parent.name == art["sha1"][:2]
    # blob + the two span paths
    assert Path(art["path"]).stat().st_nlink == 3


def test_distinct_payloads_do_not_share_a_blob(trace_dir):
    store = _spans._get_store()

    a = store.persist_artifact("llm.input", {"traceId": "t1"}, {"m": "one"})
    b = store.persist_artifact("llm.input", {"traceId": "t2"}, {"m": "two"})

    assert a["sha1"] != b["sha1"]
    assert Path(a["path"]).stat().st_ino != Path(b["path"]).stat().st_ino
    assert len(sorted(store.blobs_dir.rglob("*.json"))) == 2


def test_write_path_falls_back_when_hard_links_are_unavailable(trace_dir, monkeypatch):
    store = _spans._get_store()

    def _no_links(*_args, **_kwargs):
        raise OSError("hard links unsupported")

    monkeypatch.setattr(_store_mod.os, "link", _no_links)
    payload = {"m": "fallback"}

    art = store.persist_artifact("tool.input", {"traceId": "t1"}, payload)

    path = Path(art["path"])
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert path.stat().st_nlink == 1
    assert art.get("error") is None
    assert not list(store.blobs_dir.rglob("*.tmp"))


def test_failed_temp_write_leaves_no_orphan_tmp_file(trace_dir, monkeypatch):
    store = _spans._get_store()
    real_write_text = Path.write_text
    calls = {"n": 0}

    def _fail_first_call(self, data, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a write that reaches disk before it fails (e.g. ENOSPC
            # mid-write), so a leftover temp file would prove the bug.
            real_write_text(self, data, *args, **kwargs)
            raise OSError("simulated disk full")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _fail_first_call)
    payload = {"m": "diskfull"}

    art = store.persist_artifact("tool.input", {"traceId": "t1"}, payload)

    path = Path(art["path"])
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert path.stat().st_nlink == 1
    assert art.get("error") is None
    assert not list(store.blobs_dir.rglob("*.tmp"))


def test_string_payloads_keep_the_txt_extension(trace_dir):
    store = _spans._get_store()

    art = store.persist_artifact("subagent.external.transcript", {"traceId": "t1"}, "raw text")

    path = Path(art["path"])
    assert path.suffix == ".txt"
    assert path.read_text(encoding="utf-8") == "raw text"
    assert (store.blobs_dir / art["sha1"][:2] / f"{art['sha1']}.txt").exists()


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
    from raven.observability import semconv

    class _Resp:
        content = "hi"
        tool_calls: list = []
        usage = None
        finish_reason = "stop"
        reasoning_content = None
        thinking_blocks = [{"type": "thinking", "thinking": "structured"}]

    with trace.span("llm.call") as s:
        semconv.llm_call(s, {"self": None, "messages": [], "tools": None, "model": "openrouter/x"}, _Resp(), None)
    with trace.span("tool.call") as s:
        semconv.tool_call(s, {"name": "grep", "params": {}}, "ok", None)
    by = {sp["name"]: sp for sp in _spans_written(trace_dir)}
    assert by["llm.call"]["attributes"]["llm.provider"]
    assert by["llm.call"]["attributes"]["llm.model"]
    assert by["tool.call"]["attributes"]["tool.name"] == "grep"
    output_path = Path(by["llm.call"]["attributes"]["llm.output.artifact_path"])
    assert json.loads(output_path.read_text(encoding="utf-8"))["thinking_blocks"] == _Resp.thinking_blocks


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


def test_provider_label_reports_the_normalized_route_prefix() -> None:
    """Telemetry names the backend that served the call, in one spelling.

    Every gateway is reached through the same provider class, so the class name
    hides the backend; LiteLLM encodes it as the route prefix. Grouping metrics
    needs one spelling per backend, which is why this goes through the registry's
    splitter rather than slicing the id here.
    """
    from raven.observability.semconv import _provider_label

    assert _provider_label("openrouter/anthropic/claude-sonnet-4-5", "LiteLLMProvider") == "openrouter"
    assert _provider_label("nano-gpt/gpt-4o", "LiteLLMProvider") == "nano_gpt"
    assert _provider_label("NANO-GPT/gpt-4o", "LiteLLMProvider") == "nano_gpt"
    # A bare id names no backend, so the class is all there is to report.
    assert _provider_label("claude-opus-4-5", "AnthropicProvider") == "AnthropicProvider"
    assert _provider_label(None, "AnthropicProvider") == "AnthropicProvider"


# ---------------------------------------------------------------------------
# Which front end produced a trace, where the channel cannot say.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _undeclared_surface():
    """A declaration is process-wide, so leaving one set would leak into the
    next test in the file."""
    previous = _spans._surface
    _spans.set_surface(None)
    yield
    _spans._surface = previous


def test_a_host_that_declares_nothing_reports_the_channel(trace_dir):
    """Every gateway channel already names its front end -- `qq`, `web`, `cli` --
    so falling back keeps the attribute populated instead of leaving a column
    empty for everything but two hosts."""
    with trace.span("session.turn", channel="qq"):
        pass

    attrs = _spans_written(trace_dir)[0]["attributes"]
    assert attrs["channel.id"] == "qq", "the channel has to reach the span, or this proves nothing"
    assert attrs["surface"] == "qq"


def test_the_served_page_is_distinguishable_from_the_terminal(trace_dir):
    """The point of the whole dimension. Both run on the `tui` channel by design
    -- one session pool, so the same person sees the same conversations from
    either -- which leaves `channel.id` reading the same for both."""
    from raven.cli.serve_commands import SERVED_PAGE_SURFACE
    from raven.cli.tui_commands import TERMINAL_SURFACE

    _spans.set_surface(TERMINAL_SURFACE)
    with trace.span("session.turn", channel="tui"):
        pass
    _spans.set_surface(SERVED_PAGE_SURFACE)
    with trace.span("session.turn", channel="tui"):
        pass

    terminal, page = _spans_written(trace_dir)
    assert terminal["attributes"]["channel.id"] == page["attributes"]["channel.id"] == "tui"
    assert terminal["attributes"]["surface"] == "tui"
    assert page["attributes"]["surface"] == "page"


def test_the_page_does_not_call_itself_web(trace_dir):
    """`web` was the retired web channel, a different front end on its own channel. Two
    different things under one label is worse than no label."""
    from raven.cli.serve_commands import SERVED_PAGE_SURFACE

    assert SERVED_PAGE_SURFACE != "web"


def test_a_declaration_reaches_every_span_in_the_turn(trace_dir):
    """Declared once at startup rather than threaded through each turn, so a
    child span inherits it without any call site knowing about it."""
    _spans.set_surface("page")
    with trace.span("session.turn", channel="tui"):
        with trace.span("llm.call", {"llm.provider": "openrouter", "llm.model": "m"}):
            pass

    assert {sp["attributes"]["surface"] for sp in _spans_written(trace_dir)} == {"page"}


def test_each_host_declares_before_it_can_emit_a_span():
    """A declaration that lands after the first span would leave the opening of
    every session mislabelled. Asserted against the source because booting either
    host in a unit test costs a real engine, and the ordering is what matters.
    """
    import inspect

    from raven.cli import serve_commands, tui_commands

    serve_src = inspect.getsource(serve_commands._serve_main)
    assert "set_surface(SERVED_PAGE_SURFACE)" in serve_src
    assert serve_src.index("set_surface") < serve_src.index("await build_rpc_stack(")

    tui_src = inspect.getsource(tui_commands._run_rpc_server_until_done)
    assert "set_surface(TERMINAL_SURFACE)" in tui_src
    assert tui_src.index("set_surface") < tui_src.index("register_aligned_methods_except_system")


# ---------------------------------------------------------------------------
# Per-turn surface: one gateway process now serves several front ends at once.
# ---------------------------------------------------------------------------


def test_a_turn_that_declares_its_own_surface_wins_over_the_process_global(trace_dir):
    """A gateway hosting the page serves the page, the shell, and relayed
    terminals from one process, so the process-wide declaration cannot name
    them all. A turn that arrived on a connection which declared itself carries
    that name on every span, whatever the process says."""
    _spans.set_surface("page")
    with trace.span("session.turn", channel="tui", surface="shell"):
        with trace.span("llm.call", {"llm.provider": "openrouter", "llm.model": "m"}):
            pass

    assert {sp["attributes"]["surface"] for sp in _spans_written(trace_dir)} == {"shell"}


def test_a_turn_without_its_own_surface_falls_back_to_the_process_global(trace_dir):
    """`raven serve` standalone and `raven agent` declare nothing per turn;
    their spans must look exactly as they did before the field existed."""
    _spans.set_surface("page")
    with trace.span("session.turn", channel="tui", surface=None):
        pass

    assert _spans_written(trace_dir)[0]["attributes"]["surface"] == "page"


def test_with_no_declaration_anywhere_the_channel_still_names_the_surface(trace_dir):
    with trace.span("session.turn", channel="tui", surface=None):
        pass

    assert _spans_written(trace_dir)[0]["attributes"]["surface"] == "tui"


def test_the_request_source_carries_the_surface_into_the_turn_seed():
    """The turn runs on the spine's own task, out of reach of the connection's
    contextvars, so the surface has to ride the TurnRequest into the root
    span's seed -- children then inherit it like the rest of the identity."""
    from raven.observability.semconv import turn_seed
    from raven.spine import ChatType, Origin, Source, TurnRequest

    declared = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM, surface="shell"),
        text="hi",
    )
    seed = turn_seed({"self": None, "req": declared, "session_key": "tui:c"})
    assert seed["surface"] == "shell"
    assert seed["channel"] == "tui", "the shared channel must stay exactly what it was"

    undeclared = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="hi",
    )
    assert turn_seed({"self": None, "req": undeclared, "session_key": "tui:c"})["surface"] is None


def test_attempt_id_defaults_to_trace_id(trace_dir):
    """Without an open attempt, every turn is its own single-turn attempt."""
    with trace.span("session.turn", session_key="cli:a"):
        with trace.span("llm.call"):
            pass

    spans = _spans_written(trace_dir)
    trace_ids = {sp["traceId"] for sp in spans}
    assert len(trace_ids) == 1
    for sp in spans:
        assert sp["attributes"]["attempt.id"] == sp["traceId"]


def test_explicit_attempt_groups_turns_under_one_id(trace_dir):
    """begin_attempt groups several turns' spans; end_attempt restores per-turn ids."""
    aid = trace.begin_attempt("cli:a")
    try:
        with trace.span("session.turn", session_key="cli:a"):
            with trace.span("tool.call"):
                pass
        with trace.span("session.turn", session_key="cli:a"):
            pass
        # An unrelated session is not captured by cli:a's attempt.
        with trace.span("session.turn", session_key="cli:b") as other:
            assert other.attempt_id == other.trace_id
    finally:
        assert trace.end_attempt("cli:a") == aid

    with trace.span("session.turn", session_key="cli:a") as after:
        assert after.attempt_id == after.trace_id

    spans = _spans_written(trace_dir)
    grouped = [sp for sp in spans if sp["attributes"]["attempt.id"] == aid]
    assert len(grouped) == 3  # two turns + one tool call
    assert len({sp["traceId"] for sp in grouped}) == 2  # across two traces
    assert aid.startswith("att-")


def test_a_nested_root_does_not_inherit_the_dispatchers_attempt(trace_dir):
    """A root begins its own attempt, not the attempt it was dispatched from.

    The trace id is fresh but the attempt id was still read off the enclosing
    context, so the second turn filed itself under the first turn's attempt --
    exactly the grouping the trajectory tools use, so two unrelated turns read
    as one trajectory.
    """
    with trace.span("session.turn", session_key="cli:a", root=True) as first:
        with trace.span("tool.call"):
            with trace.span("session.turn", session_key="cli:b", root=True) as second:
                pass

    assert first.attempt_id == first.trace_id
    assert second.attempt_id == second.trace_id, "a root must not carry the dispatcher's attempt"
    assert second.attempt_id != first.attempt_id


def test_a_nested_root_takes_its_own_sessions_open_attempt(trace_dir):
    """An explicit attempt on the root's own session still wins over the ambient one."""
    outer = trace.begin_attempt("cli:a")
    inner = trace.begin_attempt("cli:b")
    try:
        with trace.span("session.turn", session_key="cli:a", root=True):
            with trace.span("tool.call"):
                with trace.span("session.turn", session_key="cli:b", root=True) as second:
                    assert second.attempt_id == inner
    finally:
        trace.end_attempt("cli:b")
        trace.end_attempt("cli:a")
    assert outer != inner


def test_a_child_keeps_the_attempt_its_tree_began_with(trace_dir):
    """Inheritance is what holds a tree together once the attempt itself moves on.

    Ending the attempt (or opening the next one) while a turn is still running
    must not re-file the spans still to come: they belong to the attempt the turn
    started under. Resolving per span from the session would split one turn.
    """
    first = trace.begin_attempt("cli:a")
    with trace.span("session.turn", session_key="cli:a", root=True) as turn:
        assert turn.attempt_id == first
        second = trace.begin_attempt("cli:a")
        with trace.span("tool.call") as mid:
            with trace.span("llm.call") as leaf:
                pass
    trace.end_attempt("cli:a")

    assert second != first
    assert mid.attempt_id == first, "a child must keep its tree's attempt"
    assert leaf.attempt_id == first
    written = {sp["spanId"]: sp["attributes"]["attempt.id"] for sp in _spans_written(trace_dir)}
    assert set(written.values()) == {first}


def test_attempt_id_survives_detached_and_checkpoint(trace_dir):
    aid = trace.begin_attempt("cli:c")
    try:
        with trace.span("session.turn", session_key="cli:c") as root:
            root.checkpoint()
            with trace.span("skill.inject", detached=True):
                pass
    finally:
        trace.end_attempt("cli:c")
    for sp in _spans_written(trace_dir):
        assert sp["attributes"]["attempt.id"] == aid


def test_suppress_silences_only_its_own_block(trace_dir):
    with trace.span("session.turn", session_key="cli:s"):
        pass
    with trace.suppress():
        assert not trace.enabled()
        with trace.span("session.turn", session_key="cli:s"):
            with trace.span("llm.call"):
                pass
    assert trace.enabled()
    with trace.span("tool.call"):
        pass

    names = [sp["name"] for sp in _spans_written(trace_dir)]
    assert names == ["session.turn", "tool.call"], "suppressed spans must not be emitted"


def test_suppress_is_task_local(trace_dir):
    """A concurrent task keeps tracing while another task suppresses —
    the contract that lets a replay run beside real turns in one process."""

    started = asyncio.Event()
    release = asyncio.Event()

    async def suppressed_task():
        with trace.suppress():
            started.set()
            await release.wait()
            with trace.span("llm.call"):
                pass

    async def live_task():
        await started.wait()
        with trace.span("session.turn", session_key="cli:live"):
            pass
        release.set()

    async def main():
        await asyncio.gather(suppressed_task(), live_task())

    asyncio.run(main())

    names = [sp["name"] for sp in _spans_written(trace_dir)]
    assert names == ["session.turn"], "only the live task's span may be emitted"
