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
from raven.trajectory import store as tstore

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
        assert ids[-7:] == ["error", "timing", "relations", "integrity", "raw", "files", "file"], kind
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
            extra={"llm.usage.input_tokens": 1},
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
        ("llm", "llm.input"): ["messages", "model", "tools", "outline"],
        ("llm", "llm.thinking"): ["thinking", "thinkingBlocks"],
        ("llm", "llm.output"): ["content", "toolCalls", "finish", "usage", "response"],
        ("tool", "tool.input"): ["params", "schema"],
        ("tool", "tool.output"): ["result", "params", "schema"],
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
        ("store", "artifact:memory.store"): ["messages", "stats", "outline"],
        ("fb", "summary"): ["injected", "used"],
        ("enq", "summary"): ["operation"],
        ("pz", "io.input"): ["content", "messages", "outline"],
        ("pz", "io.output"): ["result"],
        ("plugin", "summary"): ["plugin", "contribution", "result"],
        ("foo", "artifact:foo.alpha"): ["content", "attributes"],
        ("boom", "error"): ["error"],
    }
    for (span_id, slot), own in expected.items():
        entry = _entry(index, span_id, slot)
        descriptor = _describe(index, state, entry)
        ids = _ids(descriptor)
        # Only the entry charged a clock of its own carries a timing block.
        timed = entry.timing_basis not in ("zero", "shared", "not_recorded")
        expected_ids = own + (["timing"] if timed else []) + ["relations"]
        if span_id == "boom":
            expected_ids = ["error"] + (["timing"] if timed else []) + ["relations"]
        if entry.integrity:
            expected_ids = expected_ids + ["integrity"]
        expected_ids = expected_ids + ["raw"]
        # A span that names artifact files lists them, and reads them one by one, after the raw record.
        attrs = tstore._span_attrs(index.capture(entry.entry_id).span or {})
        if any(key.endswith(".artifact_path") for key in attrs):
            expected_ids = expected_ids + ["files", "file"]
        assert ids == expected_ids, (span_id, slot, ids)
        assert len(ids) == len(set(ids))
        assert all(b.renderer in tdet.RENDERERS for b in descriptor.blocks)
        assert all(b.availability in tdet.AVAILABILITIES for b in descriptor.blocks)
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


def test_a_page_of_wide_text_is_measured_as_sent_and_comes_back_whole(state):
    """Twenty messages of 15000 CJK characters are about 880 KiB in UTF-8, the
    form the transport sends; counted as ASCII escapes they would be twice that."""
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _llm(state, "t", "llm", "turn", start=1, end=2, v2_count=20, output={"content": "a"}),
        ],
    )
    index = _ready(state)
    entry = _entry(index, "llm", "llm.input")
    shell = json.loads(Path(tidx.tstore._span_attrs(index.span("t", "llm"))["llm.input.artifact_path"]).read_text())
    wide = "\u4e2d" * 15000
    for ref in shell["messages"]:
        artifact_v2.message_path(state / "logs" / "audit-artifacts", ref["$msg"]).write_text(
            json.dumps({"role": "user", "content": wide}, ensure_ascii=False), encoding="utf-8"
        )
    body = _block(index, state, entry, "messages")
    assert len(body.data["items"]) == 20
    assert body.next_cursor is None and not body.truncated
    assert all(item["content"] == wide for item in body.data["items"])


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
    messages = [{"role": "user", "content": "é" * (200 * 1024)}, {"role": "user", "content": "ok"}]
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
            extra={"llm.usage.input_tokens": 1},
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
    assert not_loaded >= {"toolCalls", "response"} or all(b.reason != tdet.REASON_NOT_LOADED for b in out.blocks), [
        (b.id, b.availability, b.reason) for b in out.blocks
    ]
    monkeypatch.setattr(tdet, "DESCRIBE_ARTIFACTS", 0)
    out = _describe(index, state, _entry(index, "llm", "llm.output"))
    assert {b.id for b in out.blocks} >= {"content", "toolCalls", "finish", "usage", "response"}
    assert all(b.reason == tdet.REASON_NOT_LOADED for b in out.blocks if b.id in ("content", "toolCalls", "response"))
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
    # The input is charged nothing, so it carries no timing block; the output owns the clock.
    assert "timing" not in _ids(descriptor)
    with pytest.raises(tdet.UnknownBlockError):
        _block(index, state, tool_in, "timing")
    tool_out = _entry(index, "tool", "tool.output")
    timing = _block(index, state, tool_out, "timing")
    values = {item["key"]: item["value"] for item in timing.data["items"]}
    assert (
        values["charged_ms"] == 2000
        and values["timing_basis"] == "span_full"
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
    # The usage block reads the normalized counters alone, so the lost artifact does not touch it.
    usage = _block(index, state, _entry(index, "llm", "llm.output"), "usage")
    assert usage.availability == tdet.AVAILABLE and usage.reason is None
    assert {item["key"] for item in usage.data["items"]} == {"input_tokens"} and not usage.integrity
    response = _block(index, state, _entry(index, "llm", "llm.output"), "response")
    assert response.availability == tdet.AVAILABLE and response.reason == "artifact_missing"
    assert response.data["value"] == {"llm.http_status": 200}
    monkeypatch.setattr(tdet, "DESCRIBE_ARTIFACTS", 0)
    descriptor = _describe(index, state, _entry(index, "curate", "io.input"))
    request_block = next(b for b in descriptor.blocks if b.id == "request")
    assert (request_block.availability, request_block.reason) == (tdet.AVAILABLE, tdet.REASON_NOT_LOADED)


def test_generic_json_with_odd_messages_fields_does_not_crash(state):
    spans = [_turn(state, "t", "turn", start=0, end=100)]
    for name, value in (("num", 1), ("flag", True), ("none", None), ("obj", {"a": 1}), ("list", [1, {"$msg": "x"}])):
        spans.append(
            _span(
                "t",
                name,
                "foo.bar",
                parent="turn",
                start=1,
                end=2,
                attrs={f"foo.{name}.artifact_path": _artifact(state, {"messages": value, "k": 1}, f"odd-{name}")},
            )
        )
    _append(state, spans)
    index = _ready(state)
    for name in ("num", "flag", "none", "obj", "list"):
        entry = _entry(index, name, f"artifact:foo.{name}")
        descriptor = _describe(index, state, entry)
        content = next(b for b in descriptor.blocks if b.id == "content")
        assert (content.availability, content.renderer) == (tdet.AVAILABLE, tdet.JSON), name
        body = _block(index, state, entry, "content")
        assert body.data["value"]["k"] == 1, name
        assert "integrity" not in _ids(descriptor), name


def test_bad_json_blob_is_reported_by_the_integrity_block(state):
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _llm(state, "t", "llm", "turn", start=1, end=2, v2_count=5, output={"content": "a"}),
        ],
    )
    index = _ready(state)
    entry = _entry(index, "llm", "llm.input")
    shell = json.loads(Path(tidx.tstore._span_attrs(index.span("t", "llm"))["llm.input.artifact_path"]).read_text())
    artifact_v2.message_path(state / "logs" / "audit-artifacts", shell["messages"][0]["$msg"]).write_text(
        "{bad json", encoding="utf-8"
    )
    descriptor = _describe(index, state, entry)
    assert "blob_missing" in descriptor.integrity and "integrity" in _ids(descriptor)
    integrity = next(b for b in descriptor.blocks if b.id == "integrity")
    assert integrity.preview == list(descriptor.integrity)[:3]
    body = _block(index, state, entry, "integrity")
    assert body.data["items"] == list(descriptor.integrity) and body.data["complete"] is True
    assert not body.truncated
    page = _block(index, state, entry, "messages")
    assert "blob_missing" in page.integrity and page.data["items"][0]["content"].startswith("[message blob missing")


def test_integrity_read_is_bounded_and_marks_partial_results(state, monkeypatch):
    opens = {"n": 0}
    original = tconv._read_artifact

    def counting(state_dir, path, limit=tconv._ARTIFACT_LIMIT):
        opens["n"] += 1
        return original(state_dir, path, limit)

    monkeypatch.setattr(tconv, "_read_artifact", counting)
    many = {f"foo.k{i:02d}.artifact_path": _artifact(state, {"k": i}, f"many-{i}") for i in range(30)}
    many["foo.k05.artifact_path"] = str(state / "logs" / "audit-artifacts" / "absent.json")
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _span("t", "many", "foo.bar", parent="turn", start=1, end=2, attrs=many),
        ],
    )
    index = _ready(state)
    entry = _entry(index, "many", "artifact:foo.k00")
    opens["n"] = 0
    descriptor = _describe(index, state, entry)
    assert opens["n"] <= tdet.DESCRIBE_ARTIFACTS
    assert "integrity" not in _ids(descriptor)
    missing_entry = _entry(index, "many", "artifact:foo.k05")
    opens["n"] = 0
    body = _block(index, state, missing_entry, "integrity")
    assert opens["n"] <= tdet.DESCRIBE_ARTIFACTS
    assert "artifact_missing" in body.data["items"]
    gone_out = _tool(state, "t", "tool", "turn", start=3, end=4)
    gone_out["attributes"]["tool.output.artifact_path"] = str(state / "logs" / "audit-artifacts" / "gone-out.json")
    _append(state, [gone_out])
    index.refresh_sync(None)
    tool_out = _entry(index, "tool", "tool.output")
    monkeypatch.setattr(tdet, "DESCRIBE_ARTIFACTS", 1)
    monkeypatch.setattr(tdet, "DESCRIBE_BLOBS", 0)
    opens["n"] = 0
    partial = _block(index, state, tool_out, "integrity")
    assert opens["n"] <= 1
    assert "artifact_missing" in partial.data["items"]
    assert partial.data["complete"] is False and partial.truncated
    with pytest.raises(tdet.UnknownBlockError):
        monkeypatch.setattr(tdet, "DESCRIBE_ARTIFACTS", 0)
        _block(index, state, entry, "integrity")


# ── the message outline ───────────────────────────────────────────────


def _items(block):
    return list(block.data["items"])


def test_outline_pages_every_message_with_a_cursor_to_its_body(state):
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _llm(state, "t", "llm", "turn", start=1, end=2, v2_count=450, output={"content": "a"}),
        ],
    )
    index = _ready(state)
    entry = _entry(index, "llm", "llm.input")
    descriptor = _describe(index, state, entry)
    outline_block = next(b for b in descriptor.blocks if b.id == "outline")
    assert outline_block.preview is None and outline_block.total_items == 450
    first = _block(index, state, entry, "outline")
    assert first.renderer == tdet.ITEMS and first.total_items == 450
    rows = _items(first)
    assert len(rows) == tdet.OUTLINE_PAGE and first.next_cursor is not None
    assert [r["index"] for r in rows] == list(range(200))
    assert rows[7] == {
        "index": 7,
        "role": "user",
        "bytes": rows[7]["bytes"],
        "chars": len("message 7"),
        "preview": "message 7",
        "partial": False,
        "missing": False,
        "cursor": rows[7]["cursor"],
    }
    assert rows[7]["bytes"] > 0
    second = _block(index, state, entry, "outline", cursor=first.next_cursor)
    third = _block(index, state, entry, "outline", cursor=second.next_cursor)
    assert [r["index"] for r in _items(second)] == list(range(200, 400))
    assert [r["index"] for r in _items(third)] == list(range(400, 450)) and third.next_cursor is None
    # A row's cursor opens the message page that starts with that very message.
    body = _block(index, state, entry, "messages", cursor=_items(second)[13]["cursor"])
    assert body.data["offset"] == 213 and body.data["items"][0]["content"] == "message 213"
    assert body.total_items == 450


def test_outline_reads_big_blobs_at_the_top_level_and_calls_them_partial(state):
    nested = {"tool_calls": [{"function": {"role": "x", "content": "inner", "arguments": '{"content": 1}'}}]}
    tail_role = {**nested, "content": 'He said "hi" é ' + "x" * 9000, "role": "assistant"}
    head_role = {**nested, "role": "assistant", "content": "lead words " + "y" * 9000}
    small = {
        "role": "user",
        "content": [{"type": "text", "text": "a part"}, {"type": "image_url", "image_url": {"url": "u"}}],
    }
    refs = [_blob(state, head_role), _blob(state, tail_role), _blob(state, small), {"$msg": "0" * 40}]
    shell = {"artifactFormat": artifact_v2.ARTIFACT_FORMAT, "messages": refs, "prompt": refs[-1], "tools": []}
    attrs = {"llm.model": "m", "llm.input.artifact_path": _artifact(state, shell, "big-in")}
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _span("t", "llm", "llm.call", parent="turn", start=1, end=2, attrs=attrs),
        ],
    )
    index = _ready(state)
    rows = _items(_block(index, state, _entry(index, "llm", "llm.input"), "outline"))
    assert rows[0]["role"] == "assistant" and rows[0]["partial"] and rows[0]["chars"] is None
    assert rows[0]["preview"] == "lead words " + "y" * (tdet.OUTLINE_PREVIEW_CHARS - len("lead words "))
    assert rows[0]["bytes"] > tdet.OUTLINE_BLOB_BYTES
    # The content came first and runs past the prefix: its first words are read, the role after it is not reached.
    assert rows[1]["role"] == "unknown" and rows[1]["partial"] and not rows[1]["missing"]
    assert rows[1]["preview"].startswith('He said "hi" é xxxx')
    assert rows[2] == {**rows[2], "role": "user", "preview": "a part", "chars": len("a part"), "partial": False}
    assert rows[3]["missing"] and rows[3]["role"] == "unknown" and rows[3]["bytes"] is None


def test_scan_top_level_accepts_only_complete_top_level_fields():
    scan = tdet._scan_top_level
    assert scan('{"role":"user","content":"hi"}') == {"role": "user", "content": "hi", "content_kind": "text"}
    nested = '{"tool_calls":[{"function":{"role":"x","arguments":"{\\"content\\": 1}"}}],"role":"assistant","content":"He said \\"hi\\" \\u00e9"}'
    assert scan(nested) == {"role": "assistant", "content": 'He said "hi" é', "content_kind": "text"}
    cut = scan('{"role":"user","content":"cut off in the mid')
    assert cut["role"] == "user" and cut["content"] is None
    long_cut = scan('{"content":"' + "z" * 100)
    assert long_cut["content"] == "z" * tdet.OUTLINE_PREVIEW_CHARS and long_cut["role"] is None
    half_key = scan('{"role":"user","cont')
    assert half_key == {"role": "user", "content": None, "content_kind": None}
    parts = scan('{"role":"user","content":[{"type":"text","text":"first part"},{"type":"image_url"}]}')
    assert parts == {"role": "user", "content": "first part", "content_kind": "array"}
    assert scan('{"role":"user","content":[{"type":"image_url","image_url":{"url":"u"}}]}')["content"] == "[multipart]"
    assert scan("not json at all") == {"role": None, "content": None, "content_kind": None}
    assert scan('{"role":5,"content":"x"}') == {"role": None, "content": "x", "content_kind": "text"}


# ── blocks that say nothing, and clocks nobody owns ───────────────────


def test_empty_model_output_blocks_are_not_listed(state):
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _llm(
                state,
                "t",
                "quiet",
                "turn",
                start=1,
                end=2,
                output={"content": "", "tool_calls": [], "finish_reason": "stop"},
                extra={"llm.usage.input_tokens": 3, "llm.usage.output_tokens": 0},
            ),
            _llm(
                state,
                "t",
                "loud",
                "turn",
                start=3,
                end=4,
                output={
                    "content": "x",
                    "tool_calls": [{"id": "1", "name": "f", "arguments": "{}"}],
                    "finish_reason": "tool_calls",
                },
            ),
        ],
    )
    index = _ready(state)
    quiet = _ids(_describe(index, state, _entry(index, "quiet", "llm.output")))
    assert "content" not in quiet and "toolCalls" not in quiet and "response" not in quiet
    assert quiet[:2] == ["finish", "usage"]
    loud = _ids(_describe(index, state, _entry(index, "loud", "llm.output")))
    assert loud[:3] == ["content", "toolCalls", "finish"] and "usage" not in loud
    # Only the user's own words keep an empty content block: an empty reply is itself information.
    user = _ids(_describe(index, state, _entry(index, "turn", "turn.input")))
    assert user[0] == "content"


def test_untimed_entries_carry_no_timing_block(state):
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _llm(state, "t", "llm", "turn", start=1, end=3, output={"content": "a", "reasoning_content": "deep"}),
            _tool(state, "t", "tool", "turn", start=4, end=6),
        ],
    )
    index = _ready(state)
    untimed = [("turn", "turn.input"), ("llm", "llm.input"), ("llm", "llm.thinking"), ("tool", "tool.input")]
    timed = [("turn", "turn.output"), ("llm", "llm.output"), ("tool", "tool.output")]
    for span_id, slot in untimed:
        assert "timing" not in _ids(_describe(index, state, _entry(index, span_id, slot))), (span_id, slot)
    for span_id, slot in timed:
        values = {
            i["key"]: i["value"] for i in _block(index, state, _entry(index, span_id, slot), "timing").data["items"]
        }
        assert values["timing_basis"] == "span_full" and values["charged_ms"] > 0, (span_id, slot)


def test_usage_block_reads_the_normalized_counters_and_nothing_else(state):
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
                output={
                    "content": "a",
                    "usage": {"prompt_tokens": 15124, "completion_tokens": 126, "total_tokens": 15250},
                },
                extra={
                    "llm.usage.input_tokens": 532,
                    "llm.usage.output_tokens": 126,
                    "llm.usage.reasoning_tokens": 80,
                    "llm.usage.cache_read_tokens": 14592,
                    "llm.usage.cache_write_tokens": None,
                    "llm.usage.total_tokens": 15250,
                    "llm.usage.cost_total": 0.00060885,
                },
            ),
        ],
    )
    index = _ready(state)
    usage = _block(index, state, _entry(index, "llm", "llm.output"), "usage")
    items = {i["key"]: i["value"] for i in usage.data["items"]}
    assert items == {
        "input_tokens": 532,
        "output_tokens": 126,
        "reasoning_tokens": 80,
        "cache_read_tokens": 14592,
        "cache_write_tokens": None,
        "total_tokens": 15250,
        "cost_total": 0.00060885,
    }
    assert all(i["source"] == "attribute" for i in usage.data["items"])
    raw = _block(index, state, _entry(index, "llm", "llm.output"), "raw")
    assert raw.data["value"]["attributes"]["llm.usage.input_tokens"] == 532


def test_feedback_blocks_list_each_skill_with_its_use_and_the_name_its_own_turn_injected_it_under(state):
    inject = lambda trace, span_id, parent, start, ids, names: _span(  # noqa: E731
        trace,
        span_id,
        "skill.inject",
        parent=parent,
        start=start,
        end=start,
        attrs={"skill.inject.via": "always", "skill.inject.ids": ids, "skill.inject.names": names},
    )
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            # The turn's own injection names `a`; `b` was injected with no name recorded.
            inject("t", "inj", "turn", 0, ["a", "b"], ["Alpha", ""]),
            _span(
                "t",
                "fb",
                "memory.feedback",
                parent="turn",
                start=1,
                end=2,
                attrs={"memory.session_id": SESSION, "memory.injected": ["a", "b", "c"], "memory.used": ["b"]},
            ),
            # Another turn and another trace both name `b` and `c`: neither reaches this turn's feedback.
            _turn(state, "t", "turn2", start=200, end=300),
            inject("t", "inj2", "turn2", 201, ["b", "c"], ["Wrong turn", "Wrong turn"]),
            _turn(state, "u", "turn3", start=400, end=500),
            inject("u", "inj3", "turn3", 401, ["c"], ["Wrong trace"]),
        ],
    )
    index = _ready(state)
    entry = _entry(index, "fb", "summary")
    assert _ids(_describe(index, state, entry))[:2] == ["injected", "used"]
    injected = _block(index, state, entry, "injected")
    assert injected.data["items"] == [
        {"id": "a", "name": "Alpha", "used": False},
        {"id": "b", "name": None, "used": True},
        {"id": "c", "name": None, "used": False},
    ]
    used = _block(index, state, entry, "used")
    assert used.data["items"] == [{"id": "b", "name": None, "used": True}]
    with pytest.raises(tdet.UnknownBlockError):
        _block(index, state, entry, "origin")
    with pytest.raises(tdet.UnknownBlockError):
        _block(index, state, _entry(index, "turn", "turn.input"), "tool")


# ── the files a span names ─────────────────────────────────────────────


def test_the_raw_record_lists_the_span_s_files_and_reads_each_by_its_cursor(state, monkeypatch):
    outside = state.parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    big = "x" * (tdet.ARTIFACT_LIMIT + 100)
    extra = {
        "a.artifact_path": _artifact(state, {"k": [1, 2]}, "a"),
        "a.artifact_bytes": 13,
        "b.artifact_path": _artifact(state, "plain text, not json", "b"),
        "c.artifact_path": _artifact(state, big, "c"),
        "d.artifact_path": str(outside),
        "e.artifact_path": str(state / "logs" / "audit-artifacts" / "gone.json"),
        "f.artifact_path": _artifact(state, {"long": "y" * 5000}, "f"),
    }
    _append(
        state,
        [_turn(state, "t", "turn", start=0, end=100), _tool(state, "t", "x", "turn", start=1, end=2, extra=extra)],
    )
    index = _ready(state)
    entry = _entry(index, "x", "tool.output")
    descriptor = _describe(index, state, entry)
    assert _ids(descriptor)[-3:] == ["raw", "files", "file"]
    files = next(b for b in descriptor.blocks if b.id == "files")
    assert files.total_items == 8
    # The directory reads no file: every name, in order, with the size recorded beside it and a cursor.
    directory = _block(index, state, entry, "files").data["items"]
    keys = [item["key"] for item in directory]
    assert keys == sorted(keys) and keys[0] == "a.artifact_path" and "tool.output.artifact_path" in keys
    assert directory[0]["size"] == 13 and directory[1]["size"] is None
    assert [item["index"] for item in directory] == list(range(8))
    body = {item["key"]: _block(index, state, entry, "file", cursor=item["cursor"]) for item in directory}
    one = lambda key: body[key].data["items"][0]  # noqa: E731
    assert one("a.artifact_path") == {
        "index": 0,
        "key": "a.artifact_path",
        "path": extra["a.artifact_path"],
        "size": 13,
        "kind": "json",
        "value": {"k": [1, 2]},
        "shown_bytes": len('{"k": [1, 2]}'),
        "truncated": None,
    }
    assert one("b.artifact_path")["kind"] == "text" and one("b.artifact_path")["text"] == "plain text, not json"
    # Over the read cap: the first 512 KiB, as text, saying why.
    assert one("c.artifact_path")["kind"] == "text" and one("c.artifact_path")["truncated"] == "file_limit"
    assert one("c.artifact_path")["shown_bytes"] == tdet.ARTIFACT_LIMIT
    assert one("d.artifact_path") == {**one("d.artifact_path"), "kind": "none", "reason": "artifact_outside_store"}
    assert body["d.artifact_path"].integrity == ("artifact_outside_store",)
    assert one("e.artifact_path")["kind"] == "none" and one("e.artifact_path")["reason"] == "artifact_missing"
    assert one("tool.output.artifact_path")["value"] == {"result": "ok"}
    assert all(b.total_items == 8 and b.next_cursor is None for b in body.values())
    # Without a cursor the first file; a cursor past the list is stale.
    assert _block(index, state, entry, "file").data["items"][0]["index"] == 0
    with pytest.raises(tidx.CursorExpiredError):
        _block(index, state, entry, "file", cursor=tdet._encode_cursor(entry.entry_id, entry.revision, index.epoch, 99))
    # A file under the read cap that cannot fit one response is cut to fit, and says so.
    monkeypatch.setattr(tdet, "RESPONSE_LIMIT", tdet.RESPONSE_RESERVE + 2000)
    cut = _block(index, state, entry, "file", cursor=directory[5]["cursor"]).data["items"][0]
    assert cut["key"] == "f.artifact_path" and cut["kind"] == "text" and cut["truncated"] == "response_limit"
    assert 0 < cut["shown_bytes"] < 5000


def test_the_files_directory_pages_by_cursor_and_a_span_without_files_has_none(state, monkeypatch):
    extra = {f"n{k:02d}.artifact_path": _artifact(state, {"n": k}, f"n{k}") for k in range(5)}
    _append(
        state,
        [
            _turn(state, "t", "turn", start=0, end=100),
            _tool(state, "t", "x", "turn", start=1, end=2, extra=extra),
            _span("t", "bare", "custom.step", parent="turn", start=3, end=4, attrs={"memory.session_id": SESSION}),
        ],
    )
    index = _ready(state)
    monkeypatch.setattr(tdet, "FILES_DIRECTORY_PAGE", 3)
    entry = _entry(index, "x", "tool.output")
    pages, cursor = [], None
    while True:
        page = _block(index, state, entry, "files", cursor=cursor)
        pages.append([item["index"] for item in page.data["items"]])
        cursor = page.next_cursor
        if cursor is None:
            break
    assert pages == [[0, 1, 2], [3, 4, 5], [6]]
    bare = next(e for e in index.entries() if e.span_id == "bare")
    assert "files" not in _ids(_describe(index, state, bare)) and "file" not in _ids(_describe(index, state, bare))
    with pytest.raises(tdet.UnknownBlockError):
        _block(index, state, bare, "file")
