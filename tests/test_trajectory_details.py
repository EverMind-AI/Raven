"""Tests for the trajectory detail layer (`raven.trajectory.details`)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from raven.tracing import artifact_v2
from raven.trajectory import conversation as tconv
from raven.trajectory import details as tdet
from raven.trajectory import index as tidx

SESSION = "tui:main"


def _ts(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"2026-09-01T10:{minutes:02d}:{secs:02d}+00:00"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    (path / "logs").mkdir(parents=True)
    return path


def _append(state: Path, spans: list[dict]) -> None:
    target = state / "logs" / "audit-spans.log"
    with target.open("ab") as fh:
        for span in spans:
            fh.write(json.dumps(span).encode() + b"\n")


def _artifact(state: Path, payload, name: str) -> str:
    directory = state / "logs" / "audit-artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _blob(state: Path, message: dict) -> dict:
    sha1 = hashlib.sha1(json.dumps(message, ensure_ascii=False, default=str).encode()).hexdigest()
    path = artifact_v2.message_path(state / "logs" / "audit-artifacts", sha1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(message), encoding="utf-8")
    return {"$msg": sha1}


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


def _turn(state, trace, span_id, *, start, end, content_in="hello", content_out="done", extra=None, files=None):
    prefix = files or span_id
    attrs = {
        "turn.input_preview": content_in[:20],
        "turn.input.artifact_path": _artifact(
            state,
            {
                "content": content_in,
                "channel": "tui",
                "chat_id": "c1",
                "media": [{"name": "a.png", "type": "image/png"}],
            },
            f"{prefix}-in",
        ),
    }
    if content_out is not None:
        attrs["turn.output_preview"] = content_out[:20]
        attrs["turn.output.artifact_path"] = _artifact(state, {"content": content_out}, f"{prefix}-out")
    attrs.update(extra or {})
    return _span(trace, span_id, "session.turn", start=start, end=end, attrs=attrs)


def _tool(state, trace, span_id, parent, *, start, end, name="search", params=None, result="ok", extra=None):
    attrs = {
        "tool.name": name,
        "tool.args_preview": "{}",
        "tool.result_preview": str(result)[:20],
        "tool.input.artifact_path": _artifact(state, {"name": name, "params": params or {"q": "x"}}, f"{span_id}-in"),
        "tool.output.artifact_path": _artifact(state, {"result": result}, f"{span_id}-out"),
    }
    attrs.update(extra or {})
    return _span(trace, span_id, "tool.call", parent=parent, start=start, end=end, attrs=attrs)


def _llm(
    state, trace, span_id, parent, *, start, end, tools=None, tool_names=None, output=None, v2_count=0, extra=None
):
    if v2_count:
        refs = [_blob(state, {"role": "user", "content": f"message {i}"}) for i in range(v2_count)]
        shell = {
            "artifactFormat": artifact_v2.ARTIFACT_FORMAT,
            "provider": "p",
            "providerClass": "P",
            "model": "m",
            "systemPrompt": refs[0],
            "prompt": refs[-1],
            "messages": refs,
            "tools": tools if tools is not None else [],
            "request": {"bytes": 10, "images": 0, "imageBytes": 0},
            "generation": {"temperature": 0.1},
        }
    else:
        shell = {
            "messages": [{"role": "user", "content": "q"}],
            "tools": tools if tools is not None else [],
            "model": "m",
        }
    attrs = {"llm.model": "m", "llm.provider": "p", "llm.input.artifact_path": _artifact(state, shell, f"{span_id}-in")}
    if tool_names is not None:
        attrs["llm.tool_names"] = tool_names
    if output is not None:
        attrs["llm.output.artifact_path"] = _artifact(state, output, f"{span_id}-out")
        if isinstance(output.get("content"), str):
            attrs["llm.output_preview"] = output["content"][:20]
    attrs.update(extra or {})
    return _span(trace, span_id, "llm.call", parent=parent, start=start, end=end, attrs=attrs)


def _ready(state: Path, **limits) -> tidx.SessionIndex:
    index = tidx.SessionIndex(SESSION, state, limits=tidx.Limits(**limits))
    for _ in range(50):
        index.refresh_sync(None)
        if index.index_state().phase == tidx.PHASE_READY:
            return index
    raise AssertionError("index not ready")


def _entry(index: tidx.SessionIndex, span_id: str, slot: str):
    matches = [e for e in index.entries() if e.span_id == span_id and e.slot == slot]
    assert len(matches) == 1, [(e.span_id, e.slot) for e in index.entries()]
    return matches[0]


def _describe(index, state, entry):
    descriptor = tdet.describe(index, entry.entry_id, state=state)
    assert descriptor is not None
    return descriptor


def _block(index, state, entry, block_id, cursor=None):
    return tdet.read_block(
        index, entry.entry_id, block_id, state=state, entry_revision=entry.revision, epoch=index.epoch, cursor=cursor
    )


def _ids(descriptor):
    return [b.id for b in descriptor.blocks]


TAIL = ["timing", "relations", "raw"]


# ── registry and coverage ─────────────────────────────────────────────


def test_block_specs_are_unique_and_renderers_known():
    kinds = list(tdet._REGISTRY) + [
        "subagent.external.summary",
        "foo.bar.summary",
        "span.error",
        "memory.enqueue.summary",
    ]
    for kind in kinds:
        specs = tdet.block_specs(kind, slot="artifact:foo.alpha" if kind.startswith("foo") else "")
        ids = [s.id for s in specs]
        assert len(ids) == len(set(ids)), kind
        assert ids[-5:] == ["error", "timing", "relations", "integrity", "raw"], kind
        assert all(s.renderer in tdet.RENDERERS for s in specs), kind


def test_every_kind_in_the_gui_table_yields_ordered_blocks(state):
    spans = [
        _turn(state, "t", "turn", start=0, end=200),
        _llm(
            state,
            "t",
            "llm",
            "turn",
            start=1,
            end=2,
            tools=[{"name": "search", "parameters": {}}],
            tool_names=["search"],
            output={
                "content": "a",
                "tool_calls": [{"id": "1", "name": "search", "arguments": "{}"}],
                "finish_reason": "tool_calls",
                "usage": {"input_tokens": 1},
                "reasoning_content": "think",
                "thinking_blocks": [{"thinking": "think"}],
                "call": {"status": 200},
            },
        ),
        _tool(state, "t", "tool", "turn", start=3, end=4),
        _span(
            "t",
            "read",
            "skill.read",
            parent="turn",
            start=5,
            end=6,
            attrs={
                "skill.name": "pdf",
                "skill.id": "s1",
                "skill.read.via_tool": "read_skill",
                "tool.name": "read_skill",
                "tool.input.artifact_path": _artifact(
                    state, {"name": "read_skill", "params": {"skill": "pdf"}}, "read-in"
                ),
                "tool.output.artifact_path": _artifact(state, {"result": "## pdf\nbody"}, "read-out"),
            },
        ),
        _span(
            "t",
            "inject",
            "skill.inject",
            parent="turn",
            start=7,
            end=8,
            attrs={
                "skill.inject.count": 1,
                "skill.inject.artifact_path": _artifact(
                    state,
                    {"via": "system", "skills": [{"name": "pdf", "id": "s1"}], "sources": ["local"], "body_len": 42},
                    "inject",
                ),
            },
        ),
        _span(
            "t",
            "rewrite",
            "skill.rewrite",
            parent="turn",
            start=9,
            end=10,
            attrs={
                "skill.rewrite.input.artifact_path": _artifact(state, {"query": "q"}, "rw-in"),
                "skill.rewrite.output.artifact_path": _artifact(
                    state, {"need_retrieval": False, "rewritten_query": "q2"}, "rw-out"
                ),
            },
        ),
        _span(
            "t",
            "gate",
            "skill.gate",
            parent="turn",
            start=11,
            end=12,
            attrs={
                "skill.gate.candidate_count": 2,
                "skill.gate.selected_count": 1,
                "skill.gate.input.artifact_path": _artifact(
                    state,
                    {
                        "task": "do",
                        "candidates": [{"id": "a"}, {"id": "b"}],
                        "available_tools": ["x"],
                        "available_subagents": [],
                    },
                    "gate-in",
                ),
                "skill.gate.output.artifact_path": _artifact(state, {"selected": [{"id": "a"}]}, "gate-out"),
            },
        ),
        _span(
            "t",
            "curate",
            "context.curate",
            parent="turn",
            start=13,
            end=14,
            attrs={
                "context.curate.input.artifact_path": _artifact(
                    state, {"turn_id": "x", "session_key": SESSION}, "cu-in"
                ),
                "context.curate.output.artifact_path": _artifact(
                    state, {"produced": 1, "history_len": 3, "working_state": "ws"}, "cu-out"
                ),
            },
        ),
        _span(
            "t",
            "sub",
            "subagent.run",
            parent="turn",
            start=15,
            end=16,
            attrs={
                "subagent.task": "task text",
                "subagent.label": "helper",
                "subagent.task_id": "k",
                "subagent.origin_session": SESSION,
            },
        ),
        _span(
            "t",
            "ext",
            "subagent.external",
            parent="turn",
            start=17,
            end=18,
            attrs={
                "subagent.external.agent": "codex",
                "subagent.external.transport": "cli",
                "subagent.external.answer_chars": 10,
                "subagent.external.elapsed_ms": 5,
                "subagent.external.frames.0": "frame-a",
                "subagent.external.transcript.artifact_path": _artifact(state, "transcript text", "ext-tr"),
            },
        ),
        _span(
            "t",
            "recall",
            "memory.recall",
            parent="turn",
            start=19,
            end=20,
            attrs={
                "memory.query": "what",
                "memory.top_k": 3,
                "memory.hits": 1,
                "memory.recall.artifact_path": _artifact(
                    state, [{"text": "hit", "score": 0.5, "metadata": {}}], "recall"
                ),
            },
        ),
        _span(
            "t",
            "store",
            "memory.store",
            parent="turn",
            start=21,
            end=22,
            attrs={
                "memory.session_id": SESSION,
                "memory.message_count": 1,
                "memory.store.artifact_path": _artifact(
                    state, {"session_id": SESSION, "messages": [{"role": "user", "content": "m"}]}, "store"
                ),
            },
        ),
        _span(
            "t",
            "fb",
            "memory.feedback",
            parent="turn",
            start=23,
            end=24,
            attrs={"memory.session_id": SESSION, "memory.injected": ["a"], "memory.used": []},
        ),
        _span("t", "enq", "memory.enqueue", parent="turn", start=25, end=26),
        _span(
            "t",
            "pz",
            "personalize.classify",
            parent="turn",
            start=27,
            end=28,
            attrs={
                "personalize.step": "classify",
                "personalize.input.artifact_path": _artifact(
                    state, {"message": "m", "history": [{"role": "user", "content": "h"}]}, "pz-in"
                ),
                "personalize.output.artifact_path": _artifact(
                    state, {"result": {"domain": "x", "needs_clarification": False}}, "pz-out"
                ),
            },
        ),
        _span(
            "t",
            "plugin",
            "plugin.load",
            parent="turn",
            start=29,
            end=30,
            attrs={
                "plugin.name": "p",
                "plugin.contribution": "tool",
                "plugin.result_type": "Tool",
                "plugin.opt_out": False,
            },
        ),
        _span(
            "t",
            "foo",
            "foo.bar",
            parent="turn",
            start=31,
            end=32,
            attrs={
                "foo.alpha.artifact_path": _artifact(state, {"a": 1}, "foo-a"),
                "foo.beta.artifact_path": _artifact(state, {"b": [1, 2]}, "foo-b"),
                "foo.level": 3,
            },
        ),
        _span(
            "t",
            "boom",
            "tool.call",
            parent="turn",
            start=33,
            end=34,
            attrs={
                "tool.name": "boom",
                "tool.input.artifact_path": _artifact(state, {"name": "boom", "params": {}}, "boom-in"),
            },
            status={"code": "ERROR", "message": "exploded"},
        ),
    ]
    _append(state, spans)
    index = _ready(state)
    expected = {
        ("turn", "turn.input"): ["content", "media", "origin"],
        ("turn", "turn.output"): ["content"],
        ("llm", "llm.input"): ["messages", "model", "tools"],
        ("llm", "llm.thinking"): ["thinking", "thinkingBlocks"],
        ("llm", "llm.output"): ["content", "toolCalls", "finish", "usage", "response"],
        ("tool", "tool.input"): ["tool", "params", "schema"],
        ("tool", "tool.output"): ["result", "tool", "params", "schema"],
        ("read", "skill.read"): ["skill", "params", "content", "origin"],
        ("inject", "skill.inject"): ["skills", "origin", "stats"],
        ("rewrite", "io.input"): ["query"],
        ("rewrite", "io.output"): ["decision", "query"],
        ("gate", "io.input"): ["task", "candidates", "capabilities"],
        ("gate", "io.output"): ["skills", "stats"],
        ("curate", "io.input"): ["request"],
        ("curate", "io.output"): ["content", "stats"],
        ("sub", "subagent.run"): ["task", "agent"],
        ("ext", "artifact:subagent.external.transcript"): ["agent", "result", "transcript", "frames"],
        ("recall", "artifact:memory.recall"): ["query", "settings", "hits"],
        ("store", "artifact:memory.store"): ["messages", "stats"],
        ("fb", "summary"): ["injected", "used", "origin"],
        ("enq", "summary"): ["operation"],
        ("pz", "io.input"): ["content", "messages"],
        ("pz", "io.output"): ["result"],
        ("plugin", "summary"): ["plugin", "contribution", "result"],
        ("foo", "artifact:foo.alpha"): ["content", "attributes"],
        ("boom", "error"): ["error"],
    }
    for (span_id, slot), own in expected.items():
        entry = _entry(index, span_id, slot)
        descriptor = _describe(index, state, entry)
        ids = _ids(descriptor)
        tail = ["error"] if entry.operation_status == "error" and span_id != "boom" else []
        expected_ids = own + (["timing", "relations"] if span_id != "boom" else ["timing", "relations"])
        if span_id == "boom":
            expected_ids = ["error", "timing", "relations"]
        if entry.integrity:
            expected_ids = expected_ids + ["integrity"]
        expected_ids = expected_ids + ["raw"]
        assert ids == expected_ids, (span_id, slot, ids)
        assert len(ids) == len(set(ids))
        assert all(b.renderer in tdet.RENDERERS for b in descriptor.blocks)
        assert all(b.availability in tdet.AVAILABILITIES for b in descriptor.blocks)
        del tail
    enqueue = _describe(index, state, _entry(index, "enq", "summary"))
    assert enqueue.blocks[0].availability == tdet.EMPTY
    assert enqueue.notes == (tdet.NOTE_OUTER_ONLY,)
    boom = _describe(index, state, _entry(index, "boom", "error"))
    assert boom.blocks[0].availability == tdet.AVAILABLE
    body = _block(index, state, _entry(index, "boom", "error"), "error")
    assert {"key": "status_message", "value": "exploded", "source": "derived"} in body.data["items"]


# ── availability and values ───────────────────────────────────────────


def test_availability_states_and_real_values(state, tmp_path):
    big = {"pad": "x" * (600 * 1024)}
    folder = state / "logs" / "audit-artifacts" / "folder"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    spans = [
        _turn(state, "t", "turn", start=0, end=100),
        _span(
            "t",
            "miss",
            "tool.call",
            parent="turn",
            start=1,
            end=2,
            attrs={
                "tool.name": "x",
                "tool.input.artifact_path": str(state / "logs" / "audit-artifacts" / "nope.json"),
                "tool.output.artifact_path": str(outside),
            },
        ),
        _span("t", "dir", "foo.bar", parent="turn", start=3, end=4, attrs={"foo.dir.artifact_path": str(folder)}),
        _span(
            "t",
            "bad",
            "foo.bar",
            parent="turn",
            start=5,
            end=6,
            attrs={"foo.raw.artifact_path": _artifact(state, "not json", "raw")},
        ),
        _span(
            "t",
            "big",
            "foo.bar",
            parent="turn",
            start=7,
            end=8,
            attrs={"foo.big.artifact_path": _artifact(state, big, "big")},
        ),
        _span(
            "t",
            "zero",
            "tool.call",
            parent="turn",
            start=9,
            end=10,
            attrs={
                "tool.name": "z",
                "tool.input.artifact_path": _artifact(state, {"name": "z", "params": {}}, "z-in"),
                "tool.output.artifact_path": _artifact(state, {"result": ""}, "z-out"),
            },
        ),
        _span(
            "t",
            "pz",
            "personalize.extract",
            parent="turn",
            start=11,
            end=12,
            attrs={
                "personalize.input.artifact_path": _artifact(
                    state, {"original_message": "o", "question": "q", "answer": "a"}, "pe-in"
                ),
                "personalize.output.artifact_path": _artifact(state, {"result": False}, "pe-out"),
            },
        ),
    ]
    _append(state, spans)
    index = _ready(state)
    miss_in = _describe(index, state, _entry(index, "miss", "tool.input"))
    params = next(b for b in miss_in.blocks if b.id == "params")
    assert (params.availability, params.reason) == (tdet.MISSING, "artifact_missing")
    assert "artifact_missing" in miss_in.integrity and tdet.NOTE_INCOMPLETE in miss_in.notes
    miss_out = _describe(index, state, _entry(index, "miss", "tool.output"))
    result = next(b for b in miss_out.blocks if b.id == "result")
    assert (result.availability, result.reason) == (tdet.UNREADABLE, "artifact_outside_store")
    dir_entry = _entry(index, "dir", "artifact:foo.dir")
    content = next(b for b in _describe(index, state, dir_entry).blocks if b.id == "content")
    assert (content.availability, content.reason) == (tdet.UNREADABLE, "artifact_unreadable")
    bad_entry = _entry(index, "bad", "artifact:foo.raw")
    content = next(b for b in _describe(index, state, bad_entry).blocks if b.id == "content")
    assert (content.availability, content.renderer) == (tdet.AVAILABLE, tdet.TEXT)
    assert _block(index, state, bad_entry, "content").data == {"text": "not json"}
    big_entry = _entry(index, "big", "artifact:foo.big")
    content = next(b for b in _describe(index, state, big_entry).blocks if b.id == "content")
    assert (content.availability, content.reason) == (tdet.TRUNCATED, "artifact_truncated")
    body = _block(index, state, big_entry, "content")
    assert body.availability == tdet.TRUNCATED and body.renderer == tdet.TEXT and body.truncated
    assert body.data["text"].startswith('{"pad"') and "artifact_truncated" in body.integrity
    zero = _block(index, state, _entry(index, "zero", "tool.output"), "result")
    assert zero.availability == tdet.AVAILABLE and zero.data == {"text": ""}
    false_result = _block(index, state, _entry(index, "pz", "io.output"), "result")
    assert false_result.availability == tdet.AVAILABLE
    assert false_result.data["items"] == [{"key": "result", "value": False, "source": "artifact"}]
    agent = _describe(index, state, _entry(index, "turn", "turn.output"))
    assert "capabilities" not in _ids(agent)


# ── messages paging ───────────────────────────────────────────────────


def test_messages_page_with_missing_and_oversize_blobs(state):
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _llm(state, "t", "llm", "turn", start=1, end=2, v2_count=45, output={"content": "a"}),
        ],
    )
    shell_path = Path(_ready(state).entries()[0].entry_id and "")
    del shell_path
    index = _ready(state)
    entry = _entry(index, "llm", "llm.input")
    shell = json.loads(Path(tidx.tstore._span_attrs(index.span("t", "llm"))["llm.input.artifact_path"]).read_text())
    gone = shell["messages"][3]["$msg"]
    artifact_v2.message_path(state / "logs" / "audit-artifacts", gone).unlink()
    huge = shell["messages"][5]["$msg"]
    artifact_v2.message_path(state / "logs" / "audit-artifacts", huge).write_text(
        json.dumps({"role": "user", "content": "y" * (600 * 1024)})
    )
    pages = []
    cursor = None
    while True:
        body = _block(index, state, entry, "messages", cursor)
        pages.append(body)
        if body.next_cursor is None:
            break
        cursor = body.next_cursor
    assert len(pages) == 3 and [len(p.data["items"]) for p in pages] == [20, 20, 5]
    assert pages[0].total_items == 45
    assert pages[0].data["items"][3]["content"].startswith("[message blob missing")
    assert pages[0].data["items"][5]["content"].startswith("[message blob over the cap")
    assert "blob_missing" in pages[0].integrity and "blob_truncated" in pages[0].integrity
    assert pages[1].data["items"][0]["content"] == "message 20"
    system = _block(index, state, entry, "system")
    assert system.data == {"text": "message 0"}
    prompt = _block(index, state, entry, "prompt")
    assert prompt.data == {"text": "message 44"}
    descriptor = _describe(index, state, entry)
    messages = next(b for b in descriptor.blocks if b.id == "messages")
    assert messages.total_items == 45 and len(messages.preview) == 2


# ── schema association ────────────────────────────────────────────────


def _schema_block(index, state, span_id):
    return _block(index, state, _entry(index, span_id, "tool.input"), "schema")


def test_schema_proven_only_inside_the_sequential_window(state):
    schema_a = {"name": "foo", "parameters": {"v": 1}}
    schema_b = {"name": "foo", "parameters": {"v": 2}}
    spans = [
        _turn(state, "t", "turn", start=0, end=400),
        _llm(state, "t", "l1", "turn", start=1, end=10, tools=[schema_a], tool_names=["foo"], output={"content": ""}),
        _tool(state, "t", "t1", "turn", start=11, end=12, name="foo"),
        _tool(state, "t", "t1b", "turn", start=11, end=13, name="foo"),
        _llm(state, "t", "l2", "turn", start=20, end=30, tools=[schema_b], tool_names=["foo"], output={"content": ""}),
        _tool(state, "t", "t2", "turn", start=31, end=32, name="foo"),
        _llm(state, "t", "l3", "turn", start=40, end=50, tools=[schema_b], tool_names=["bar"], output={"content": ""}),
        _tool(state, "t", "late", "turn", start=41, end=42, name="foo"),
        _tool(state, "t", "unlisted", "turn", start=51, end=52, name="foo"),
        _tool(state, "t", "rootless", None, start=60, end=61, name="foo"),
    ]
    _append(state, spans)
    index = _ready(state)
    first = _schema_block(index, state, "t1")
    assert (
        first.availability == tdet.AVAILABLE
        and first.data["value"] == schema_a
        and first.data["source_span_id"] == "l1"
    )
    assert _schema_block(index, state, "t1b").data["source_span_id"] == "l1"
    second = _schema_block(index, state, "t2")
    assert (
        second.availability == tdet.AVAILABLE
        and second.data["value"] == schema_b
        and second.data["source_span_id"] == "l2"
    )
    late = _schema_block(index, state, "late")
    assert (late.availability, late.reason) == (tdet.NOT_RECORDED, tdet.REASON_SCHEMA_UNPROVEN)
    unlisted = _schema_block(index, state, "unlisted")
    assert (unlisted.availability, unlisted.reason) == (tdet.NOT_RECORDED, tdet.REASON_SCHEMA_UNPROVEN)
    rootless = _schema_block(index, state, "rootless")
    assert rootless.reason == tdet.REASON_SCHEMA_UNPROVEN


def test_schema_unproven_when_parent_llm_calls_overlap_or_are_unparseable(state):
    schema = {"name": "foo", "parameters": {}}
    spans = [
        _turn(state, "t", "turn", start=0, end=100),
        _llm(state, "t", "l1", "turn", start=0, end=10, tools=[schema], tool_names=["foo"], output={"content": ""}),
        _llm(state, "t", "l2", "turn", start=5, end=15, tools=[schema], tool_names=["foo"], output={"content": ""}),
        _tool(state, "t", "mid", "turn", start=12, end=13, name="foo"),
        _turn(state, "u", "turn2", start=0, end=100),
        _llm(state, "u", "m1", "turn2", start=0, end=10, tools=[schema], tool_names=["foo"], output={"content": ""}),
        _llm(state, "u", "m2", "turn2", start=5, end=15, tools=[schema], tool_names=["bar"], output={"content": ""}),
        _tool(state, "u", "mid2", "turn2", start=12, end=13, name="foo"),
        _turn(state, "v", "turn3", start=0, end=100),
        _llm(state, "v", "n1", "turn3", start=0, end=10, tools=[schema], tool_names=["foo"], output={"content": ""}),
        _tool(state, "v", "ok", "turn3", start=11, end=12, name="foo"),
        _turn(state, "w", "turn4", start=0, end=100),
        _tool(state, "w", "other_branch", "turn4", start=11, end=12, name="foo"),
        _turn(state, "x", "turn5", start=0, end=100),
        _llm(
            state,
            "x",
            "p1",
            "turn5",
            start=0,
            end=10,
            tools=[schema, schema],
            tool_names=["foo"],
            output={"content": ""},
        ),
        _tool(state, "x", "dup", "turn5", start=11, end=12, name="foo"),
        _turn(state, "y", "turn6", start=0, end=100),
        _llm(state, "y", "q1", "turn6", start=0, end=10, tools=[schema], tool_names=["foo"], output={"content": ""}),
        _tool(state, "y", "early", "turn6", start=5, end=6, name="foo"),
    ]
    bad_time = _llm(
        state, "v", "n2", "turn3", start=20, end=30, tools=[schema], tool_names=["foo"], output={"content": ""}
    )
    bad_time["startTime"] = "not a time"
    spans.append(bad_time)
    _append(state, spans)
    index = _ready(state)
    for span_id in ("mid", "mid2", "other_branch", "dup", "early", "ok"):
        body = _schema_block(index, state, span_id)
        assert (body.availability, body.reason) == (tdet.NOT_RECORDED, tdet.REASON_SCHEMA_UNPROVEN), span_id


# ── budgets ───────────────────────────────────────────────────────────


def test_response_budget_degrades_every_renderer(state, monkeypatch):
    deep = {"k": 1}
    for _ in range(12):
        deep = {"n": deep}
    huge_text = "t" * (600 * 1024)
    big_item = {"content": "é" * (150 * 1024)}
    big_attr = {"blob": "z" * (100 * 1024)}
    spans = [
        _turn(state, "t", "turn", start=0, end=100, content_in=huge_text),
        _span(
            "t",
            "deep",
            "foo.bar",
            parent="turn",
            start=1,
            end=2,
            attrs={"foo.deep.artifact_path": _artifact(state, deep, "deep")},
        ),
        _span(
            "t",
            "items",
            "foo.bar",
            parent="turn",
            start=3,
            end=4,
            attrs={"foo.list.artifact_path": _artifact(state, {"items": [big_item, {"content": "small"}]}, "items")},
        ),
        _span(
            "t",
            "attrs",
            "memory.enqueue",
            parent="turn",
            start=5,
            end=6,
            attrs={"memory.big": big_attr, "memory.big2": big_attr, "memory.small": 1},
        ),
        _span(
            "t",
            "refs",
            "subagent.external",
            parent="turn",
            start=7,
            end=8,
            attrs={**{f"subagent.external.frames.{i}": f"f{i}" for i in range(120)}, "subagent.external.agent": "a"},
        ),
        _span(
            "t",
            "note",
            "foo.bar",
            parent="turn",
            start=9,
            end=10,
            attrs={"foo.note.artifact_path": _artifact(state, "n" * (600 * 1024), "long-note")},
        ),
    ]
    _append(state, spans)
    index = _ready(state)
    text = _block(index, state, _entry(index, "turn", "turn.input"), "content")
    assert text.truncated and text.availability == tdet.TRUNCATED and text.data is None
    note = _block(index, state, _entry(index, "note", "artifact:foo.note"), "content")
    assert note.truncated and note.availability == tdet.TRUNCATED
    assert len(note.data["text"].encode()) <= tdet.ARTIFACT_LIMIT
    deep_body = _block(index, state, _entry(index, "deep", "artifact:foo.deep"), "content")
    assert deep_body.truncated and "$depth_truncated" in json.dumps(deep_body.data)
    monkeypatch.setattr(tdet, "RESPONSE_LIMIT", 200 * 1024)
    items_entry = _entry(index, "items", "artifact:foo.list")
    body = _block(index, state, items_entry, "content")
    assert body.renderer == tdet.JSON and body.truncated and body.availability == tdet.TRUNCATED
    assert tdet._size(body.data) <= 200 * 1024 - tdet.RESPONSE_RESERVE
    assert "$depth_truncated" in json.dumps(body.data) or body.data["value"].get("$oversize") is True
    monkeypatch.setattr(tdet, "RESPONSE_LIMIT", 1024 * 1024)
    monkeypatch.setattr(tdet, "VALUE_LIMIT", 1024)
    attrs_body = _block(index, state, _entry(index, "attrs", "summary"), "operation")
    assert attrs_body.truncated
    values = {item["key"]: item["value"] for item in attrs_body.data["items"]}
    assert values["memory.big"] == {"$oversize": True, "bytes": tdet._size(big_attr)}
    assert values["memory.small"] == 1
    refs = _block(index, state, _entry(index, "refs", "summary"), "frames")
    assert len(refs.data["items"]) == 50 and refs.next_cursor is not None
    second = _block(index, state, _entry(index, "refs", "summary"), "frames", refs.next_cursor)
    assert len(second.data["items"]) == 50 and second.data["offset"] == 50


def test_oversize_single_item_is_replaced_and_offset_advances(state, monkeypatch):
    messages = [{"role": "user", "content": "é" * (150 * 1024)}, {"role": "user", "content": "ok"}]
    shell = {"messages": messages, "tools": []}
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _span(
                "t",
                "llm",
                "llm.call",
                parent="turn",
                start=1,
                end=2,
                attrs={"llm.input.artifact_path": _artifact(state, shell, "llm-in")},
            ),
        ],
    )
    index = _ready(state)
    entry = _entry(index, "llm", "llm.input")
    monkeypatch.setattr(tdet, "RESPONSE_LIMIT", 400 * 1024)
    first = _block(index, state, entry, "messages")
    assert first.truncated and first.data["items"][0]["$oversize"] is True and first.data["items"][0]["index"] == 0
    assert first.next_cursor is not None
    second = _block(index, state, entry, "messages", first.next_cursor)
    assert second.data["items"] == [{"role": "user", "content": "ok"}] and second.next_cursor is None


def test_describe_budget_marks_unopened_blocks_not_loaded(state, monkeypatch):
    reads = {"artifacts": 0, "blobs": 0}
    original = tconv._read_artifact

    def counting(state_dir, path, limit=tconv._ARTIFACT_LIMIT):
        if "_messages" in str(path):
            reads["blobs"] += 1
        else:
            reads["artifacts"] += 1
        return original(state_dir, path, limit)

    monkeypatch.setattr(tconv, "_read_artifact", counting)
    many = {f"foo.k{i:02d}.artifact_path": _artifact(state, {"k": i}, f"many-{i}") for i in range(20)}
    spans = [
        _turn(state, "t", "turn", start=0, end=100),
        _span("t", "many", "foo.bar", parent="turn", start=1, end=2, attrs=many),
        _llm(
            state,
            "t",
            "llm",
            "turn",
            start=3,
            end=4,
            v2_count=500,
            output={
                "content": "a",
                "tool_calls": [{"id": "1", "name": "x", "arguments": "{}"}],
                "usage": {"input_tokens": 1},
            },
        ),
    ]
    _append(state, spans)
    index = _ready(state)
    reads["artifacts"] = reads["blobs"] = 0
    descriptor = _describe(index, state, _entry(index, "llm", "llm.input"))
    assert reads["blobs"] <= tdet.DESCRIBE_BLOBS and reads["artifacts"] <= tdet.DESCRIBE_ARTIFACTS
    assert {b.id for b in descriptor.blocks} >= {"messages", "system", "prompt", "model", "tools", "request"}
    monkeypatch.setattr(tdet, "DESCRIBE_ARTIFACTS", 1)
    out = _describe(index, state, _entry(index, "llm", "llm.output"))
    not_loaded = {b.id for b in out.blocks if b.reason == tdet.REASON_NOT_LOADED}
    assert not_loaded >= {"toolCalls", "usage", "response"} or all(
        b.reason != tdet.REASON_NOT_LOADED for b in out.blocks
    )
    monkeypatch.setattr(tdet, "DESCRIBE_ARTIFACTS", 0)
    out = _describe(index, state, _entry(index, "llm", "llm.output"))
    assert {b.id for b in out.blocks} >= {"content", "toolCalls", "finish", "usage", "response"}
    assert all(
        b.reason == tdet.REASON_NOT_LOADED for b in out.blocks if b.id in ("content", "toolCalls", "usage", "response")
    )
    body = _block(index, state, _entry(index, "llm", "llm.output"), "toolCalls")
    assert body.availability == tdet.AVAILABLE and body.total_items == 1


def test_large_but_valid_artifacts_parse_in_describe_and_block_alike(state):
    tools = [{"name": "search", "parameters": {"schema": "s" * (120 * 1024)}}]
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _llm(
                state, "t", "llm", "turn", start=1, end=2, tools=tools, tool_names=["search"], output={"content": "a"}
            ),
            _tool(state, "t", "tool", "turn", start=3, end=4, params={"big": "p" * (90 * 1024)}),
        ],
    )
    index = _ready(state)
    descriptor = _describe(index, state, _entry(index, "llm", "llm.input"))
    tools_block = next(b for b in descriptor.blocks if b.id == "tools")
    assert tools_block.availability == tdet.AVAILABLE and tools_block.reason is None
    body = _block(index, state, _entry(index, "llm", "llm.input"), "tools")
    assert body.availability == tdet.AVAILABLE and body.total_items == 1
    params = next(b for b in _describe(index, state, _entry(index, "tool", "tool.input")).blocks if b.id == "params")
    assert params.availability == tdet.AVAILABLE
    schema = _schema_block(index, state, "tool")
    assert schema.availability == tdet.AVAILABLE and schema.data["value"]["name"] == "search"


def test_descriptor_preview_budget_drops_largest_previews_first(state, monkeypatch):
    _append(state, [_turn(state, "t", "turn", start=0, end=100, content_in="i" * 5000, content_out="o" * 5000)])
    index = _ready(state)
    monkeypatch.setattr(tdet, "RESPONSE_LIMIT", tdet.RESPONSE_RESERVE + 1500)
    descriptor = _describe(index, state, _entry(index, "turn", "turn.input"))
    assert descriptor.truncated
    dropped = [b for b in descriptor.blocks if b.reason == tdet.REASON_PREVIEW_DROPPED]
    assert dropped and dropped[0].id == "content" and dropped[0].preview is None


# ── info, timing, relations ───────────────────────────────────────────


def test_info_notes_timing_and_relations(state):
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100, content_out=None, extra={"turn.in_progress": True}),
            _tool(state, "t", "tool", "turn", start=1, end=3, extra={"tool.duration_ms": 1999}),
            _span(
                "S",
                "sub",
                "session.turn",
                start=10,
                end=20,
                session="sub",
                attrs={
                    "trace.dispatched_in_trace_id": "t",
                    "trace.dispatched_by_span_id": "turn",
                    "turn.input_preview": "x",
                },
            ),
        ],
    )
    index = _ready(state)
    turn = _describe(index, state, _entry(index, "turn", "turn.input"))
    assert tdet.NOTE_IN_PROGRESS in turn.notes and turn.operation_status == "running"
    tool_in = _entry(index, "tool", "tool.input")
    descriptor = _describe(index, state, tool_in)
    assert descriptor.operation_status == "ok" and not descriptor.failure_entry
    timing = _block(index, state, tool_in, "timing")
    values = {item["key"]: item["value"] for item in timing.data["items"]}
    assert (
        values["charged_ms"] == 0
        and values["timing_basis"] == "zero"
        and values["duration_owner"] == "t:tool:tool.output"
    )
    assert values["tool.duration_ms"] == 1999
    relations = _block(index, state, tool_in, "relations")
    rel = {item["key"]: item["value"] for item in relations.data["items"]}
    assert rel["sibling_entries"] == ["t:tool:tool.output"] and rel["turn_number"] == 1
    sub = _entry(index, "sub", "turn.input")
    sub_rel = {item["key"]: item["value"] for item in _block(index, state, sub, "relations").data["items"]}
    assert sub_rel["trace.dispatched_in_trace_id"] == "t" and sub_rel["origin"] == "subagent"
    raw = _block(index, state, tool_in, "raw")
    assert raw.data["value"]["attributes"]["tool.name"] == "search"
    assert {a["key"] for a in raw.data["value"]["artifacts"]} == {"tool.input", "tool.output"}
    assert "result" not in json.dumps(raw.data["value"]["artifacts"])


# ── errors and version consistency ────────────────────────────────────


def test_error_paths(state, tmp_path):
    link_target = tmp_path / "secret.json"
    link_target.write_text('{"content": "secret"}', encoding="utf-8")
    link = state / "logs" / "audit-artifacts" / "link.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(link_target, link)
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _span("t", "lnk", "foo.bar", parent="turn", start=1, end=2, attrs={"foo.link.artifact_path": str(link)}),
        ],
    )
    index = _ready(state)
    assert tdet.describe(index, "nope", state=state) is None
    entry = _entry(index, "turn", "turn.input")
    with pytest.raises(tdet.EntryGoneError):
        tdet.read_block(index, "nope", "content", state=state, entry_revision=1, epoch=index.epoch)
    with pytest.raises(tdet.UnknownBlockError):
        _block(index, state, entry, "schema")
    with pytest.raises(tdet.UnknownBlockError):
        _block(index, state, entry, "error")
    with pytest.raises(tidx.CursorExpiredError):
        _block(index, state, entry, "content", cursor="garbage")
    with pytest.raises(tidx.CursorExpiredError):
        _block(
            index,
            state,
            entry,
            "content",
            cursor=tdet._encode_cursor(entry.entry_id, entry.revision + 1, index.epoch, 0),
        )
    with pytest.raises(tdet.RevisionChangedError) as excinfo:
        tdet.read_block(
            index, entry.entry_id, "content", state=state, entry_revision=entry.revision + 5, epoch=index.epoch
        )
    assert excinfo.value.current_revision == entry.revision
    with pytest.raises(tdet.RevisionChangedError):
        tdet.read_block(index, entry.entry_id, "content", state=state, entry_revision=entry.revision, epoch="other")
    linked = _block(index, state, _entry(index, "lnk", "artifact:foo.link"), "content")
    assert (linked.availability, linked.reason) == (tdet.UNREADABLE, "artifact_outside_store")
    assert tdet.describe(index, entry.entry_id, state=state, expected_revision=entry.revision + 1).revision_changed


def test_capture_snapshot_survives_a_refresh_between_capture_and_io(state, monkeypatch):
    _append(state, [_turn(state, "t", "turn", start=0, end=1, content_out=None, extra={"turn.in_progress": True})])
    index = _ready(state)
    entry = _entry(index, "turn", "turn.input")
    original_capture = index.capture

    def racing_capture(entry_id):
        view = original_capture(entry_id)
        _append(
            state,
            [_turn(state, "t", "turn", start=0, end=9, content_in="rewritten", content_out="final", files="turn-v2")],
        )
        index.refresh_sync(None)
        return view

    monkeypatch.setattr(index, "capture", racing_capture)
    body = _block(index, state, entry, "content")
    assert body.entry_revision == entry.revision
    assert body.data == {"text": "hello"}
    monkeypatch.setattr(index, "capture", original_capture)
    current = _entry(index, "turn", "turn.input")
    assert current.revision > entry.revision
    with pytest.raises(tdet.RevisionChangedError):
        _block(index, state, entry, "content")


def test_page_cursor_is_bound_to_revision_and_epoch(state):
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _llm(
                state,
                "t",
                "llm",
                "turn",
                start=1,
                end=2,
                v2_count=25,
                output={"content": "a"},
                extra={"turn.in_progress": True},
            ),
        ],
    )
    index = _ready(state)
    entry = _entry(index, "llm", "llm.input")
    first = _block(index, state, entry, "messages")
    assert first.next_cursor is not None
    _append(state, [_llm(state, "t", "llm", "turn", start=1, end=5, v2_count=25, output={"content": "b"})])
    index.refresh_sync(None)
    with pytest.raises(tdet.RevisionChangedError):
        _block(index, state, entry, "messages", first.next_cursor)
    fresh = _entry(index, "llm", "llm.input")
    with pytest.raises(tidx.CursorExpiredError):
        _block(index, state, fresh, "messages", first.next_cursor)
    log = state / "logs" / "audit-spans.log"
    data = log.read_bytes()
    log.write_bytes(data[: len(data) // 2])
    index.refresh_sync(None)
    index.refresh_sync(None)
    assert index.epoch != first.epoch
    rebuilt = next((e for e in index.entries() if e.span_id == "llm" and e.slot == "llm.input"), None)
    if rebuilt is not None:
        with pytest.raises((tidx.CursorExpiredError, tdet.RevisionChangedError)):
            tdet.read_block(
                index,
                rebuilt.entry_id,
                "messages",
                state=state,
                entry_revision=rebuilt.revision,
                epoch=first.epoch,
                cursor=first.next_cursor,
            )


# ── review regressions ────────────────────────────────────────────────


def test_plain_text_artifacts_are_available_text(state):
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _span(
                "t",
                "ext",
                "subagent.external",
                parent="turn",
                start=1,
                end=2,
                attrs={
                    "subagent.external.agent": "codex",
                    "subagent.external.transcript.artifact_path": _artifact(state, "plain transcript", "tr"),
                },
            ),
            _span(
                "t",
                "foo",
                "foo.bar",
                parent="turn",
                start=3,
                end=4,
                attrs={"foo.note.artifact_path": _artifact(state, "just a note", "note")},
            ),
        ],
    )
    index = _ready(state)
    ext = _entry(index, "ext", "artifact:subagent.external.transcript")
    descriptor = _describe(index, state, ext)
    transcript = next(b for b in descriptor.blocks if b.id == "transcript")
    assert (transcript.availability, transcript.reason, transcript.renderer) == (tdet.AVAILABLE, None, tdet.TEXT)
    assert descriptor.integrity == () and "integrity" not in _ids(descriptor)
    body = _block(index, state, ext, "transcript")
    assert body.availability == tdet.AVAILABLE and body.data == {"text": "plain transcript"} and body.integrity == ()
    note = _entry(index, "foo", "artifact:foo.note")
    content = next(b for b in _describe(index, state, note).blocks if b.id == "content")
    assert (content.renderer, content.availability) == (tdet.TEXT, tdet.AVAILABLE)
    assert _block(index, state, note, "content").data == {"text": "just a note"}


def test_integrity_block_covers_problems_found_while_reading(state):
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _llm(state, "t", "llm", "turn", start=1, end=2, v2_count=6, output={"content": "a"}),
            _tool(state, "t", "tool", "turn", start=3, end=4),
        ],
    )
    index = _ready(state)
    llm_in = _entry(index, "llm", "llm.input")
    tool_out = _entry(index, "tool", "tool.output")
    assert llm_in.integrity == () and tool_out.integrity == ()
    shell = json.loads(Path(tidx.tstore._span_attrs(index.span("t", "llm"))["llm.input.artifact_path"]).read_text())
    artifact_v2.message_path(state / "logs" / "audit-artifacts", shell["messages"][1]["$msg"]).unlink()
    Path(tidx.tstore._span_attrs(index.span("t", "tool"))["tool.output.artifact_path"]).unlink()
    descriptor = _describe(index, state, llm_in)
    assert "blob_missing" in descriptor.integrity and "integrity" in _ids(descriptor)
    body = _block(index, state, llm_in, "integrity")
    assert "blob_missing" in body.data["items"]
    tool_descriptor = _describe(index, state, tool_out)
    assert "artifact_missing" in tool_descriptor.integrity and "integrity" in _ids(tool_descriptor)
    assert "artifact_missing" in _block(index, state, tool_out, "integrity").data["items"]
    assert _ids(tool_descriptor).index("integrity") == _ids(tool_descriptor).index("raw") - 1


def test_derived_blocks_keep_source_failures(state, monkeypatch):
    big = {"turn_id": "x", "session_key": SESSION, "pad": "x" * (600 * 1024)}
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _span(
                "t",
                "curate",
                "context.curate",
                parent="turn",
                start=1,
                end=2,
                attrs={"context.curate.input.artifact_path": str(state / "logs" / "audit-artifacts" / "gone.json")},
            ),
            _span(
                "t",
                "big",
                "context.curate",
                parent="turn",
                start=3,
                end=4,
                attrs={"context.curate.input.artifact_path": _artifact(state, big, "big-curate")},
            ),
            _span(
                "t",
                "raw",
                "context.curate",
                parent="turn",
                start=5,
                end=6,
                attrs={"context.curate.input.artifact_path": _artifact(state, "not json", "raw-curate")},
            ),
            _llm(
                state,
                "t",
                "llm",
                "turn",
                start=7,
                end=8,
                output={"content": "a", "usage": {"input_tokens": 1}},
                extra={"llm.usage.input_tokens": 1, "llm.http_status": 200},
            ),
        ],
    )
    index = _ready(state)
    Path(tidx.tstore._span_attrs(index.span("t", "llm"))["llm.output.artifact_path"]).unlink()
    request = _block(index, state, _entry(index, "curate", "io.input"), "request")
    assert (request.availability, request.reason) == (tdet.MISSING, "artifact_missing") and request.data is None
    assert "artifact_missing" in request.integrity
    big_request = _block(index, state, _entry(index, "big", "io.input"), "request")
    assert (big_request.availability, big_request.reason) == (tdet.TRUNCATED, "artifact_truncated")
    raw_request = _block(index, state, _entry(index, "raw", "io.input"), "request")
    assert (raw_request.availability, raw_request.reason) == (tdet.UNREADABLE, "artifact_unreadable")
    usage = _block(index, state, _entry(index, "llm", "llm.output"), "usage")
    assert usage.availability == tdet.AVAILABLE and usage.reason == "artifact_missing"
    assert {item["key"] for item in usage.data["items"]} == {
        "llm.usage.input_tokens"
    } and "artifact_missing" in usage.integrity
    response = _block(index, state, _entry(index, "llm", "llm.output"), "response")
    assert response.availability == tdet.AVAILABLE and response.reason == "artifact_missing"
    assert response.data["value"] == {"llm.http_status": 200}
    monkeypatch.setattr(tdet, "DESCRIBE_ARTIFACTS", 0)
    descriptor = _describe(index, state, _entry(index, "curate", "io.input"))
    request_block = next(b for b in descriptor.blocks if b.id == "request")
    assert (request_block.availability, request_block.reason) == (tdet.AVAILABLE, tdet.REASON_NOT_LOADED)
